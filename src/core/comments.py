from __future__ import annotations

import hashlib
import textwrap

import structlog
import structlog.contextvars
import typing_extensions as TE

from core import common, graphql, locks, messages
from core.client import GithubClient
from core.models import Activity, ActivityType, ChangeSetConfig, GithubRepo, PullRequest
from core.status_codes import StatusCode
from main import app, celery, db, redis_client
from util import time_util

logger = structlog.stdlib.get_logger()


PostPullCommentStatus = TE.Literal[
    "change_set",
    "queued",
    "blocked",
    "top",
    "stuck",
    "merged",
]


@celery.task
def post_pull_comment(
    pr_id: int,
    status: PostPullCommentStatus,
    ci_map: dict[str, str] | None = None,
    note: str | None = None,
) -> None:
    """
    Schedule a comment to be posted to the pull request on GitHub.

    This is useful for surfacing messages to users about the status of their
    pull request (as seen by Aviator).

    :param pr_id: The id of the PullRequest (note: this corresponds to the PR in
        Aviator's database, NOT the GitHub PR number).
    :param status:
    :param ci_map: Map of impacted CI to its corresponding URL
    :param note: Arbitrary string passed to provide additional debug information in comments.
    :return: None
    """
    pr: PullRequest = PullRequest.get_by_id_x(pr_id)
    repo = pr.repo
    ci_map = ci_map or {}

    # HACK HACK HACK HACK HACK
    # We always need to create the queued
    if status == "queued":
        # Note that we may have duplicate queued Activity objects for batch PRs.
        # Since we only mark the first PR in a batch as `top`,
        # we will get a duplicated `queued` Activity if other PRs in the batch are requeued.
        activity = Activity(
            repo_id=repo.id,
            name=ActivityType.QUEUED,
            pull_request_id=pr_id,
            status_code=pr.status_code,
        )
        db.session.add(activity)
        db.session.commit()

    if not _should_post_comment(repo.id, pr.number, status):
        return

    # capture status as activity if applicable.
    # HACK: see above
    if not status == "queued" and not status == "merged":
        if status == "change_set":
            activity_type = ActivityType.CHANGE_SET
        elif status == "blocked":
            activity_type = ActivityType.BLOCKED
        elif status == "top":
            activity_type = ActivityType.TOP
        elif status == "stuck":
            activity_type = ActivityType.STUCK
        else:
            TE.assert_never(status)
        activity = Activity(
            repo_id=repo.id,
            name=activity_type,
            pull_request_id=pr_id,
            status_code=pr.status_code,
        )
        db.session.add(activity)
        db.session.commit()

    if not repo.enable_comments:
        logger.info(
            "Repository %d PR %d skipping comment as disabled %s",
            repo.id,
            pr.number,
            status,
        )
        return

    logger.info(
        "Repository %d PR %d adding comment for status %s", repo.id, pr.number, status
    )
    issue_comment = None
    if status == "queued":
        # We no longer post comments for queued (this is surfaced in the sticky
        # comment). We still call post_pull_comment because of the hack above.
        return
    elif status == "blocked":
        status_code = StatusCode(pr.status_code)
        issue_comment = (
            f"This pull request failed to merge: {status_code.message()}. "
            f"After you have resolved the problem, you should remove the `{repo.blocked_label}` "
            f"pull request label from this PR and then try to re-queue the PR. "
            f"Note that the pull request will be automatically "
            f"re-queued if it has the `{repo.queue_label}` label."
        )
        if ci_map:
            issue_comment += "\n\nFailed checks: " + ", ".join(
                [ci_format(ci, url) for (ci, url) in ci_map.items()]
            )
        if (
            repo.parallel_mode
            and pr.latest_bot_pr
            and pr.latest_bot_pr.number != pr.number
        ):
            # only add associated PR info if draft PR is different from original PR.
            issue_comment += f"\n\nAssociated Draft PR #{pr.latest_bot_pr.number}"
        if note:
            logger.info("Received note for PR %d: %s", pr.number, note)
            issue_comment += f"\n\nAdditional debug info: {note}"
    elif status == "stuck":
        issue_comment = (
            "Although the CI of the associated draft PR has passed, "
            "the CI of this PR itself is still running. This is unexpected. "
        )
        if repo.parallel_mode_config.stuck_pr_timeout_mins:
            issue_comment += (
                "This PR will be dequeued after %d mins. Please check the CI status of this PR."
                % repo.parallel_mode_config.stuck_pr_timeout_mins
            )
        else:
            issue_comment += (
                "\nThis PR will be dequeued shortly. Please set `stuck_pr_timeout_mins` "
                "to wait before dequeuing. Read more: "
                "https://docs.aviator.co/reference/configuration-file#parallel-mode"
            )
        if ci_map:
            issue_comment += "\n\nStuck CI(s) in this PR: " + ", ".join(
                [ci_format(ci, url) for (ci, url) in ci_map.items()]
            )
    elif status == "change_set" and note:
        issue_comment = (
            "PR has been added to the changeset: "
            + note
            + ": "
            + app.config["BASE_URL"]
            + "/changeset/"
            + note
        )
    elif status == "merged" and repo.parallel_mode_config.use_fast_forwarding and note:
        issue_comment = f"The changes in this PR have been merged with #{note}"

    if issue_comment:
        _, client = common.get_client(repo)
        client.create_issue_comment(pr.number, issue_comment)
    logger.info("Repository %d PR %d comments %s", repo.id, pr.number, status)


