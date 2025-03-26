import math
import uuid

import structlog

from core import comments, common
from core.batch import Batch
from core.client import GithubClient
from core.models import BotPr, GithubRepo, PullRequest, StatusCode
from main import db
from slackhook.events import send_pr_update_to_channel
from util import time_util
from webhooks import controller as hooks

logger = structlog.stdlib.get_logger()


def requeue_prs(
    prs: list[PullRequest],
    repo: GithubRepo,
    failed_batch_pr: PullRequest | None,
    bot_pr: BotPr,
) -> None:
    logger.info(
        "Processing requeue for bisection",
        repo_id=repo.id,
        prs=[pr.number for pr in prs],
        failed_pr=failed_batch_pr,
    )
    comment = _get_comment(failed_batch_pr, bot_pr)
    sorted_prs = sorted(prs, key=lambda p: p.queued_at)
    sorted_prs = _ensure_batch_bisection_attempts(repo, sorted_prs)
    status_code = (
        StatusCode.REQUEUED_BY_BISECTION if repo.batch_size > 1 else StatusCode.QUEUED
    )

    batch_size, batch_count = _get_dimensions(repo, sorted_prs, failed_batch_pr)
    batches = [uuid.uuid4().hex for _ in range(batch_count)]

    for i, pr in enumerate(sorted_prs):
        if repo.batch_size > 1:
            _, client = common.get_client(repo)
            client.create_issue_comment(pr.number, comment)
            if pr.merge_operations and not failed_batch_pr:
                pr.merge_operations.is_bisected = True
                pr.merge_operations.bisected_batch_id = batches[i // batch_size]
                pr.merge_operations.times_bisected += 1
            elif failed_batch_pr and pr.merge_operations:
                # If there's a failed Batch PR, mark other PRs in the batch as clean.
                pr.merge_operations.is_bisected = False
                pr.merge_operations.bisected_batch_id = None
                pr.merge_operations.times_bisected = 0

        # We update the queued_at here to ensure that the ordering on the Queued page is correct.
        # There was no manual action to requeue the PR, so the queued_at will no longer be consistent with
        # the actual time that the PR was queued.
        pr.queued_at = time_util.now()
        pr.set_status("queued", status_code)
        # We do this to create a queued `Activity` object.
        comments.post_pull_comment.delay(pr.id, "queued", ci_map={})

    db.session.commit()
    if repo.batch_size > 1:
        pr_ids = [pr.id for pr in prs]
        hooks.call_webhook_for_batch.delay(bot_pr.id, pr_ids, "batch_bisected")


def handle_successful_bisection(client: GithubClient, bot_pr: BotPr) -> None:
    """
    If a botPR is successful in this scenario, we simply will close the botPR and reset
    # the bisection flag for the PRs in the batch.
    """
    logger.info(
        "Handling successful bisection",
        repo_id=bot_pr.repo_id,
        bot_pr_number=bot_pr.number,
    )
    batch = Batch(client, bot_pr.batch_pr_list)
    comment = (
        f"The parallel bisected batch passed successfully using #{bot_pr.number}, "
        "moving the PR to the regular queue."
    )
    for pr in batch.prs:
        if not pr.merge_operations:
            logger.error(
                "Invariant, the PR should have merge operation",
                pr_id=pr.number,
                repo_id=pr.repo_id,
            )
            continue
        pr.merge_operations.is_bisected = False
        pr.merge_operations.bisected_batch_id = None
        pr.merge_operations.times_bisected = 0
        client.create_issue_comment(pr.number, comment)
        pr.queued_at = time_util.now()
        pr.set_status("queued", StatusCode.QUEUED)
        comments.post_pull_comment.delay(pr.id, "queued", ci_map={})
    bot_pr.set_status("closed", StatusCode.NOT_QUEUED)
    db.session.commit()


def _get_comment(failed_batch_pr: PullRequest | None, bot_pr: BotPr) -> str:
    if failed_batch_pr:
        # We currently bisect the remaining PRs even if we know that failed_batch_pr is the PR that failed.
        # We should ideally not bisect, and just keep all remaining PRs in the same batch.
        # This requires having some sort of indicator in create_new_batches to create 1 batch instead of bisecting.
        return f"Requeuing in a new bisected batch. Failed PR: {failed_batch_pr.markup_text}"
    return f"Requeuing in a new bisected batch: associated failed draft PR: {bot_pr.markup_text}."


def _ensure_batch_bisection_attempts(
    repo: GithubRepo, sorted_prs: list[PullRequest]
) -> list[PullRequest]:
    if not repo.parallel_mode_config.max_bisection_attempts:
        return sorted_prs

    requeueable_prs = []
    for pr in sorted_prs:
        if (
            pr.merge_operations
            and pr.merge_operations.times_bisected
            >= repo.parallel_mode_config.max_bisection_attempts
        ):
            logger.info(
                "PR has been bisected too many times, will not bisect",
                repo_id=repo.id,
                pr_number=pr.number,
                max_bisection_attempts=repo.parallel_mode_config.max_bisection_attempts,
            )
            if pr.status not in ("merged", "closed"):
                # Do not call `mark_as_blocked` here since we attempt requeuing
                # with bisection in `handle_failed_bot_pr`.
                common.set_pr_blocked(pr, StatusCode.BATCH_BISECTION_ATTEMPT_EXCEEDED)
                db.session.commit()
                hooks.send_blocked_webhook(pr)
                send_pr_update_to_channel.delay(pr.id)
                _, client = common.get_client(repo)
                pull = client.get_pull(pr.number)
                client.add_label(pull, repo.blocked_label)
        else:
            requeueable_prs.append(pr)
    return requeueable_prs


def _get_dimensions(
    repo: GithubRepo, prs: list[PullRequest], failed_batch_pr: PullRequest | None
) -> tuple[int, int]:
    if repo.parallel_mode_config.max_bisected_batch_size:
        batch_size = repo.parallel_mode_config.max_bisected_batch_size
        batch_count = math.ceil(len(prs) / batch_size)
    else:
        # To ensure we don't create more than 2 batches, we take the ceiling.
        batch_size = math.ceil(len(prs) / 2)
        batch_count = 2

    if failed_batch_pr:
        # If there's a failed batch PR, we don't need to bisect.
        logger.info(
            "Bisection includes failed batch PR,, will create single batch",
            repo_id=repo.id,
            failed_pr_number=failed_batch_pr.number,
            sorted_prs=prs,
        )
        batch_size = len(prs)
        batch_count = 1
    return batch_size, batch_count


def can_tag_batch(repo: GithubRepo, batch: Batch, all_bot_prs: list[BotPr]) -> bool:
    if batch.is_skip_line():
        return True
    if not repo.parallel_mode_config.max_parallel_builds:
        return True
    if not repo.parallel_mode_config.max_parallel_bisected_builds:
        # If we don't have a separate limit for bisected builds, we should consider all builds.
        return len(all_bot_prs) < repo.parallel_mode_config.max_parallel_builds

    bisected_batch_count = len([bot_pr.is_bisected_batch for bot_pr in all_bot_prs])
    regular_batch_count = len(all_bot_prs) - bisected_batch_count
    if batch.is_bisected_batch():
        return (
            bisected_batch_count
            < repo.parallel_mode_config.max_parallel_bisected_builds
        )
    return regular_batch_count < repo.parallel_mode_config.max_parallel_builds


def is_parallel_bisection_batch(repo: GithubRepo, bot_pr: BotPr) -> bool:
    return bool(
        repo.parallel_mode_config
        and repo.parallel_mode_config.use_parallel_bisection
        and bot_pr.is_bisected_batch
    )
