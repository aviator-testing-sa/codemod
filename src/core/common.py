from __future__ import annotations

import datetime
import fnmatch
import subprocess
from collections.abc import Iterable
from typing import Any, Union, cast

import pydantic
import requests.exceptions
import sqlalchemy as sa
import structlog

import core.attention_set
import errors
import util.posthog_util
from auth.models import AccessToken, Account, User
from core import (
    connect,
    github_user,
    locks,
    pygithub,
    rate_limit,
    stack_manager,
    subscriber,
)
from core.client import GithubClient
from core.models import (
    Activity,
    ActivityOpenedPayload,
    ActivityType,
    AuditLog,
    AuditLogAction,
    AuditLogEntity,
    BaseBranch,
    BotPr,
    BranchCommitMapping,
    GitCommit,
    GithubRepo,
    GithubTest,
    GithubUser,
    MergeOperation,
    PullRequest,
    PullRequestQueuePrecondition,
    PullRequestUserSubscription,
    ReadyHookState,
    ReadySource,
    TestStatus,
)
from core.status_codes import StatusCode
from lib import gh
from main import app, celery, db
from schema.github.base import PullRequest as SchemaPullRequest
from schema.github.base import PullRequestRef
from schema.github.base import Repository as SchemaRepository
from schema.github.base import User as SchemaUser
from slackhook import notify
from util import dbutil
from webhooks import controller as hooks

CHECK_RUN_NAME: str = app.config["CHECK_RUN_NAME"]

logger = structlog.stdlib.get_logger()


def get_client(
    repo: GithubRepo,
    force_access_token: bool = False,
) -> tuple[AccessToken, GithubClient]:
    access_token = get_access_token(repo, force=force_access_token)
    slog = logger.bind(repo_id=repo.id, access_token_id=access_token.id)

    try:
        github_client = GithubClient(access_token, repo)
    except pygithub.BadCredentialsException as exc:
        access_token.is_valid = False
        db.session.commit()
        slog.info(
            "Got bad credentials while creating GitHub client, marking access token as invalid",
            exc_info=exc,
        )
        raise Exception(
            "Failed to fetch client for repo %d due to bad credentials" % repo.id
        )
    except Exception as exc:
        if is_network_issue(exc):
            slog.info("Got network issue while creating GitHub client", exc_info=exc)
            raise errors.TimeoutException(
                "Failed to fetch client for repo %d due to time out" % repo.id
            )
        slog.warning("Failed to create GitHub client for repository", exc_info=exc)
        raise Exception("Failed to fetch client for repo %d" % repo.id)

    if not access_token.is_valid:
        slog.info("Marking access token as valid (was marked as not valid)")
        access_token.is_valid = True
        db.session.commit()
    return access_token, github_client


def get_access_token(repo: GithubRepo, *, force: bool = False) -> AccessToken:
    if not repo:
        raise errors.AccessTokenException("no repo found")

    access_token: AccessToken | None
    if repo.access_token_id:
        access_token = AccessToken.get_by_id_x(repo.access_token_id)
    else:
        access_token = AccessToken.query.filter_by(  # nosemgrep
            account_id=repo.account_id
        ).first()
        if access_token:
            repo.access_token_id = access_token.id
            db.session.commit()

    if not access_token:
        logger.info("Access token request failed")
        raise errors.AccessTokenException(
            "Failed to fetch access token for repo %d" % repo.id
        )

    # Try to refresh the token if we need to
    refreshed_access_token = connect.ensure_access_token(access_token, force=force)

    if not refreshed_access_token or not refreshed_access_token.token:
        # 1. Label the access_token as invalid
        # access_token.is_valid = False

        # TODO: We will invalidate the tokens after data verification.
        # NOTE: Sometimes a good token may fail. So we are not aggressively invalidating for now
        logger.info("Invalid access token", token_id=access_token.id)

        # 2. Try to see if there are other tokens available with a different installation id.
        #    This handles the scenario where user might have uninstalled and re-installed aviator-app
        refreshed_access_token = get_alternative_access_token(
            repo=repo, bad_token=access_token, force=force
        )

        if not refreshed_access_token or not refreshed_access_token.token:
            logger.info("Access token request failed")
            raise errors.AccessTokenException(
                "Failed to fetch access token for repo %d" % repo.id
            )

    if not refreshed_access_token.login and connect.is_dev_token(
        refreshed_access_token
    ):
        gh_user_data = connect.fetch_authenticated_user_login(
            token=refreshed_access_token.token
        )
        if not gh_user_data:
            logger.info(
                "Failed to fetch login information for access token",
                access_token_id=refreshed_access_token.id,
                account_id=refreshed_access_token.account_id,
            )
            return refreshed_access_token
        refreshed_access_token.login = gh_user_data.login
        logger.info(
            "Set login information for access token",
            access_token_id=refreshed_access_token.id,
            access_token_login=refreshed_access_token.login,
        )

    db.session.commit()

    return refreshed_access_token


def ensure_access_token(account_id: int) -> AccessToken:
    access_token: AccessToken | None = db.session.scalar(
        sa.select(AccessToken).where(
            AccessToken.account_id == account_id,
            AccessToken.is_valid == True,
            AccessToken.deleted == False,
        )
    )
    if not access_token:
        raise errors.AccessTokenException(
            "Failed to fetch access token for account %d" % account_id
        )
    refreshed_access_token = connect.ensure_access_token(access_token)
    if not refreshed_access_token:
        raise errors.AccessTokenException(
            "Failed to refresh access token for %d" % access_token.id
        )
    return refreshed_access_token