def ci_format(ci_name: str, ci_url: str) -> str:
    if len(ci_url) > 0:
        return f"[{ci_name}]({ci_url})"
    return "`" + ci_name + "`"


@celery.task
def add_bot_pr_close_comment(
    repo_id: int,
    bot_pr_number: int,
    comment: str,
) -> None:
    """
    Post a comment on the bot PR describing the reason for close.
    """
    if not _should_post_comment(repo_id, bot_pr_number, "closed"):
        logger.info(
            "Skipping bot PR comment, not should_post_comment.",
            repo_id=repo_id,
            bot_pr_number=bot_pr_number,
        )
        return

    logger.info(
        "Posting BotPR close comment.",
        repo_id=repo_id,
        bot_pr_number=bot_pr_number,
        comment=comment,
    )

    repo = GithubRepo.get_by_id_x(repo_id)
    _, client = common.get_client(repo)
    client.create_issue_comment(bot_pr_number, comment)


@celery.task
def add_optimistic_comment(
    repo_id: int, bot_pr_number: int, optimistic_check_pr: int
) -> None:
    """
    Post a comment on the bot PR to indicate that we are going use optimistic
    validation to merge the original PR.
    """
    if not _should_post_comment(repo_id, bot_pr_number, "optimistic"):
        logger.info(
            "Skipping optimistic comment posting, as already posted %d %d",
            repo_id,
            bot_pr_number,
        )
        return
    repo = GithubRepo.get_by_id_x(repo_id)
    comment = f"Merging using optimistic validation - PR #{optimistic_check_pr} has passing CI."
    _, client = common.get_client(repo)
    client.create_issue_comment(bot_pr_number, comment)


@celery.task
def post_parallel_mode_same_pr_comment(pr_id: int) -> None:
    pr = PullRequest.get_by_id_x(pr_id)
    repo = pr.repo
    log = logger.bind(repo_id=repo.id, pr_id=pr.id, pr_number=pr.number)
    if not repo.enable_comments:
        log.info("Skipping same pull comment as repo has comments disabled")
        return

    _, client = common.get_client(repo)
    client.create_issue_comment(
        pr.number,
        (
            "Skipping bot pull request creation because the queue is empty and "
            f"this pull request is up to date with `` {pr.target_branch_name} ``."
        ),
    )
    log.info("Posted same pull comment")


def _comment_redis_key(repo_id: int, pr_number: int) -> str:
    return "comment-%d-%d" % (repo_id, pr_number)


def _should_post_comment(repo_id: int, pr_number: int, status: str) -> bool:
    key = _comment_redis_key(repo_id, pr_number)
    existing_status = redis_client.get(key)
    # nothing changed so don't post a comment
    if existing_status and existing_status.decode("utf-8") == status:
        return False
    redis_client.set(key, status, ex=time_util.ONE_WEEK_SECONDS)
    return True


@celery.task
def add_inactive_user_comment(repo_id: int, pr_id: int, git_user_id: int) -> None:
    repo = GithubRepo.get_by_id_x(repo_id)
    pr = PullRequest.get_by_id_x(pr_id)
    logger.info(
        "Repo %d PR %d adding inactive user comment for user %d",
        repo_id,
        pr_id,
        git_user_id,
    )
    if _should_post_comment(repo.id, pr.number, "inactive"):
        max_free_users = app.config["MAX_FREE_USERS"]
        comment = (
            f"Free usage limit reached: {max_free_users} users this month."
            "To add more users, upgrade to the Pro plan."
        )
        _, client = common.get_client(repo)
        client.create_issue_comment(pr.number, comment)


