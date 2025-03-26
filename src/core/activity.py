from __future__ import annotations

from core.models import (
    Activity,
    ActivityApprovedPayload,
    ActivityPayload,
    ActivitySynchronizePayload,
    ActivityType,
    GithubRepo,
    PullRequest,
)
from core.status_codes import StatusCode
from lib.gh import webhooks
from main import db, logger, redis_client
from util.time_util import ONE_WEEK_SECONDS


def create_activity(
    repo_id: int,
    pr_id: int,
    activity_type: ActivityType,
    status_code: StatusCode,
    payload: ActivityPayload | None = None,
    *,
    commit: bool = True,
    dedupe: bool = True,
) -> Activity | None:
    """
    Create a new Activity object for the pull request.

    Returns the Activity that was created (or None of the creation was
    de-duplicated).
    """
    logger.info("Creating activity", activity_type=activity_type, pr_id=pr_id)
    if dedupe and not _should_create_activity(pr_id, activity_type):
        return None
    activity = Activity(
        repo_id=repo_id,
        pull_request_id=pr_id,
        name=activity_type,
        status_code=status_code,
        payload=payload,
    )
    db.session.add(activity)
    if commit:
        db.session.commit()
    _update_latest_activity(pr_id, activity_type)
    return activity


def create_pr_activity_from_webhook(
    repo: GithubRepo,
    pr: PullRequest,
    webhook: webhooks.WebhookBase,
) -> None:
    # TODO
    #     For now, we only capture synchronize here. Importantly, we can't yet
    #     capture labeled here because internally that's done elsewhere and the
    #     sequencing of that matters to how we consider a pull request queued
    #     and/or how we calculate requeue attempts. That logic will need to be
    #     untangled separately.
    if isinstance(webhook, webhooks.WebhookPullRequestSynchronize):
        create_activity(
            repo.id,
            pr.id,
            ActivityType.SYNCHRONIZE,
            StatusCode.UNKNOWN,
            ActivitySynchronizePayload(
                old_head_commit_oid=webhook.before,
                new_head_commit_oid=webhook.after,
                sender_github_login=webhook.sender.login,
            ),
            dedupe=False,
            commit=True,
        )
    elif isinstance(webhook, webhooks.WebhookPullRequestReviewSubmitted):
        if webhook.review.state == "approved":
            create_activity(
                repo.id,
                pr.id,
                ActivityType.APPROVED,
                StatusCode.UNKNOWN,
                ActivityApprovedPayload(
                    approver_github_login=webhook.review.user.login,
                ),
                dedupe=False,
                commit=True,
            )
            logger.info(
                "Created PR approved activity",
                repo_id=repo.id,
                pr_number=pr.number,
            )
    else:
        logger.info("Ignoring webhook", webhook_type=type(webhook))


def _should_create_activity(pr_id: int, activity: ActivityType) -> bool:
    """
    Determine whether or not a new Activity object should be created.
    """
    existing: bytes | None = redis_client.get(_activity_redis_key(pr_id))
    if existing and existing.decode("utf-8") == activity.value:
        return False
    return True


def _update_latest_activity(pr_id: int, activity: ActivityType) -> None:
    """
    Update the latest activity for a PR.
    """
    redis_client.set(_activity_redis_key(pr_id), activity.value, ex=ONE_WEEK_SECONDS)


def _activity_redis_key(pr_id: int) -> str:
    return f"pr/{pr_id}/activity"
