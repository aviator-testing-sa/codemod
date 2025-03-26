from __future__ import annotations

import hashlib

import structlog

from core import common, custom_checks, locks, messages, pygithub
from core.models import PullRequest
from core.status_codes import StatusCode
from main import app, redis_client
from util import time_util

logger = structlog.stdlib.get_logger()


def update_pr_check(pr: PullRequest, status: str = "") -> None:
    """
    If the repository has PR checks enabled, this will introduce a new status check
    to report current status of the PR. The content of this check will be similar to
    <code>comments.py#update_pr_sticky_comment</code>.
    :param pr: The PullRequest (note: this corresponds to the PR in
        Aviator's database, NOT the GitHub PR number).
    :param status: whether to update PR check as skip, pending, failure or success. This
        will represent the status check in GitHub. If present, the value should override
        the pr.status.
    """
    try:
        with locks.lock("update-pr-check-%d", f"update-pr-check-{pr.id}", expire=20):
            _update_pr_check_sync(pr, status)
    except Exception as e:
        # Sometimes we get weird errors here -- log them for better debugability
        logger.info("Error while updating PR sticky comment", pr_id=pr.id, exc_info=e)
        raise


def _update_pr_check_sync(pr: PullRequest, status: str) -> None:
    repo = pr.repo
    publish_check = repo.current_config.merge_rules.publish_status_check
    if publish_check == "never":
        return

    if not status:
        if pr.status == "open" and publish_check in ("ready", "queued"):
            return
        if pr.status == "pending" and publish_check == "queued":
            return
        status = "pending"
        if pr.status == "blocked":
            status = "failure"
        elif pr.status == "merged":
            status = "success"

    content = messages.get_message_body(repo, pr)
    if not _should_push_update(repo.id, pr.number, status, content):
        logger.info(
            "Repo %d PR %d skipping check status %s", repo.id, pr.number, status
        )
        return

    _, client = common.get_client(repo)
    assert pr.head_commit_sha, (
        f"Missing head commit SHA for PR {pr.number} Repo {repo.id}"
    )
    client.repo.create_check_run(
        app.config["CHECK_RUN_NAME"],
        pr.head_commit_sha or "",
        details_url=app.config["BASE_URL"] + "/queue/queued",
        external_id=str(pr.id),
        status=_get_gh_status(status),
        conclusion=_get_gh_conclusion(status),
        output=dict(
            title="Aviator checks - " + _get_title(pr, status),
            summary=content,
        ),
    )
    logger.info("Repo %d PR %d updated check status %s", repo.id, pr.number, status)

    # Also apply to bot pr.
    if pr.latest_bot_pr:
        bot_pr = pr.latest_bot_pr
        assert bot_pr.head_commit_sha, (
            f"Missing head commit SHA for bot PR {bot_pr.number} Repo {repo.id}"
        )
        client.repo.create_check_run(
            app.config["CHECK_RUN_NAME"],
            bot_pr.head_commit_sha or "",
            details_url=app.config["BASE_URL"] + "/queue/queued",
            external_id=str(pr.id),
            status=_get_gh_status(status),
            conclusion=_get_gh_conclusion(status),
            output=dict(
                title="Aviator checks - " + _get_title(pr, status),
                summary=content,
            ),
        )


def _get_gh_status(status: str) -> str:
    if status in ("success", "failure"):
        return "completed"
    return "in_progress"


def _get_gh_conclusion(
    status: str,
) -> str | None:
    if status in ("success", "failure"):
        return status
    return None


def _get_title(pr: PullRequest, status: str) -> str:
    if status == "success":
        return "All validations completed."
    if status == "pending":
        if pr.status_code == StatusCode.PENDING_TEST:
            # Find test statuses that are pending. Return one and the count.
            test_statuses = custom_checks.get_test_statuses_for_pr(pr)
            count = 0
            pending_test = None
            for test_info in test_statuses:
                if test_info.result == "pending":
                    count += 1
                    pending_test = test_info.name
            if count > 0:
                return f"Pending checks: {pending_test} + {count - 1} more"
        if pr.status == "open":
            return "Waiting for the user to request a merge."
    return StatusCode(pr.status_code).message()


def _should_push_update(repo_id: int, pr_number: int, value: str, content: str) -> bool:
    """
    We do two separate checks here.
    1. Don't immediately push update if we have already published a success status for the PR.
    This check will expire in 1 minute.
    2. We also check if the content of the message is identical to the last one we published.
    """
    key = "status-check-%d-%d" % (repo_id, pr_number)
    existing_value = redis_client.get(key)
    if existing_value and existing_value.decode("utf-8") == "success":
        logger.info(
            "Ignoring the status update since it's marked success",
            repo_id=repo_id,
            pr_number=pr_number,
        )
        return False
    redis_client.set(key, value, ex=time_util.ONE_MINUTE_SECONDS)

    # If the value is success, we should always post the status check
    # to avoid situations where the status is not reported on GitHub and the PR
    # fails to merge.
    if value == "success":
        return True

    # Check if the content is identical to the last one we published.
    key = "status-check-content-%d-%d" % (repo_id, pr_number)
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    existing_hash = redis_client.get(key)
    if existing_hash and existing_hash.decode("utf-8") == content_hash:
        logger.info(
            "Ignoring the status update since it's identical",
            repo_id=repo_id,
            pr_number=pr_number,
        )
        return False
    redis_client.set(key, content_hash, ex=time_util.ONE_WEEK_SECONDS)
    return True