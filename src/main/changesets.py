from __future__ import annotations

import json

import sqlalchemy as sa
import structlog
from sqlalchemy.orm import joinedload

import errors
from auth.models import AccessToken, ApiToken
from core import checks, comments, common, graphql, locks, pygithub, queue
from core.client import GithubClient
from core.models import (
    ChangeSet,
    ChangeSetConfig,
    ChangeSetMapping,
    ChangeSetRun,
    ChangeSetRunCheck,
    ChangeSetRunCommit,
    GithubRepo,
    PullRequest,
)
from core.status_codes import StatusCode
from main import app, celery, db
from util import req
from webhooks.models import MasterWebhook

logger = structlog.stdlib.get_logger()
DEFAULT_CHECK_NAME = "mergequeue/changeset"


# owner_id: This value should be from user_info table. When its value is None,
# it means the changeset is created from the backend instead of by the user.
def create_new_changeset(account_id: int, owner_id: int | None) -> ChangeSet:
    lock_name = f"changeset-account-{account_id}"
    with locks.lock("changeset", lock_name, expire=20):
        last_change_set = db.session.scalar(
            sa.select(ChangeSet)
            .where(ChangeSet.account_id == account_id, ChangeSet.deleted.is_(False))
            .order_by(ChangeSet.number.desc()),
        )
        last_deleted_change_set = db.session.scalar(
            sa.select(ChangeSet)
            .where(ChangeSet.account_id == account_id, ChangeSet.deleted.is_(True))
            .order_by(ChangeSet.number.desc()),
        )
        number = (last_change_set.number + 1) if last_change_set else 1
        if last_deleted_change_set and last_deleted_change_set.number >= number:
            number = last_deleted_change_set.number + 1

        new_change_set = ChangeSet(
            account_id=account_id,
            number=number,
            owner_id=owner_id,
        )
        db.session.add(new_change_set)
        db.session.commit()
        return new_change_set


@celery.task
def send_run_webhook(changeset_run_id: int) -> None:
    changeset_run = ChangeSetRun.get_by_id_x(changeset_run_id)
    change_set = changeset_run.change_set
    webhook = db.session.scalar(
        sa.select(MasterWebhook).where(
            MasterWebhook.account_id == change_set.account_id,
            MasterWebhook.deleted.is_(False),
        ),
    )
    owner_email = change_set.owner.email if change_set.owner else ""

    if changeset_run.commits:
        # there should be no existing commits. If there are, something went wrong.
        logger.error(
            "Changeset Run %d for changeset %d already has commits",
            changeset_run_id,
            change_set.number,
        )
    pr_list_json = []
    for pr in change_set.pr_list:
        pr_json = {
            "number": pr.number,
            "head_branch": pr.branch_name,
            "base_branch": pr.base_branch_name,
            "commit_sha": pr.head_commit_sha,
            "author": pr.creator.username,
            "repo": pr.repo.name,
        }
        pr_list_json.append(pr_json)
        commit = ChangeSetRunCommit(
            account_id=change_set.account_id,
            change_set_id=change_set.id,
            change_set_run_id=changeset_run_id,
            sha=pr.head_commit_sha,
            pull_request_id=pr.id,
        )
        db.session.add(commit)

    # Also save the state here, so if the webhook fails,
    # we don't end up in an inconsistent state. Worst case
    # this run will stay pending forever and user can manually
    # retrigger the CI.
    db.session.commit()
    custom_params = change_set.get_webhook_params()
    payload = {
        "action": "changeset_start_ci",
        "change_set_id": change_set.number,
        "status_run_id": changeset_run.id,
        "pull_requests": pr_list_json,
        "owner": {"email": owner_email},
        "custom_attributes": custom_params,
    }

    data_str = json.dumps(payload)
    signature = get_signature(change_set.account_id, data_str)
    response = req.post(
        webhook.url,
        data=data_str,
        headers={
            "X-MergeQueue-Signature": signature,
            "X-Aviator-Signature-SHA256": get_aviator_signature_sha256(
                change_set.account_id,
                data_str,
            ),
            "Content-Type": "application/json",
        },
        timeout=app.config["REQUEST_TIMEOUT_SEC"],
    )

    if response.status_code < 400:
        logger.info(
            "Posted webhook for Changeset %d status code %d response: %s",
            change_set.id,
            response.status_code,
            response.text,
        )
        webhook.status = "verified"
    else:
        logger.warning(
            "Webhook request failed for Changeset %d webhook %d status code %d %s",
            change_set.id,
            webhook.id,
            response.status_code,
            response.text,
        )
        set_change_set_run_failure(changeset_run)
        webhook.status = "failed"
    db.session.commit()