def get_alternative_access_token(
    repo: GithubRepo, bad_token: AccessToken, force: bool = False
) -> AccessToken | None:
    other_access_tokens = db.session.scalars(
        sa.select(AccessToken).where(
            AccessToken.id != bad_token.id,
            AccessToken.account_id == bad_token.account_id,
            AccessToken.is_valid == True,
            AccessToken.deleted == False,
        )
    ).all()

    for token in other_access_tokens:
        refreshed_access_token = connect.ensure_access_token(token, force=force)

        if not refreshed_access_token or not refreshed_access_token.token:
            # TODO: We will invalidate the tokens after data verification.
            # NOTE: Sometimes a good token may fail. So we are not aggressively invalidating for now
            logger.info("Invalid access token", token_id=token.id)
        else:
            # Verify if the access token actually can access the repo
            try:
                client = GithubClient(access_token=refreshed_access_token, repo=repo)
                _ = client.repo.default_branch
            except Exception:
                continue

            logger.info(
                "Found an alternative token for repo.",
                repo_id=repo.id,
                alternative_token_id=refreshed_access_token.id,
            )
            repo.access_token_id = refreshed_access_token.id
            db.session.commit()
            return refreshed_access_token

    return None


def get_repo_by_name(
    repo_name: str, *, require_billing_active: bool = True
) -> GithubRepo | None:
    """
    :param repo_name: the name of the repo we are getting
    :param require_billing_active: if True then we will only return repos that have accounts with active billing
    :return: repo with a name matching the provided repo_name and conditions, or None if no such repo exists
    """
    repo: GithubRepo | None = (
        GithubRepo.query.filter(
            GithubRepo.deleted == False,
            GithubRepo.name == repo_name,
            sa.or_(GithubRepo.active, GithubRepo.flexreview_active),
        )
        .join(Account)
        .filter(Account.billing_active)
        .order_by(
            GithubRepo.id.desc(),
        )
        .first()
    )
    # Try getting a non-billed repo if a billed one does not exist.
    # Added order by active DESC so that repos where active=True are still preferred.
    if not repo and not require_billing_active:
        r: GithubRepo | None = (
            GithubRepo.query.filter(
                GithubRepo.deleted == False, GithubRepo.name == repo_name
            )
            .order_by(
                GithubRepo.active.desc(),
                GithubRepo.flexreview_active.desc(),
                GithubRepo.id.desc(),
            )
            .first()
        )
        return r
    return repo


def get_repo_by_id(
    repo_id: int, *, require_billing_active: bool = True
) -> GithubRepo | None:
    """
    :param repo_id: internal ID of the repo
    :param require_billing_active: if True then we will only return repos that have accounts with active billing
    :return: repo with a name matching the provided repo_name and conditions, or None if no such repo exists
    """
    repo = GithubRepo.get_by_id(repo_id)
    if not require_billing_active:
        return repo
    if repo and repo.account.billing_active:
        return repo
    return None


def get_repo_for_account(account_id: int, repo_name: str) -> GithubRepo | None:
    repo: GithubRepo | None = (
        GithubRepo.query.filter_by(name=repo_name, account_id=account_id)
        .join(Account)
        .filter(Account.billing_active)
        .first()
    )
    return repo


def get_commit_for_repo(repo_id: int, commit_sha: str) -> GitCommit:
    commit: GitCommit | None = GitCommit.query.filter_by(
        repo_id=repo_id, sha=commit_sha
    ).first()
    if not commit:
        logger.info(
            "Missing commit database model, creating...",
            repo_id=repo_id,
            commit_sha=commit_sha,
        )
        commit = GitCommit(repo_id=repo_id, sha=commit_sha)
        db.session.add(commit)
        db.session.commit()
    return commit


@celery.task
def update_commit_date(repo_id: int, sha: str) -> None:
    commit: GitCommit | None = GitCommit.query.filter_by(
        repo_id=repo_id, sha=sha
    ).first()
    assert commit, f"Commit {sha} not found in repo {repo_id}"
    repo = GithubRepo.get_by_id_x(repo_id)
    if not repo.active:
        return
    if rate_limit.is_gh_rest_api_limit_low(repo.account_id):
        logger.info("Rate limit is low, skipping update_commit_date", repo_id=repo_id)
        return
    access_token, client = get_client(repo)
    if not access_token or not access_token.is_valid:
        return
    try:
        git_commit = client.repo.get_commit(sha)
    except pygithub.UnknownObjectException:
        logger.info(
            "Commit is missing from GitHub",
            repo_id=repo_id,
            commit_sha=sha,
        )
        return
    commit.created = git_commit.commit.author.date
    db.session.commit()


def is_bot_user(account_id: int, login: str) -> bool:
    if login == app.config["GITHUB_LOGIN_NAME"]:
        return True
    # logins associated with access tokens should all be bot users per ankit
    return login in [
        at.login
        for at in (
            AccessToken.query.filter_by(account_id=account_id)
            .filter(AccessToken.login.isnot(None))
            .all()
        )
    ]


def is_av_bot_pr(account_id: int, *, author_login: str, title: str) -> bool:
    return is_bot_user(account_id, author_login) and title.startswith(
        app.config["PR_TITLE_PREFIX"]
    )