@celery.task
def new_pr_changeset(pr_id: int) -> None:
    pr = PullRequest.get_by_id_x(pr_id)
    config = ChangeSetConfig.for_account(pr.account_id)
    if not config or not config.enable_comments:
        return
    dashboard_url = app.config["BASE_URL"] + "/changeset"
    issue_comment_str = (
        "Aviator Changeset actions:\n"
        "- [View Changeset dashboard](%s) - Open Changeset Dashboard on Aviator\n"
    )
    issue_comment = issue_comment_str % (dashboard_url)
    _, client = common.get_client(pr.repo)
    client.create_issue_comment(pr.number, issue_comment)
    logger.info("Repository %d PR %d changeset comment added", pr.repo_id, pr.number)


@celery.task(
    autoretry_for=(Exception,),
    retry_backoff=10,
    retry_kwargs={"max_retries": 5},
    retry_jitter=True,
)
def update_pr_sticky_comment(pr_id: int) -> None:
    """
    Update the sticky comment associated with a pull request.
    """
    pr = PullRequest.get_by_id_x(pr_id)
    try:
        with locks.lock("sticky-comment", f"pr/{pr_id}/sticky_comment", expire=20):
            # Refresh the PR to see any changes that were caused by someone else
            # holding the sticky-comment lock.
            db.session.refresh(pr)
            _update_pr_sticky_comment_sync(pr)
    except Exception as e:
        # Sometimes we get weird errors here -- log them for better debugability
        logger.info("Error while updating PR sticky comment", pr_id=pr.id, exc_info=e)
        raise


def _update_pr_sticky_comment_sync(pr: PullRequest) -> None:
    if not _should_post_sticky_comment(pr.repo, pr):
        return

    body = _sticky_comment_body(pr.repo, pr)
    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    # Optimization: only update the comment when text has actually changed
    key = _sticky_comment_redis_key(pr.repo_id, pr.number)
    existing_hash = redis_client.get(key)
    if existing_hash and existing_hash.decode("utf-8") == body_hash:
        logger.info(
            "Repo %d PR %d sticky comment unchanged (not updating on GitHub)",
            pr.repo_id,
            pr.number,
        )
        return

    # NOTE: Avoid API requests above the "if not changed" check above.
    _, client = common.get_client(pr.repo)
    pr.gh_sticky_comment_id = set_sticky_comment(
        client, pr.repo, pr.number, pr.gh_sticky_comment_id, body
    )
    # only update the cache after we have actually updated the sticky comment
    redis_client.set(key, body_hash, ex=time_util.ONE_WEEK_SECONDS)
    db.session.commit()


def _should_post_sticky_comment(repo: GithubRepo, pr: PullRequest) -> bool:
    if not repo.active or not repo.enable_comments:
        return False

    # Always post if we've already posted the comment or if the PR is in a
    # stack.
    if pr.gh_sticky_comment_id or pr.stack_parent_id or pr.stack_dependents:
        return True

    # If the PR is targeting an unsupported base branch, do not post the stick comment.
    if not repo.is_valid_base_branch(pr.target_branch_name):
        return False

    publish_config = repo.current_config.merge_rules.status_comment.publish
    if publish_config == "always":
        return True
    elif publish_config == "never":
        return False
    elif publish_config == "ready":
        return pr.status_code != StatusCode.IN_DRAFT
    elif publish_config == "queued":
        return pr.status in ("queued", "tagged", "blocked", "delayed", "pending")
    else:
        TE.assert_never(publish_config)


def _sticky_comment_redis_key(repo_id: int, pr_number: int) -> str:
    return "pr-%d-%d-sticky-comment" % (repo_id, pr_number)