def set_change_set_run_failure(changeset_run: ChangeSetRun) -> None:
    if changeset_run.checks:
        raise Exception("Cannot set failure on a change set run with checks")
    check = ChangeSetRunCheck(
        change_set_run_id=changeset_run.id,
        status="failure",
        name=DEFAULT_CHECK_NAME,
    )
    db.session.add(check)
    db.session.commit()


def refresh_status_checks(pr: PullRequest) -> None:
    # We only care about the most recent check run.
    check_commit: ChangeSetRunCommit | None = db.session.scalar(
        sa.select(ChangeSetRunCommit)
        .where(
            ChangeSetRunCommit.pull_request_id == pr.id,
            ChangeSetRunCommit.sha == pr.head_commit_sha,
            ChangeSetRunCommit.deleted.is_(False),
        )
        .order_by(ChangeSetRunCommit.change_set_run_id.desc()),
    )
    if not check_commit:
        logger.info(
            "No check runs associated with a PR",
            repo_id=pr.repo_id,
            pr_id=pr.id,
            pr_number=pr.number,
        )
        return

    # Get all the associated checks returned by the user.
    change_set_run_checks: list[ChangeSetRunCheck] = db.session.scalars(
        sa.select(ChangeSetRunCheck).where(
            ChangeSetRunCheck.change_set_run_id == check_commit.change_set_run_id,
            ChangeSetRunCheck.deleted.is_(False),
        ),
    ).all()
    for change_set_run_check in change_set_run_checks:
        create_or_update_check_with_retries(change_set_run_check, check_commit)
    logger.info(
        "Refreshed checks for Repo %d PR %d sha %s",
        pr.repo_id,
        pr.number,
        pr.head_commit_sha,
    )


@celery.task
def add_status_checks(change_set_run_check_id: int) -> None:
    change_set_run_check = ChangeSetRunCheck.get_by_id_x(change_set_run_check_id)
    change_set_run = change_set_run_check.change_set_run
    for commit in change_set_run.commits:
        create_or_update_check_with_retries(change_set_run_check, commit)


def create_or_update_check_with_retries(
    change_set_run_check: ChangeSetRunCheck,
    commit: ChangeSetRunCommit,
) -> None:
    try:
        create_or_update_check(change_set_run_check, commit)
    except Exception:
        logger.exception(
            "Failed to update check for change_set_run_check on commit",
            change_set_run_check_id=change_set_run_check.id,
            commit_id=commit.id,
        )
        create_or_update_check_async.delay(change_set_run_check.id, commit.id)


@celery.task(
    autoretry_for=(Exception,),
    retry_backoff=10,
    retry_kwargs={"max_retries": 5},
    retry_jitter=True,
)
def create_or_update_check_async(change_set_run_check_id: int, commit_id: int) -> None:
    change_set_run_check = ChangeSetRunCheck.get_by_id_x(change_set_run_check_id)
    commit = ChangeSetRunCommit.get_by_id_x(commit_id)
    assert commit, f"ChangeSetRunCommit {commit_id} not found"

    create_or_update_check(change_set_run_check, commit)


def create_or_update_check(
    change_set_run_check: ChangeSetRunCheck,
    commit: ChangeSetRunCommit,
) -> None:
    change_set_run = change_set_run_check.change_set_run
    client, access_token, pull = get_github_objects(commit.pull_request)
    change_set_number = change_set_run.change_set.number
    external_url = app.config["BASE_URL"] + "/changeset/" + str(change_set_number)
    status = change_set_run_check.to_check_status()
    conclusion: pygithub.NotSetType | str = pygithub.NotSet
    if status == "completed":
        conclusion = change_set_run_check.to_check_conclusion() or pygithub.NotSet
    db_check_run = commit.get_github_check(change_set_run_check)
    if db_check_run:
        check_run = client.repo.get_check_run(db_check_run.check_run_id)  # type: ignore[arg-type]
        # Only update the existing check_run if it's not completed. Otherwise, we'll
        # create a new one.
        if check_run.status != "completed":
            check_run.edit(
                change_set_run_check.name or "mergequeue/changeset",
                commit.sha,  # type: ignore[arg-type]
                details_url=change_set_run_check.target_url or external_url,
                external_id=str(change_set_run_check.id),
                status=status,
                conclusion=conclusion,
                output={
                    "title": "Changeset #" + str(change_set_number),
                    "summary": change_set_run_check.message or "",
                },
            )
            logger.info(
                "Updated check for change_set_run_check_id %d change_set_run_id %d on changeset %d pr %d",
                change_set_run_check.id,
                change_set_run.id,
                change_set_number,
                commit.pull_request.number,
            )
            return
    check_run = client.repo.create_check_run(
        change_set_run_check.name,
        commit.sha,  # type: ignore[arg-type]
        details_url=change_set_run_check.target_url or external_url,
        external_id=str(change_set_run_check.id),
        status=status,
        conclusion=conclusion,
        output={
            "title": "Changeset #" + str(change_set_number),
            "summary": change_set_run_check.message or "",
        },
    )
    logger.info(
        "Created check for change_set_run_check_id %d change_set_run_id %d on changeset %d pr %d",
        change_set_run_check.id,
        change_set_run.id,
        change_set_number,
        commit.pull_request.number,
    )
    commit.set_github_check_id(change_set_run_check, check_run.id)
    db.session.commit()