def has_label(pull: pygithub.PullRequest, label: str) -> bool:
    labels = [l.name for l in pull.labels]
    return label in labels


def ensure_github_test(
    repo: GithubRepo,
    test_name: str,
    provider: dbutil.JobProviders = dbutil.JobProviders.unknown,
) -> GithubTest | None:
    if is_av_custom_check(test_name):
        return None
    with locks.lock(
        "create-gh-test", f"create-gh-test-{repo.id}-{test_name}", expire=10
    ):
        gh_test: GithubTest | None = GithubTest.query.filter_by(
            repo_id=repo.id, name=test_name
        ).first()
        if not gh_test:
            gh_test = GithubTest(repo_id=repo.id, name=test_name, ignore=True)
            logger.info(
                "Creating new GithubTest model",
                repo_id=repo.id,
                test_name=test_name,
            )
            db.session.add(gh_test)
        if gh_test and (
            not gh_test.provider or gh_test.provider == dbutil.JobProviders.unknown
        ):
            gh_test.provider = provider
        db.session.commit()
    return gh_test


def ensure_commit(
    repo_id: int,
    sha: str,
) -> GitCommit:
    with locks.lock("git-commit", f"git-commit-{repo_id}-{sha}", expire=10):
        commit: GitCommit | None = GitCommit.query.filter_by(
            repo_id=repo_id, sha=sha
        ).first()
        if commit:
            return commit
        commit = GitCommit(repo_id=repo_id, sha=sha)
        db.session.add(commit)
        db.session.commit()
        update_commit_date.delay(repo_id, sha)
        return commit


def ensure_pull_request_subscribers(
    repo: GithubRepo,
    pr: PullRequest,
    pull: _GHPullRequestData,
) -> list[PullRequestUserSubscription]:
    subs: list[PullRequestUserSubscription] = []
    sub: PullRequestUserSubscription
    for u in pull.requested_reviewers:
        # TODO:
        # See the comment on lib.gh.PullRequest.requested_reviewers
        # Allegedly this can be a Team object, but it doesn't seem(?) to
        # actually ever happen.
        if isinstance(u, gh.Team):
            continue
        ghu = github_user.ensure(
            account_id=repo.account_id,
            login=u.login,
            gh_type=u.type,
            gh_node_id=u.node_id,
            gh_database_id=u.id,
        )
        sub = subscriber.ensure_pull_request_subscriber(
            pull_request_id=pr.id,
            github_user_id=ghu.id,
        )
        subs.append(sub)

    # Ensure PR author is a subscriber as well.
    author_user = github_user.ensure(
        account_id=repo.account_id,
        login=pull.user.login,
        gh_type=pull.user.type,
        gh_node_id=pull.user.node_id,
        gh_database_id=pull.user.id,
    )
    author_sub = subscriber.ensure_pull_request_owner_subscription(
        pull_request_id=pr.id,
        author_user_id=author_user.id,
    )
    subs.append(author_sub)

    return subs


# Either a pygithub PullRequest or a lib/gh PullRequest object.
# We're trying to transition away from pygithub, so we should try to use the
# lib/gh version whenever possible. They expose similar enough interfaces for
# most cases that this is pretty easy to do.
_GHPullRequestData = Union["pygithub.PullRequest", "gh.PullRequest"]


def _pull_node_id(pull: _GHPullRequestData) -> str:
    if isinstance(pull, pygithub.PullRequest):
        return cast(str, pull._rawData["node_id"])
    return pull.node_id


