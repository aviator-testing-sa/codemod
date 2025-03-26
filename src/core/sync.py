from __future__ import annotations

import time
from collections.abc import Iterable

import structlog

import core.pygithub as pygithub
import errors
import util.posthog_util
from billing import common as billing_common
from core import comments, common, const, github_user
from core.client import GithubClient
from core.models import GithubRepo, PullRequest
from core.status_codes import StatusCode
from main import celery, db

logger = structlog.stdlib.get_logger()


def schedule_repo_auto_sync(repo: GithubRepo) -> None:
    """
    Asynchronously update all PRs in a repo to be synced with the base branch.
    """
    if not repo.account.billing_active or not repo.active:
        return

    prs: Iterable[PullRequest] = (
        PullRequest.query.filter_by(repo_id=repo.id)
        .filter(PullRequest.status.in_(["open", "pending"]))
        .order_by(PullRequest.queued_at)
        .all()
    )
    if not prs:
        logger.info("auto sync: nothing to process")
        return None

    pr_nos = [pr.number for pr in prs]
    sync_to_base_pr.delay(pr_nos, repo.id)


@celery.task
def sync_to_base_pr(all_prs: list[int], repo_id: int) -> None:
    """
    Update a GitHub PR so that it is in sync with the base branch.

    This will attempt to merge or rebase (depending on the repository
    configuration) each PR so that it contains the most recent commits from the
    base branch.
    """
    repo: GithubRepo = GithubRepo.get_by_id_x(repo_id)
    repo.bind_contextvars()

    access_token, client = common.get_client(repo)
    number = all_prs[0]
    pr: PullRequest = PullRequest.query.filter_by(
        repo_id=repo_id, number=number
    ).first()

    time.sleep(0.5)
    pull = client.get_pull(pr.number)
    if pull.state == "closed":
        if pull.merged:
            util.posthog_util.capture_pull_request_event(
                util.posthog_util.PostHogEvent.MERGE_PULL_REQUEST_NO_MQ,
                pr,
            )
            pr.set_status("merged", StatusCode.MERGED_MANUALLY)
            pr.merged_at = pull.merged_at
            pr.merge_commit_sha = pull.merge_commit_sha
            if pr.merged_at and pr.merge_commit_sha is None:
                logger.error(
                    "Missing merge commit SHA for a merged PR", pr_number=pr.number
                )
        else:
            pr.set_status("merged", StatusCode.PR_CLOSED_MANUALLY)
        db.session.commit()
        logger.info("Repo %d PR %d status code %s", repo.id, pr.number, pr.status_code)
    else:
        auto_sync_label = repo.auto_sync_label
        try:
            # If auto_sync_label is None, we auto sync *all* PRs (no label required)
            if auto_sync_label is None or client.is_auto_sync(pull, auto_sync_label):
                git_user = github_user.ensure(
                    account_id=repo.account_id,
                    login=pull.user.login,
                    gh_type=pull.user.type,
                    gh_node_id=pull.user.node_id,
                    gh_database_id=pull.user.id,
                )
                billing_common.update_git_user(git_user)
                if not git_user.active:
                    comments.add_inactive_user_comment.delay(
                        repo.id, pr.id, git_user.id
                    )
                    return

                client, result = merge_or_rebase_head(repo, client, pull, pull.base.ref)
                if not result:
                    pr.merge_base_count += 1
                    logger.info(
                        "Repo %d PR %d merged %s, will circle back. Count %d",
                        repo.id,
                        pr.number,
                        pull.base.ref,
                        pr.merge_base_count,
                    )

                    if (
                        repo.auto_update.max_runs_for_update
                        and pr.merge_base_count >= repo.auto_update.max_runs_for_update
                        and auto_sync_label
                    ):
                        # reset the label
                        client.remove_label(pull, auto_sync_label.name)
                        pr.merge_base_count = 0
                        logger.info(
                            "Repo %d PR %d auto sync removed.", repo.id, pr.number
                        )
                    db.session.commit()

        except Exception as e:
            if common.is_network_issue(e):
                logger.info(
                    "Repo %d PR %d unable to connect %s", repo.id, pr.number, str(e)
                )
                sync_to_base_pr.with_countdown(20).delay(all_prs, repo_id)
                return

            logger.info(
                "Repo %d PR %d failed to update the PR %s", repo.id, pr.number, e
            )
            if repo.auto_update.max_runs_for_update and auto_sync_label:
                # only use blocked labels if auto-sync uses labels
                status_reason = None
                if isinstance(e, errors.PRStatusException):
                    status_code = e.code
                    status_reason = e.message
                else:
                    status_code = (
                        StatusCode.REBASE_FAILED
                        if repo.use_rebase
                        else StatusCode.MERGE_CONFLICT
                    )
                common.set_pr_blocked(pr, status_code, status_reason)
                db.session.commit()
                client.add_label(pull, repo.blocked_label)
                logger.info(
                    "Repo %d PR %d status code %s", repo.id, pr.number, pr.status_code
                )

    prs_left = all_prs[1:]
    if prs_left:
        sync_to_base_pr.delay(prs_left, repo_id)