@celery.task(
    autoretry_for=(Exception,),
    retry_backoff=10,
    retry_kwargs={"max_retries": 5},
    retry_jitter=True,
)
def validate_and_merge_prs(change_set_id: int) -> None:
    lock_name = f"changeset-{change_set_id}"
    with locks.lock("changeset", lock_name, expire=90):
        logger.info("Change set %d starting validation", change_set_id)
        change_set = ChangeSet.get_by_id_x(change_set_id)
        if change_set.status != "queued":
            # When a user manually cancels the change set merging, the status
            # becomes not "queued". Skip processing this.
            logger.info(
                "Change set %d status is not queued, but %s",
                change_set_id,
                change_set.status,
            )
            return
        for pr in change_set.pr_list:
            status = validate_pr_status(change_set, pr)
            if status == "failure":
                change_set.status = "failure"
                db.session.commit()
                logger.info("Change set %d failed validation", change_set_id)
                return
            if status == "pending":
                logger.info("Change set %d validation pending", change_set_id)
                return

        for pr in change_set.pr_list:
            if pr.status != "merged":
                pr.status_code = StatusCode.QUEUED
        db.session.commit()

        logger.info("Change set %d starting merging", change_set_id)
        # otherwise, merge the PRs
        result = True
        needs_retry = False
        for pr in change_set.pr_list:
            client, access_token, pull = get_github_objects(pr)
            queue.merge_pr(client, pr.repo, pr, pull, optimistic_botpr=None)
            if pr.status == "blocked":
                logger.error(
                    "PR failed to merge after validation. Repo %d PR %d",
                    pr.repo_id,
                    pr.number,
                )
                update_mapping(change_set, pr, StatusCode.BLOCKED_BY_GITHUB)
                result = False
            elif pr.status != "merged":
                needs_retry = True
                logger.error(
                    "PR merge didn't finish, scheduling retry. Repo %d PR %d",
                    pr.repo_id,
                    pr.number,
                )
                retry_merge.delay(pr.id, change_set.id)
            else:
                update_mapping(change_set, pr, StatusCode.MERGED_BY_MQ)

        if result and not needs_retry:
            change_set.status = "merged"
            db.session.commit()
            logger.info("Change set %d successfully merged", change_set_id)

        if not result:
            change_set.status = "failure"
            db.session.commit()


def process_queued_changesets() -> None:
    change_sets: list[ChangeSet] = db.session.scalars(
        sa.select(ChangeSet).where(
            ChangeSet.status == "queued",
            ChangeSet.deleted.is_(False),
        ),
    ).all()
    for change_set in change_sets:
        validate_and_merge_prs.delay(change_set.id)


@celery.task(
    autoretry_for=(Exception,),
    retry_backoff=10,
    retry_kwargs={"max_retries": 5},
    retry_jitter=True,
)
def retry_merge(pr_id: int, change_set_id: int) -> None:
    pr = PullRequest.get_by_id_x(pr_id)
    pr.bind_contextvars()
    pr.repo.bind_contextvars()
    if pr.status == "merged":
        logger.info("Retry called. PR already merged")
        return

    logger.info("Attempting Retry for pr %d repo %d.", pr.number, pr.repo_id)
    change_set = ChangeSet.get_by_id_x(change_set_id)
    client, access_token, pull = get_github_objects(pr)
    queue.merge_pr(client, pr.repo, pr, pull, optimistic_botpr=None)

    # Tell mypy that the pr might have changed.
    # Without this it complains because we checked for `pr.status == "merged"
    # above and are re-checking it below.
    pr = pr

    if pr.status == "blocked":
        logger.error("PR failed to merge after validation")
        change_set.status = "failure"
        db.session.commit()
    logger.info("Retry merge", pr_status=pr.status)
    if pr.status != "merged":
        raise errors.MergeFailed(
            "Failed to merge PR %d repo %d" % (pr.number, pr.repo_id),
        )

    # If all PRs are merged, update changeset status
    for pr in change_set.pr_list:
        if pr.status != "merged":
            return

    change_set.status = "merged"
    db.session.commit()
    logger.info("Change set %d successfully merged", change_set_id)