def ensure_pull_request(
    repo: GithubRepo,
    pull: _GHPullRequestData,
    webhook_time: datetime.datetime | None = None,
    *,
    allow_av_bot_pr: bool = False,
) -> tuple[GithubUser, PullRequest]:
    if not allow_av_bot_pr and is_av_bot_pr(
        repo.account_id,
        author_login=pull.user.login,
        title=pull.title,
    ):
        raise ValueError("Refusing to create PullRequest object for Aviator botPR.")

    git_user = github_user.ensure(
        account_id=repo.account_id,
        login=pull.user.login,
        gh_type=pull.user.type,
        gh_node_id=pull.user.node_id,
        gh_database_id=pull.user.id,
    )

    lock_name = "create-pr-%d-%d" % (pull.number, repo.id)
    with locks.lock("create-pr", lock_name, expire=10):
        # Get or create PR atomically.
        pr = PullRequest.query.filter_by(
            number=int(pull.number), repo_id=repo.id
        ).first()
        old_base_branch_name = pr.base_branch_name if pr else None
        if not pr:
            pr = PullRequest(
                number=int(pull.number),
                repo_id=repo.id,
                account_id=repo.account_id,
                creator=git_user,
                creation_time=pull.created_at,
                title=pull.title,
                branch_name=pull.head.ref,
                base_branch_name=pull.base.ref,
                # setting target_branch temporarily to avoid null failure
                target_branch_name=pull.base.ref,
                # pygithub.PullRequest.body can be None when the PR body is
                # empty.
                body=pull.body or "",
                status="open",
                status_code=StatusCode.NOT_QUEUED,
                head_commit_sha=pull.head.sha,
                base_commit_sha=pull.base.sha,
                gh_node_id=_pull_node_id(pull),
            )
            if pull.additions is not None and pull.deletions is not None:
                pr.num_modified_lines = pull.additions + pull.deletions
            if pull.draft:
                pr.status_code = StatusCode.IN_DRAFT
                pr.is_draft = True
            db.session.add(pr)
            activity = Activity(
                created=pull.created_at,
                repo_id=repo.id,
                name=ActivityType.OPENED,
                pull_request=pr,
                payload=ActivityOpenedPayload(head_commit_oid=pr.head_commit_sha),
            )
            db.session.add(activity)
            db.session.commit()
            util.posthog_util.capture_pull_request_event(
                util.posthog_util.PostHogEvent.CREATE_PULL_REQUEST, pr
            )
            pilot_data = get_pilot_data(pr, "opened")
            hooks.call_master_webhook.delay(
                pr.id, "opened", pilot_data=pilot_data, ci_map={}, note=""
            )
        elif webhook_time and webhook_time < pr.modified:
            low_rate_limit = rate_limit.is_gh_rest_api_limit_low(repo.account_id)
            logger.info(
                "PR has been modified since the webhook was sent",
                pr_number=pr.number,
                repo_id=pr.repo_id,
                webhook_time=webhook_time,
                pr_modified=pr.modified,
                low_rate_limit=low_rate_limit,
            )
            if not low_rate_limit:
                _, client = get_client(repo)
                pull = client.get_pull(pr.number)

        # Move all logic inside the redis lock to avoid inconsistencies.
        # These are all just DB calls.
        pr.title = pull.title
        # pygithub.PullRequest.body can be None when the PR body is empty.
        pr.body = pull.body or ""
        pr.branch_name = pull.head.ref
        pr.base_branch_name = pull.base.ref
        pr.base_commit_sha = pull.base.sha
        # Convert to bool in case this is None.
        pr.is_draft = bool(pull.draft)
        if pull.additions is not None and pull.deletions is not None:
            pr.num_modified_lines = pull.additions + pull.deletions
        # Make sure merge_commit_sha is correct
        if pull.merged:
            pr.merge_commit_sha = pull.merge_commit_sha

        # If the base branch is updated, attempt to clean up the old base branch.
        if old_base_branch_name and old_base_branch_name != pull.base.ref:
            # If there exists a merged PR where branch_name = old_base_branch_name,
            #   we can delete the old_base_branch_name.
            merged_prs = PullRequest.query.filter_by(
                repo_id=repo.id, status="merged", branch_name=old_base_branch_name
            ).count()
            if merged_prs > 0:
                _, client = get_client(repo)
                delete_ref_if_applicable(client, repo, old_base_branch_name)

        # Handle stacked PRs
        old_stack_order = [pr.number for pr in stack_manager.get_stack_linearized(pr)]
        stack_parent = _find_stack_parent(repo, pr, pull)
        pr.stack_parent = stack_parent
        pr.target_branch_name = (
            stack_parent.target_branch_name if stack_parent else pr.base_branch_name
        )
        new_stack_order = [pr.number for pr in stack_manager.get_stack_linearized(pr)]
        if old_stack_order != new_stack_order:
            logger.info(
                "Pull request stack order changed",
                repo_id=repo.id,
                pr_id=pr.id,
                pr_number=pr.number,
                old_order=old_stack_order,
                new_order=new_stack_order,
            )
        db.session.commit()

    # Add review_requested users to github_users if they don't already exist,
    # also create PullRequestSubscription entries to track their attention.
    ensure_pull_request_subscribers(repo, pr, pull)

    # use the pull.head.sha here instead of the pr.head_commit_sha
    # as the head commit on the PR might be stale
    # we want to identify changed commit_sha in process_fetched
    ensure_branch_commit(repo, pr.branch_name, pull.head.sha, pr_id=pr.id)

    # Doing this after the commit so that the stack is correctly updated.
    _ensure_stacks_target_branch(pr)

    return git_user, pr


def _ensure_stacks_target_branch(source_pr: PullRequest) -> None:
    """
    Find the full stack of the PRs, and update target branch everywhere.
    """
    original_target_branch = source_pr.target_branch_name
    all_ancestors = stack_manager.get_all_stack_ancestors(source_pr)

    # Root PR should have the correct target branch.
    target_branch = (
        all_ancestors[-1].base_branch_name if all_ancestors else original_target_branch
    )

    if original_target_branch != target_branch:
        logger.info(
            "Found different target branches for repo",
            repo_id=source_pr.repo_id,
            pr_number=source_pr.number,
            original_target_branch=original_target_branch,
            new_target_branch=target_branch,
        )
        for pr in all_ancestors:
            pr.target_branch_name = target_branch
        db.session.commit()

    # If the target branch was indeed changed, we should also look at
    # child PRs.
    for pr in stack_manager.get_stack_descendants(source_pr, []):
        pr.target_branch_name = target_branch
    db.session.commit()


def ensure_branch_commit(
    repo: GithubRepo,
    branch_name: str,
    commit_sha: str,
    pr_id: int | None = None,
) -> None:
    commit = ensure_commit(repo.id, commit_sha)
    with locks.lock(
        "create-commit",
        f"create-branch-commit-{repo.id}-{branch_name}-{commit_sha}",
        expire=10,
    ):
        mapping: BranchCommitMapping | None = BranchCommitMapping.query.filter_by(
            git_commit_id=commit.id,
            branch_name=branch_name,
        ).first()
        if not mapping:
            if not pr_id:
                pr = get_pr_by_branch(repo, branch_name)
                pr_id = pr.id if pr else None
            mapping = BranchCommitMapping(
                git_commit_id=commit.id, pull_request_id=pr_id, branch_name=branch_name
            )
            db.session.add(mapping)
            db.session.commit()