def merge_or_rebase_head(
    repo: GithubRepo,
    client: GithubClient,
    pull: pygithub.PullRequest,
    base_branch: str,
) -> tuple[GithubClient, bool]:
    """
    :return: A tuple with client and a bool.
        The bool is True if the PR is already up-to-date or
        does not need updating. Returns False if a new merge-commit or rebase was created.
    """
    if common.has_label(pull, const.SKIP_UPDATE_LABEL):
        logger.info("Repo %d PR %d skipping update to head", repo.id, pull.number)
        return client, True

    if repo.use_rebase and not repo.parallel_mode:
        # Do not use rebase if parallel mode is enabled. Rebase on large repos
        # can take a long time and cause timeouts, and there's no value of doing
        # it in parallel mode since we merge original PRs directly.
        try:
            # Get a new access token so that it doesn't expire.
            # We do this for rebase since it tends to take longer than merge.
            access_token, client = common.get_client(repo, force_access_token=True)
            logger.info("Rebasing repo %d pull request %d", repo.id, pull.number)
            did_update_head = client.rebase_pull(pull, base_branch)
        except Exception as exc:
            logger.info(
                "Failed to rebase onto latest changes for repo %d PR #%d: %s",
                repo.id,
                pull.number,
                exc,
            )
            raise errors.PRStatusException(
                StatusCode.REBASE_FAILED,
                "Failed to rebase this PR onto the latest changes from the "
                "base branch. You will probably need to rebase this PR "
                "manually and resolve conflicts).",
            )
    else:
        try:
            did_update_head = client.merge_head(pull, pull.base.ref)
        except errors.MergeConflictException as exc:
            logger.info(
                "Failed to merge latest changes",
                repo=repo.id,
                pr_number=pull.number,
                exception=exc,
            )
            raise errors.PRStatusException(
                StatusCode.MERGE_CONFLICT,
                # This message is a bit verbose, but we just want to make sure
                # the end user doesn't interpret this as "merge this PR into
                # main manually without using Aviator").
                "Failed to merge changes from the base branch into this PR. "
                "You will probably need to merge the latest changes from the base "
                "branch into the PR branch and manually resolve conflicts.",
            )
        except Exception as exc:
            logger.info(
                "Failed to merge latest changes for repo",
                repo_id=repo.id,
                pr_number=pull.number,
                exc_info=exc,
            )
            if common.is_network_issue(exc):
                return client, False
            if not isinstance(exc, pygithub.GithubException):
                # not a Github error, don't mark as merge conflict
                raise
            if _is_required_check_expected(exc):
                raise errors.PRStatusException(
                    StatusCode.BLOCKED_BY_GITHUB,
                    "Failed to merge changes from the base branch into this PR. "
                    f"This is likely due to protected branch settings on `{pull.head.ref}` branch. "
                    "You can manually update this branch, or configure Aviator to bypass the "
                    "required checks in the branch protection settings.",
                )
            raise
    return client, not did_update_head


def _is_required_check_expected(error: pygithub.GithubException) -> bool:
    if isinstance(error.data, dict):
        note = str(error.data.get("message")).lower()
        protected_branch_checks = [
            "required status check",
            "review is required",
            "waiting on code owner review",
            "protected branch",
        ]
        if any([check in note for check in protected_branch_checks]):
            return True
    return False