def validate_pr_status(change_set: ChangeSet, pr: PullRequest) -> checks.TestResult:
    """
    validates whether a pr in a change set is in a good state to merge
    throws UnknownMergeableStatusException which means that we should retry in a few seconds

    :param change_set: the change set
    :param pr: the pr in the change set
    :return: True if pr is in a state to merge
    """
    client, access_token, pull = get_github_objects(pr)
    if pull.merged:
        logger.info(
            "PR in changeset already merged",
            repo_id=pr.repo_id,
            pr_number=pr.number,
            pr_id=pr.id,
        )

        )
        update_mapping(change_set, pr, StatusCode.NOT_APPROVED)
        return "pending"

    result = queue.check_mergeability_for_merge(
        client,
        pr.repo,
        pr,
        pull,
        mark_blocked=False,
    )
    if result.test_result != "success":
        logger.info(
            "PR validation failed: CI not passing",
            repo_id=pr.repo_id,
            pr_number=pr.number,
            test_result=result.test_result,
            ci_map=list(result.ci_map.keys()),
        )
        update_mapping(change_set, pr, StatusCode.FAILED_TESTS)
        return result.test_result

    if pull.mergeable is None or pull.mergeable_state == "unknown":
        logger.info(
            "PR validation pending: mergeable status unknown",
            repo_id=pr.repo_id,
            pr_number=pr.number,
            pr_id=pr.id,
        )
        update_mapping(change_set, pr, StatusCode.PENDING_MERGEABILITY)
        logger.info(
            "Found PR that is in unknown mergeable state",
            pr_id=pr.id,
            pr_number=pr.number,
            repo_id=pr.repo_id,
        )
        return "pending"

    if not pull.mergeable or pull.mergeable_state in ["blocked", "dirty", "draft"]:
        # Reference: https://docs.github.com/en/graphql/reference/enums#mergeablestate
        # A behind state means that the base ref is not up-to-date.
        # An unstable state means that some non-blocking checks are failing.
        # A blocked state would mean that the PR is blocked by a required status check.
        #   It's possible that the status may be returned as blocked for missing approvals,
        #   but we check for that above.
        # A dirty state means that the PR is not mergeable due to merge conflicts.
        logger.info(
            "PR validation failed: not mergeable",
            repo_id=pr.repo_id,
            pr_number=pr.number,
            pr_id=pr.id,
            mergeable=pull.mergeable,
            mergeable_state=pull.mergeable_state,
        )
        update_mapping(change_set, pr, StatusCode.BLOCKED_BY_GITHUB)
        return "failure"

    return "success"


def get_github_objects(
    pr: PullRequest,
) -> tuple[GithubClient, AccessToken, pygithub.PullRequest]:
    repo = GithubRepo.get_by_id_x(pr.repo_id)
    access_token, client = common.get_client(repo)
    pull = client.get_pull(pr.number)
    return client, access_token, pull


def update_mapping(
    change_set: ChangeSet,
    pr: PullRequest,
    status_code: StatusCode,
) -> None:
    pr.status_code = status_code
    db.session.commit()


def is_run_latest(change_set: ChangeSet) -> bool:
    if not requires_ci(change_set.account_id):
        return True

    latest_run = change_set.latest_status_run
    if not latest_run:
        return False

    run_commits = latest_run.commits
    pr_to_commit = {commit.pull_request_id: commit.sha for commit in run_commits}
    for pr in change_set.pr_list:
        if pr.id not in pr_to_commit:
            logger.info(
                "Changeset missing PR in the commits",
                changeset_id=change_set.id,
                changeset_number=change_set.number,
                pr_id=pr.id,
            )
            return False
        if pr.head_commit_sha != pr_to_commit[pr.id]:
            logger.info(
                "Changeset PR commit is stale",
                changeset_id=change_set.id,
                changeset_number=change_set.number,
                pr_id=pr.id,
            )
            return False

    return True


def requires_ci(account_id: int) -> bool:
    config: ChangeSetConfig | None = db.session.scalar(
        sa.select(ChangeSetConfig).where(
            ChangeSetConfig.account_id == account_id,
            ChangeSetConfig.deleted.is_(False),
        ),
    )
    return config.require_global_ci if config else False