def _sticky_comment_body(repo: GithubRepo, pr: PullRequest) -> str:
    body = (
        "# Current Aviator status\n"
        "> Aviator will automatically update this comment as the status of the PR changes.\n"
        "> Comment `/aviator refresh` to force Aviator to re-examine your PR "
        "(or [learn about other `/aviator` commands](https://docs.aviator.co/reference/slash-commands)).\n"
        "\n\n"
    )
    body += messages.get_message_body(repo, pr)

    if not app.config["PROMOTE_AVIATOR_APP"] and not app.config["PROMOTE_CHROME_EXT"]:
        return body

    body += "\n\n---\n"

    if app.config["PROMOTE_AVIATOR_APP"]:
        app_url = f"{app.config['BASE_URL']}/repo/{repo.name}/pull/{pr.number}"
        body += textwrap.dedent(
            f"""
            <div id="aviator-app-message">
              See the real-time status of this PR on the
              <a class="promote-aviator-app" href="{app_url}" target="_blank">Aviator webapp</a>.
            </div>
            """
        ).strip()
        body += "\n"

    if app.config["PROMOTE_CHROME_EXT"]:
        chrome_ext_url = f"https://chrome.google.com/webstore/detail/{app.config['CHROME_EXT_APP_ID']}"
        body += textwrap.dedent(
            f"""
            <div id="chrome-ext-message">
              Use the <a href="{chrome_ext_url}" target='_blank'>Aviator Chrome Extension</a>
              to see the status of your PR within GitHub.
            </div>
            """
        )
        body += "\n"
    return body.strip()


def set_sticky_comment(
    client: GithubClient,
    repo: GithubRepo,
    pr_number: int,
    comment_id: str | None,
    body: str,
) -> str:
    """
    Create or update a sticky comment.

    Always returns the id of the comment that was created or edited (which
    should be persisted in the database). It's possible for this not to match
    the comment_id passed in (e.g., if the comment was deleted in GitHub).
    """
    with structlog.contextvars.bound_contextvars(repo_id=repo.id, pr_number=pr_number):
        if not comment_id:
            logger.info("No sticky comment ID. Creating a new one")
            comment = client.create_issue_comment(pr_number, body)
            return str(comment.node_id)logger.info("Updating sticky comment in place", comment_id=comment_id)
        res = client.gql_client.fetch(
            """
            mutation updateIssueComment($input: UpdateIssueCommentInput!) {
              updateIssueComment(input: $input) {
                issueComment {
                  id
                }
              }
            }
            """,
            input={
                "id": comment_id,
                "body": body,
            },
        )
        try:
            res.raise_for_error()
            data = res.must_response_data()
        except graphql.GithubGqlNotFoundException:
            # this error is what we would get if sticky comment was deleted
            # so remake the sticky comment directly
            logger.info("No sticky comment found on PR", exc=res.errors)
            return set_sticky_comment(client, repo, pr_number, None, body)
        except graphql.GithubGqlNoDataException as exc:
            # this should not happen after already checking errors but let's double-check
            logger.error(
                "No sticky comment found",
                exc_info=exc,
            )
            raise
        return str(data["updateIssueComment"]["issueComment"]["id"])


def get_sticky_comment_url(client: GithubClient, pr: PullRequest) -> str:
    res = client.gql_client.fetch(
        """
        query getIssueComment($id: ID!) {
            node(id: $id) {
                ... on IssueComment {
                    url
                }
            }
        }
        """,
        id=pr.gh_sticky_comment_id,
    )
    try:
        res.raise_for_error()
        data = res.must_response_data()
    except graphql.GithubGqlNotFoundException:
        # this error is what we would get if sticky comment was deleted
        # so remake the sticky comment directly
        logger.info("No sticky comment found on PR", exc=res.errors)
        return ""
    except Exception as exc:
        # this should not happen after already checking errors but let's double-check
        logger.error(
            "Failed to fetch sticky_comment url",
            pr_number=pr.number,
            repo_id=pr.repo_id,
            exc_info=exc,
        )
        return ""
    url: str = data["node"]["url"]
    return url


def upsert_issue_comment_with_cache(
    github_client: GithubClient, pr_number: int, comment: str, node_id: str | None
) -> str:
    """
    Post a comment on the PR, trying to avoid unnecessary API calls.

    This caches the comment content in Redis associated with the comment node ID, so
    that if the content is the same, we don't need to make an API call to update the
    comment.
    """
    if not node_id:
        node_id = github_client.upsert_issue_comment(pr_number, comment, None)
        key = f"comment-{node_id}"
    else:
        key = f"comment-{node_id}"
        existing_comment = redis_client.get(key)
        if not existing_comment or existing_comment.decode("utf-8") != comment:
            # Call API only if the content changes.
            github_client.upsert_issue_comment(pr_number, comment, node_id)
    redis_client.set(key, comment, ex=time_util.ONE_WEEK_SECONDS)
    return node_id