class AvPRMetadata(pydantic.BaseModel):
    parent: str
    parent_head: str | None = pydantic.Field(None, alias="parentHead")
    parent_pull: int | None = pydantic.Field(None, alias="parentPull")
    trunk: str


def _parse_av_pr_metadata(
    pull_body: str | None,
) -> AvPRMetadata | None:
    """
    Parse metadata from a PR body.

    This metadata is added to PRs created with `av`.
    """
    if not pull_body:
        # `pull.body` can apparently be None if there's no text in the body
        return None

    # The metadata comment is added to the end of the PR body as implemented in
    # the `AddPRMetadata` function in
    # https://github.com/aviator-co/av/blob/master/internal/actions/pr.go.
    # TL;DR: The comment starts with a "<!-- av pr metadata" line, then has some
    # lines of help text (e.g., saying don't delete the comment), then embeds
    # the metadata as a JSON object inside of a markdown code fence:
    #     <!-- av pr metadata
    #     Please don't delete this comment! Oh no! ...
    #     ```
    #     {"parent": "master", "parentHead": "abc123", "parentPull": 123}
    #     ```
    #     -->
    found_open_comment = False
    found_code_fence = False
    meta: AvPRMetadata | None = None
    for line in pull_body.splitlines():
        if line.startswith("<!-- av pr metadata"):
            found_open_comment = True
        elif found_open_comment and line.startswith("```"):
            found_code_fence = True
        elif found_open_comment and found_code_fence:
            # This assumes the JSON is all on one line.
            # Note: we don't return/break here because we want to parse the
            # very last metadata section within the PR body.
            try:
                meta = AvPRMetadata.model_validate_json(line)
            except Exception as exc:
                meta = None
                # TODO: should show a better warning to the user here
                logger.warning(
                    "Failed to parse av pr metadata for pull request",
                    exc_info=exc,
                )
            finally:
                found_open_comment = False
                found_code_fence = False
    return meta


def _find_stack_parent(
    repo: GithubRepo,
    pr: PullRequest,
    pull: _GHPullRequestData,
) -> PullRequest | None:
    """
    Get the parent pull request if any.
    """
    meta = _parse_av_pr_metadata(pull.body)

    # if there is no metadata present or if we don't have a parent_pull
    # (i.e. we are probably at the 'first' PR) then we should return no parent
    if not meta or not meta.parent_pull:
        return None

    stack_parent_pr: PullRequest | None = PullRequest.query.filter_by(
        repo_id=repo.id, number=meta.parent_pull
    ).first()
    if not stack_parent_pr:
        # TODO: show better user-facing warnings here
        logger.warning(
            "Pull request stack parent does not exist",
            repo_id=repo.id,
            pr_id=pr.id,
            pr_number=pr.number,
            parent_pr_number=meta.parent_pull,
        )
        return None
    return stack_parent_pr


def get_pr_by_branch(repo: GithubRepo, branch: str) -> PullRequest | None:
    """
    Get the PR that is associated with the given branch.
    """
    pr: PullRequest | None = PullRequest.query.filter_by(
        repo_id=repo.id, branch_name=branch
    ).first()
    return pr


def get_pr_by_number(repo: GithubRepo, number: int) -> PullRequest | None:
    """
    Get the PR that is associated with the given number.
    """
    pr: PullRequest | None = PullRequest.query.filter_by(
        repo_id=repo.id, number=number
    ).first()
    return pr


def get_top_pr(repo_id: int, branch_name: str) -> PullRequest | None:
    prs: list[PullRequest] = (
        PullRequest.query.filter_by(repo_id=repo_id, target_branch_name=branch_name)
        .filter(PullRequest.status.in_(["queued", "tagged"]))
        .order_by(PullRequest.skip_line.desc(), PullRequest.queued_at.asc())
        .all()
    )
    if not prs:
        return None
    for pr in prs:
        if stack_manager.is_in_stack(pr) and not stack_manager.is_stack_queued(pr):
            logger.info(
                "Found stacked pull request that is marked as queued, but stack is not queued",
                repo_id=repo_id,
                pr_id=pr.id,
                pr_number=pr.number,
            )
            # if the entire stack is not queued, that means the stack
            # is not ready to be merged, so we should skip this PR
            # and move to the next one.
            continue
        return pr
    return None


def get_pull_from_payload(
    client: GithubClient,
    payload: dict,
    timestamp: int | None,
) -> pygithub.PullRequest | None:
    """
    This is a hack to generate pull object. Although this works for most part,
    it will fail for any direct API requests made using the returned object as
    the token and requester object are empty.
    """
    try:
        if timestamp and (
            datetime.datetime.now(datetime.UTC)
            - datetime.datetime.fromtimestamp(timestamp, datetime.UTC)
            > datetime.timedelta(seconds=10)
        ):
            logger.info(
                "Pull request payload is stale, fetching latest from GitHub API"
            )
            return None
        return pygithub.PullRequest(
            client.repo._requester,
            {},
            payload,
            completed=True,
        )
    except Exception as exc:
        logger.error(
            "Could not unmarshal pygithub.PullRequest object from webhook payload",
            exc_info=exc,
        )
    return None