def get_signature(account_id: int, body: str) -> str:
    token: ApiToken | None = db.session.scalar(
        sa.select(ApiToken).where(
            ApiToken.account_id == account_id,
            ApiToken.deleted.is_(False),
        ),
    )
    return token.calculate_signature(body) if token else ""


def get_aviator_signature_sha256(account_id: int, body: str) -> str:
    token: ApiToken | None = db.session.scalar(
        sa.select(ApiToken).where(
            ApiToken.account_id == account_id,
            ApiToken.deleted.is_(False),
        ),
    )
    return token.calculate_signature_256(body) if token else ""


def add_pr_to_changeset(change_set: ChangeSet, pr: PullRequest) -> ChangeSet:
    if pr.account_id != change_set.account_id:
        raise Exception("Change set account doesn't match")

    existing_changeset: ChangeSetMapping | None = db.session.scalar(
        sa.select(ChangeSetMapping).where(
            ChangeSetMapping.pull_request_id == pr.id,
        ),
    )
    if existing_changeset and not existing_changeset.change_set.deleted:
        raise PullRequestAlreadyInChangesetError

    if pr not in change_set.pr_list:
        change_set.pr_list.append(pr)
        db.session.commit()
        comments.post_pull_comment.delay(
            pr.id,
            "change_set",
            note=str(change_set.number),
        )
    else:
        logger.info(
            "Changeset already contains the PR",
            change_set=change_set.id,
            pr_number=pr.number,
        )
    return change_set


class PullRequestAlreadyInChangesetError(Exception):
    def __init__(self) -> None:
        super().__init__("The PR is already associated with another changeset.")


def create_changeset_from_prs(
    pr_list: list[PullRequest],
    owner_id: int | None = None,
) -> ChangeSet:
    if not pr_list:
        raise Exception("Empty PR list")

    if owner_id is None:
        # Attempt to fetch aviator user from PR
        aviator_user = pr_list[0].creator.aviator_user
        if aviator_user:
            owner_id = aviator_user.id

    cs = create_new_changeset(pr_list[0].account_id, owner_id)
    cs.pr_list = pr_list
    db.session.commit()
    for pr in pr_list:
        comments.post_pull_comment.delay(pr.id, "change_set", note=str(cs.number))
    return cs


def create_changeset_from_relevant_prs(pr: PullRequest) -> ChangeSet | None:
    # Find relevant PRs with the same branch name
    # and their associated Changesets
    relevant_prs: list[PullRequest] = (
        db.session.scalars(
            sa.select(PullRequest)
            .options(joinedload(PullRequest.change_sets))  # To avoid N+1 queries
            .where(
                PullRequest.account_id == pr.account_id,
                PullRequest.branch_name == pr.branch_name,
                PullRequest.deleted.is_(False),
                PullRequest.status != "merged",
            ),
        )
        .unique()
        .all()
    )

    # This is not ideal but a PR that's reopened is considered "merged" and will not
    # be included by the query. This handles the edge case
    if pr not in relevant_prs:
        relevant_prs.insert(0, pr)

    change_sets = set()
    for p in relevant_prs:
        for cs in p.change_sets:
            if not cs.deleted:
                change_sets.add(cs)

    change_set = None
    # Only add the new PR, not any other PRs that might not be in a change set,
    # to the existing change sets. The reason is that those PRs might have
    # been manually removed from a change set. Adding them back could result
    # in a very annoying workflow
    if len(change_sets) == 1:  # Only add PR to change set when it's unique
        change_set = next(iter(change_sets))
        add_pr_to_changeset(change_set, pr)
        logger.info(
            "Added PR to an existing relevant change set",

```python
            change_set=change_set.id,
            pr_number=pr.number,
            branch_name=pr.branch_name,
        )
    elif len(change_sets) > 1:
        # No action. Log the edge case for potential investigation
        logger.info(
            "Found multiple relevant change sets. PR not added.",
            change_set_ids=[cs.id for cs in change_sets],
            pr_number=pr.number,
            branch_name=pr.branch_name,
            relevant_pr_numbers=[p.number for p in relevant_prs],
        )
    # If there is no existing change set, we will create a new change set with all
    # relevant PRs.
    elif len(relevant_prs) > 1:
        change_set = create_changeset_from_prs(relevant_prs)
        logger.info(
            "Created new change set from relevant PRs",
            change_set=change_set.id,
            pr_number=pr.number,
            branch_name=pr.branch_name,
            relevant_pr_numbers=[p.number for p in relevant_prs],
        )

    return change_set
```