def get_top_bot_prs(repo: GithubRepo, target_branch_name: str) -> list[BotPr]:
    bot_prs: list[BotPr] = db.session.scalars(
        sa.select(BotPr)
        .where(BotPr.status == "queued", BotPr.repo_id == repo.id)
        .join(PullRequest)
        .filter(PullRequest.target_branch_name == target_branch_name)
        .order_by(BotPr.id.asc())
    ).all()
    return bot_prs


def get_all_tagged(repo_id: int, target_branch: str) -> list[PullRequest]:
    return (
        PullRequest.query.filter_by(status="tagged", repo_id=repo_id)
        .filter(PullRequest.target_branch_name == target_branch)
        .order_by(PullRequest.skip_line.desc(), PullRequest.queued_at.asc())
        .all()
    )


def get_emergency_merge_prs(repo_id: int, target_branch_name: str) -> list[PullRequest]:
    prs: Iterable[PullRequest] = (
        PullRequest.query.filter_by(repo_id=repo_id)
        .filter(PullRequest.target_branch_name == target_branch_name)
        .filter(PullRequest.status.notin_(["merged", "blocked"]))
        .join(MergeOperation)
        .filter(MergeOperation.emergency_merge)
        .order_by(PullRequest.id.asc())
        .all()
    )
    return [pr for pr in prs if pr.emergency_merge]


def is_network_issue(e: Exception) -> bool:
    return (
        isinstance(e, requests.exceptions.ReadTimeout)
        or isinstance(e, requests.exceptions.ConnectionError)
        or (
            isinstance(e, requests.exceptions.HTTPError)
            and e.response
            and e.response.status_code >= 500
        )
        or (isinstance(e, pygithub.GithubException) and e.status >= 500)
    )


def is_github_gone_crazy(e: Exception) -> bool:
    return (isinstance(e, pygithub.GithubException) and e.status == 500) or isinstance(
        e, subprocess.CalledProcessError
    )


def is_merge_conflict(e: Exception) -> bool:
    return (isinstance(e, pygithub.GithubException) and e.status == 409) or (
        isinstance(e, errors.PRStatusException) and e.code == StatusCode.MERGE_CONFLICT
    )


def is_pr_ready(pr: PullRequest) -> bool:
    mo = pr.merge_operations
    return bool(mo and mo.ready)


def ensure_merge_operation(
    pr: PullRequest,
    *,
    ready: bool,
    ready_source: ReadySource,
    ready_user_id: int | None = None,
    ready_gh_user_id: int | None = None,
) -> MergeOperation:
    mo = pr.merge_operations
    was_previously_ready = mo.ready if mo else False
    logger.info(
        "Ensuring MergeOperation for PullRequest",
        ready=ready,
        has_existing=mo is not None,
        was_previously_ready=was_previously_ready,
        repo_id=pr.repo_id,
        pr_number=pr.number,
        ready_source=ready_source,
        ready_user_id=ready_user_id,
        ready_gh_user_id=ready_gh_user_id,
    )
    if not mo:
        mo = MergeOperation(
            pull_request_id=pr.id,
            ready=ready,
            ready_source=ready_source,
            ready_actor_github_user_id=ready_gh_user_id,
            ready_actor_user_id=ready_user_id,
        )
        db.session.add(mo)
    else:
        mo.ready = ready
    if ready and not was_previously_ready:
        mo.ready_source = ready_source
        mo.ready_actor_user_id = ready_user_id
        mo.ready_actor_github_user_id = ready_gh_user_id

        # TEMPORARY (,,, hopefully) HACK
        # We need to reset the MergeOperation here.
        # In the near future we plan to move to a more robust FSM-like way to
        # manage MergeOperations, but for now, since we end up editing MO's in
        # place instead of creating new ones, if we end up moving from
        # "not ready" back to "ready" we want to run the ready hook again and so
        # need to set this value.
        with mo.bound_contextvars():
            logger.info("Resetting MergeOperation")
            mo.ready_hook_state = ReadyHookState.PENDING
            mo.transient_error_retry_count = 0
            mo.is_bisected = False
            mo.bisected_batch_id = None
            mo.times_bisected = 0
    db.session.commit()
    return mo


def has_merge_operation(pr: PullRequest) -> bool:
    return pr.merge_operations.ready if pr.merge_operations else False


def set_pr_blocked(
    pr: PullRequest, status_code: StatusCode, status_reason: str | None = None
) -> None:
    pr.set_status("blocked", status_code, status_reason)
    ensure_merge_operation(pr, ready=False, ready_source=ReadySource.UNKNOWN)
    core.attention_set.transition_attention(
        pr,
        pr.user_subscriptions,
        core.attention_set.AttentionTrigger.PR_BLOCKED,
        None,
        None,
    )
    # Notifications should be sent by the caller.


def skip_line_reason(pr: PullRequest) -> str | None:
    return pr.merge_operations.skip_line_reason if pr.merge_operations else None


def set_skip_line_reason(pr: PullRequest, reason: str) -> None:
    logger.info(
        "Setting PullRequest skip-line reason",
        repo_id=pr.repo_id,
        pr_id=pr.id,
        pr_number=pr.number,
        skip_line_reason=reason,
    )
    mo = pr.merge_operations
    if not mo:
        mo = MergeOperation(pull_request_id=pr.id, skip_line_reason=reason)
        db.session.add(mo)
    else:
        mo.skip_line_reason = reason
    db.session.commit()


def set_skip_validation(pr: PullRequest) -> None:
    logger.info(
        "Setting PullRequest skip_validation",
        repo_id=pr.repo_id,
        pr_id=pr.id,
        pr_number=pr.number,
    )
    mo = pr.merge_operations
    if not mo:
        mo = MergeOperation(pull_request_id=pr.id, skip_validation=True)
        db.session.add(mo)
    else:
        mo.skip_validation = True
    db.session.commit()


def is_av_custom_check(check_name: str) -> bool:
    return check_name == CHECK_RUN_NAME


def is_av_flaky_check(check_name: str) -> bool:
    return check_name.startswith(app.config["FLAKY_CHECK_NAME_PREFIX"])


def get_gh_pr_from_sha(
    commit_sha: str, repo: GithubRepo
) -> tuple[pygithub.PullRequest | None, GithubClient]:
    pr = None
    _, client = get_client(repo)
    commit = client.repo.get_commit(sha=commit_sha)
    # For some reason, `Commit.get_pulls` is missing from the type stub files
    # that are published with pygithub. :shrug:
    pulls: Iterable[pygithub.PullRequest] = commit.get_pulls()
    for pull in pulls:
        if commit_sha == pull.head.sha:
            pr = pull
            break
    return pr, client


def get_repo_and_pr(
    account_id: int, repo_name: str, pr_number: int
) -> tuple[GithubRepo, PullRequest | None]:
    repo: GithubRepo | None = GithubRepo.query.filter_by(
        account_id=account_id, name=repo_name, active=True
    ).first()
    if not repo:
        raise errors.InvalidValueException("Repository not found")

    return repo, PullRequest.query.filter_by(repo_id=repo.id, number=pr_number).first()


def delete_ref_if_applicable(client: GithubClient, repo: GithubRepo, ref: str) -> None:
    if repo.delete_branch:
        prs_count = (
            PullRequest.query.filter_by(
                repo_id=repo.id,
                base_branch_name=ref,
            )
            .filter(PullRequest.status.in_(["open", "queued", "tagged", "blocked"]))
            .count()
        )
        if prs_count == 0:
            # We should only delete the branch (pull.head.ref) if there are
            #   no other open PRs that are based off pull.head.ref.
            client.delete_ref(ref)


def pause_repo(actor: User | None, repo: GithubRepo, *, paused: bool) -> set[str]:
    """
    A unified method to pause the repo that captures the event state and notifies the subscribers.
    Also see: update_pause_unpause_for_branches
    """
    repo.enabled = not paused
    base_branches: Iterable[BaseBranch] = BaseBranch.query.filter_by(
        repo_id=repo.id
    ).all()
    for branch in base_branches:
        branch.paused = paused
    logger.info(
        "Updated pause state for all repository base branches",
        repo_id=repo.id,
        branches=[b.name for b in base_branches],
        paused=paused,
    )
    _update_paused_activity(actor, repo, paused)
    db.session.commit()
    notify.notify_queue_paused.delay(repo.id, paused)
    return {bb.name for bb in base_branches}


def update_pause_unpause_for_branches(
    actor: User | None,
    repo: GithubRepo,
    repo_enabled: bool | None,
    paused_branches: list[str],
    unpaused_branches: list[str],
    paused_message: str | None,
) -> tuple[set[str], set[str]]:
    if repo_enabled is not None:
        logger.info(
            "Updating pause/unpause state for repo",
            repo_id=repo.id,
            repo_paused=repo_enabled,
        )
        if repo_enabled != repo.enabled:
            repo.enabled = repo_enabled
            _update_paused_activity(actor, repo, not repo_enabled)
            db.session.commit()
            notify.notify_queue_paused.delay(repo.id, not repo_enabled)

    branch_names = set(paused_branches + unpaused_branches)
    base_branches: list[BaseBranch] = db.session.scalars(
        sa.select(BaseBranch).where(
            BaseBranch.repo_id == repo.id,
            BaseBranch.name == sa.func.any(list(branch_names)),
            BaseBranch.deleted == False,
        )
    ).all()
    base_branch_dict = {bb.name: bb for bb in base_branches}
    logger.info(
        "Updating pause/unpause state for branches",
        repo_id=repo.id,
        paused_branches=paused_branches,
        unpaused_branches=unpaused_branches,
        reason=paused_message,
    )
    newly_paused_branches = set()
    newly_unpaused_branches = set()
    for branch in paused_branches:
        if branch not in base_branch_dict:
            base_branch = BaseBranch(
                repo_id=repo.id,
                name=branch,
            )
            base_branch_dict[branch] = base_branch
            db.session.add(base_branch)
        base_branch = base_branch_dict[branch]
        if not base_branch.paused:
            newly_paused_branches.add(branch)
            base_branch.paused = True
            base_branch.paused_message = paused_message
            _update_paused_activity(actor, repo, True, branch)

    for branch in unpaused_branches:
        if branch not in base_branch_dict:
            base_branch = BaseBranch(
                repo_id=repo.id,
                name=branch,
            )
            base_branch_dict[branch] = base_branch
            db.session.add(base_branch)
        base_branch = base_branch_dict[branch]
        if base_branch.paused:
            newly_unpaused_branches.add(branch)
            base_branch.paused = False
            base_branch.paused_message = None
            _update_paused_activity(actor, repo, False, branch)

    db.session.commit()
    logger.info(
        "Updated pause/unpause state for branches",
        repo_id=repo.id,
        newly_paused_branches=list(newly_paused_branches),
        newly_unpaused_branches=list(newly_unpaused_branches),
    )
    if newly_paused_branches:
        notify.notify_queue_paused.delay(
            repo.id, True, ", ".join(newly_paused_branches), paused_message
        )
    if newly_unpaused_branches:
        notify.notify_queue_paused.delay(
            repo.id, False, ", ".join(newly_unpaused_branches)
        )
    return newly_paused_branches, newly_unpaused_branches


def _update_paused_activity(
    actor: User | None, repo: GithubRepo, paused: bool, base_branch: str | None = None
) -> None:
    activity = Activity(
        repo_id=repo.id,
        name=ActivityType.PAUSED if paused else ActivityType.UNPAUSED,
        base_branch=base_branch,
    )
    if actor:
        action = AuditLogAction.QUEUE_PAUSED if paused else AuditLogAction.QUEUE_RESUMED
        target = f"{repo.name}/{base_branch}" if base_branch else repo.name
        AuditLog.capture_user_action(
            user=actor,
            action=action,
            entity=AuditLogEntity.MERGE_QUEUE,
            target=target,
            commit_to_db=False,
        )
    db.session.add(activity)


def get_paused_queue_depth(
    repo: GithubRepo, paused_base_branches: list[BaseBranch]
) -> int:
    base_branch_names = [bb.name for bb in paused_base_branches]
    tagged_prs: Iterable[PullRequest] = (
        PullRequest.query.filter_by(repo_id=repo.id)
        .filter(PullRequest.status.in_(["tagged"]))
        .all()
    )
    paused_pr_count: int = 0
    for pr in tagged_prs:
        # This should only be the case for exceptionally old data
        if not pr.target_branch_name:
            continue
        if any(fnmatch.fnmatch(pr.target_branch_name, bb) for bb in base_branch_names):
            paused_pr_count += 1

    return paused_pr_count


def str_to_test_status(raw_status: str | None) -> TestStatus:
    try:
        return TestStatus(raw_status) if raw_status else TestStatus.Pending
    except Exception as exc:
        logger.error(
            "Got invalid/unknown test status",
            test_status=raw_status,
            exc_info=exc,
        )
        return TestStatus.Unknown


def get_pilot_data(pr: PullRequest, action: str) -> dict[str, Any]:
    owner, name = pr.repo.name.split("/")
    repo_name = name if name else pr.repo.name
    user: SchemaUser = SchemaUser(login=pr.creator.username)
    pull_request: SchemaPullRequest = SchemaPullRequest(
        number=pr.number,
        url=pr.url,
        state=pr.status,
        labels=[],
        user=user,
        skip_line=pr.skip_line,
        skip_line_reason=skip_line_reason(pr),
        head=PullRequestRef(
            ref=pr.branch_name or "",
            sha=pr.head_commit_sha or "",
        ),
        base=PullRequestRef(
            ref=pr.base_branch_name or "",
            # TODO: we can't provide this here,,, probably indicates that there's
            #   a mismatch between Pilot events triggered by GitHub webhooks and
            #   by Aviator's own internal events and that we can't easily share
            #   models between them.
            sha="",
        ),
    )
    repository: SchemaRepository = SchemaRepository(
        full_name=pr.repo.name,
        name=repo_name,
        private=True,
    )

    return dict(
        pull_request=pull_request.tojs_dict(),
        repository=repository.tojs_dict(),
        action=action,
    )


def block_pr_until_merged(pr: PullRequest, target_pr: PullRequest) -> None:
    existing_block: PullRequestQueuePrecondition | None = db.session.scalars(
        sa.select(PullRequestQueuePrecondition).where(
            PullRequestQueuePrecondition.pull_request_id == pr.id,
            PullRequestQueuePrecondition.blocking_pull_request_id == target_pr.id,
        )
    ).first()
    if existing_block:
        logger.info(
            "PR already blocked until merged",
            repo_id=pr.repo_id,
            pr_number=pr.number,
            target_pr=target_pr.number,
        )
        return

    block = PullRequestQueuePrecondition(
        pull_request_id=pr.id, blocking_pull_request_id=target_pr.id
    )
    db.session.add(block)
    db.session.commit()
    logger.info(
        "Blocking PR until merged",
        repo_id=pr.repo_id,
        pr_number=pr.number,
        target_pr=target_pr.number,
    )


def cancel_blocking_prs(pr: PullRequest) -> None:
    existing_blocks: list[PullRequestQueuePrecondition] = db.session.scalars(
        sa.select(PullRequestQueuePrecondition).where(
            PullRequestQueuePrecondition.pull_request_id == pr.id,
        )
    ).all()
    blocking_prs: list[PullRequest] = []
    for block in existing_blocks:
        blocking_prs.append(block.blocking_pull_request)
        db.session.delete(block)
    db.session.commit()
    logger.info(
        "Cleared blocking status for PRs",
        repo_id=pr.repo_id,
        pr_id=pr.id,
        pr_number=pr.number,
        blocked_prs=[p.number for p in blocking_prs],
    )


def get_pending_preconditions(pr: PullRequest) -> list[PullRequest]:
    """
    Check if the preconditions are satisfied for the given PR.
    """
    return db.session.scalars(
        sa.select(PullRequest)
        .join(
            PullRequestQueuePrecondition,
            PullRequestQueuePrecondition.blocking_pull_request_id == PullRequest.id,
        )
        .where(
            PullRequestQueuePrecondition.pull_request_id == pr.id,
            PullRequest.status != "merged",
        )
    ).all()
