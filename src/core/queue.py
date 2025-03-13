from __future__ import annotations

import collections
import dataclasses
import datetime
import fnmatch
import time
import uuid
from typing import Any

import redis_lock  # type: ignore[import-untyped]
import requests.exceptions
import sqlalchemy as sa
import structlog
import typing_extensions as TE
from sqlalchemy.orm import joinedload

import atc.non_released_prs
import core.custom_validations
import errors
import flexreview.celery_tasks
import schema
import util.posthog_util
from auth.models import AccessToken, Account
from basemodel import BaseModelQuery
from billing import capability
from billing import common as billing_common
from billing.plans import Feature
from core import (
    activity,
    av_checks,
    av_hooks,
    bisection,
    checks,
    comments,
    common,
    custom_checks,
    extract,
    graphql,
    internal_webhooks,
    locks,
    pygithub,
    stack_manager,
    sync,
    target_manager,
)
from core.batch import Batch
from core.client import GithubClient, SquashPullInput
from core.const import MERGED_BY_MQ_LABEL, SKIP_UPDATE_LABEL
from core.models import (
    Activity,
    ActivityBotPullRequestCreatedPayload,
    ActivityMergedPayload,
    ActivityResyncPayload,
    ActivityType,
    BaseBranch,
    BotPr,
    ChangeSetConfig,
    GithubRepo,
    GithubTestStatus,
    GithubUser,
    MergeOperation,
    PullRequest,
    ReadyHookState,
    ReadySource,
    RegexConfig,
    bot_pr_mapping,
)
from core.status_codes import StatusCode
from flexreview import validation
from lib import gh_graphql
from main import app, celery, db, redis_client
from slackhook.common import should_notify
from slackhook.events import (
    notify_pr_blocked,
    notify_pr_ready_to_queue,
    send_pr_update_to_channel,
)
from util import dbutil, time_util
from webhooks import controller as hooks

logger = structlog.stdlib.get_logger()

VALID_ACTIONS = [
    "labeled",
    "unlabeled",
    "closed",
    "opened",
    "reopened",
    "dismissed",
    "submitted",
    "synchronize",
    "ready_for_review",  # Moving from draft to open state
    # We need to listen to the 'edited' event to capture when the PR changes
    # base branch (in order to make sure we're stacking it correctly).
    "edited",
    "resolved",
]

MERGEABILITY_CHECK_MAX_ATTEMPTS = 5
NEW_BATCH_KEY = "new"


@celery.task
def fetch_all_repos(account_id: int) -> None:
    """
    Set up all the repositories for an account.

    This also calls :func:`fetch_prs_async` for each repo.
    """
    account = Account.get_by_id_x(account_id)
    if not account.billing_active:
        logger.error("Fetch repos called for inactive account %d", account.id)
        return
    repos = GithubRepo.query.filter_by(account_id=account_id).all()
    for repo in repos:
        time.sleep(5)
        extract.analyze_first_pull.delay(repo.id)
        fetch_prs_async.delay(repo.id)


@celery.task
def fetch_prs_async(repo_id: int) -> None:
    """
    Fetch the PRs for a repo and add them to the queue.
    """
    repo = GithubRepo.get_by_id(repo_id)
    if not repo or not repo.account.billing_active:
        return

    with repo.bound_contextvars():
        try:
            fetch_prs(repo)
            if repo.is_no_queue_mode:
                process_unordered_async.delay(repo.id)
        except errors.AccessTokenException as exc:
            logger.info("Unable to fetch access token", exc_info=exc)
        except pygithub.BadCredentialsException as exc:
            logger.warning("Authentication failed", exc_info=exc)
        except pygithub.UnknownObjectException as exc:
            logger.info("Unable to fetch repo", exc_info=exc)
        except requests.exceptions.HTTPError as exc:
            if exc.response and exc.response.status_code == 401:
                logger.info("Authentication failed", exc_info=exc)
            else:
                logger.error("Repo failed to fetch", exc_info=exc)
        except Exception as exc:
            logger.error("Repo failed to fetch", exc_info=exc)


@celery.task
def fetch_pending_prs(repo_id: int) -> None:
    """
    Fetch the PRs that are reported as pending mergeable but have all CIs completed.
    We trigger this more frequently only to ensure that the PRs are not waiting on the
    GitHub mergeability check. We already receive webhook events for all the other actions.
    """
    repo = GithubRepo.get_by_id_x(repo_id)
    with repo.bound_contextvars():
        _process_pending(repo)
        _process_stacked_prs(repo_id)


def _process_pending(repo: GithubRepo) -> None:
    pending_prs: list[PullRequest] = (
        PullRequest.query.filter_by(
            repo_id=repo.id,
            status="pending",
            status_code=StatusCode.PENDING_MERGEABILITY,
        )
        .filter(PullRequest.gh_node_id.isnot(None))
        .limit(100)
        .all()
    )
    if not pending_prs:
        return

    logger.info("Found pending PRs for repo", n_prs=len(pending_prs))
    prs_to_validate = []
    for pr in pending_prs:
        if _has_finished_pending_tests(repo, pr, is_pr_pending=True):
            with pr.bound_contextvars():
                logger.info(
                    "PullRequest has finished pending tests, checking mergeable status"
                )
            prs_to_validate.append(pr)
    mergeable_prs = _fetch_gh_mergeable_prs(repo, prs_to_validate)
    for pr in mergeable_prs:
        with pr.bound_contextvars():
            logger.info("PullRequest is mergeable now, calling fetch_pr")
            fetch_pr.delay(pr.number, repo.name)


def _process_stacked_prs(repo_id: int) -> None:
    # Stacked PRs can get stuck in limbo when all CIs finish around the same
    # time. We need to check if they are ready to be queued here.
    pending_stack_prs: list[PullRequest] = PullRequest.query.filter_by(
        repo_id=repo_id,
        status="pending",
        status_code=StatusCode.PENDING_STACK_READY,
    ).all()
    for pr in pending_stack_prs:
        _transition_stacked_pr_queued(pr)


@celery.task
def process_unordered_async(repo_id: int) -> None:
    repo = GithubRepo.get_by_id_x(repo_id)
    with repo.bound_contextvars():
        process_all(repo)


@celery.task
def process_top_async(repo_id: int, optional_target_branch: str = "") -> None:
    repo = GithubRepo.get_by_id(repo_id)
    if not repo or not repo.account.billing_active:
        return

    with repo.bound_contextvars():
        try:
            if repo.is_no_queue_mode:
                return

            target_branches: set[str] = set()
            if not optional_target_branch:
                target_branches = _get_all_target_branches(repo)
            else:
                target_branches.add(optional_target_branch)
            for target_branch in target_branches:
                process_top_lock = f"process_top-{repo.id}-{target_branch}"
                if locks.is_locked(process_top_lock):
                    logger.info(
                        "Process top is already locked for repo, skipping current celery task",
                        target_branch=target_branch,
                    )
                    continue
                with locks.lock("process_top", process_top_lock, expire=600):
                    with locks.for_repo(repo):
                        db.session.refresh(repo)
                        if repo.parallel_mode:
                            process_top_parallel_mode(repo, target_branch)
                        else:
                            process_top_fifo_for_branch(repo, target_branch)
        except pygithub.UnknownObjectException as exc:
            logger.info("Unable to fetch repo", exc_info=exc)
        except errors.GithubMergeabilityPendingException:
            logger.info(
                "Failed to process top due to pending mergeability, will try again shortly"
            )
        except pygithub.BadCredentialsException as exc:
            logger.info("Failed to connect with GitHub", exc_info=exc)
        except Exception as exc:
            logger.error("Failed to process top for repo", exc_info=exc)


def fetch_prs(repo: GithubRepo) -> None:
    logger.info("Fetching PullRequests for repository")
    graphql_client = _get_graphql(repo)
    _, client = common.get_client(repo)
    try:
        pr_numbers = graphql_client.get_approved_pulls(
            repo.org_name,
            repo.repo_name,
            labels=repo.queue_labels,
            count=50,
        )
        # Find all the PRs that have ready label, and process the ones that are not already queued.
        pull_requests: list[PullRequest] = (
            PullRequest.query.filter_by(repo_id=repo.id)
            .filter(PullRequest.number.in_(pr_numbers))
            .all()
        )
    except requests.exceptions.ReadTimeout as exc:
        # In case the request times out, let's fallback to a direct DB query.
        logger.info("Failed to fetch repos", repo_id=repo.id, exc_info=exc)
        pr_list: list[PullRequest] = (
            PullRequest.query.filter_by(repo_id=repo.id)
            .filter(PullRequest.status == "pending")
            .order_by(PullRequest.id.desc())
            .limit(50)
            .all()
        )
        pull_requests = pr_list
        pr_numbers = [p.number for p in pull_requests]

    total_fetched = 0
    for pr in pull_requests:
        if pr.status in [
            "open",
            "pending",
            "blocked",
        ]:
            content: dict[str, Any] | None = None
            if pr.status_code == StatusCode.PENDING_MERGEABILITY:
                pull = client.get_pull(pr.number)
                checks.fetch_latest_test_statuses(client, repo, pull, botpr=False)
                content = pull.raw_data
            # Set the attempt to max attempts to avoid retries since this is a slow cron.
            fetch_pr_with_repo(
                pr.number,
                repo,
                content=content,
                attempt=MERGEABILITY_CHECK_MAX_ATTEMPTS,
            )
            if app.config["FLEXREVIEW_VALIDATION"]:
                # This validation is typically done via the pull request review
                # webhook, but we also do it here to catch any missing webhooks.
                validation.validate_flex_mergeability.delay(pr.id, status="completed")
            total_fetched += 1
    logger.info(
        "Fetched PullRequests for repository",
        total_repo_prs=len(pr_numbers),
        n_prs_fetched=total_fetched,
    )

    if repo.parallel_mode:
        tag_queued_prs_async.delay(repo.id)

    # If a user uses slash command instead of a label, we do not get the PR from the
    # graphQL API. So fetching rest of the pending PRs here.
    # The difference between this call and `fetch_pending_prs` is that this call
    # fetches all the pending PRs independent of the CI status. Since this will
    # end up doing a lot more calls, we only do this in the slow cron.
    pending_prs: list[PullRequest] = (
        PullRequest.query.filter_by(
            repo_id=repo.id,
            status="pending",
            status_code=StatusCode.PENDING_MERGEABILITY,
        )
        .filter(PullRequest.number.notin_(pr_numbers))
        .all()
    )

    mergeable_prs = _fetch_gh_mergeable_prs(repo, pending_prs)
    for pr in mergeable_prs:
        with pr.bound_contextvars():
            logger.info("Pull request is mergeable, calling fetch_pr")
            pull = client.get_pull(pr.number)
            checks.fetch_latest_test_statuses(client, repo, pull, botpr=False)
            content = pull.raw_data
            fetch_pr.delay(pr.number, repo.name, content=content)

    unresolved_prs: list[PullRequest] = (
        PullRequest.query.filter_by(
            repo_id=repo.id,
            status="pending",
            status_code=StatusCode.UNRESOLVED_CONVERSATIONS,
        )
        .filter(PullRequest.number.notin_(pr_numbers))
        .all()
    )

    resolved_prs = graphql_client.get_resolved_conversation_pulls(repo, unresolved_prs)
    for pr_number in resolved_prs:
        logger.info(
            "PullRequest has resolved conversations, calling fetch_pr",
            pr_number=pr_number,
        )
        fetch_pr.delay(pr_number, repo.name)


@celery.task
def tag_queued_prs_async(repo_id: int) -> None:
    repo = GithubRepo.get_by_id_x(repo_id)
    with repo.bound_contextvars():
        tag_queued_lock = f"tag_queued-{repo.id}"
        if locks.is_locked(tag_queued_lock):
            logger.info(
                "Tag queued PR is already locked for repo, skipping current celery task"
            )
            return
        with locks.lock("tag_queued", tag_queued_lock, expire=600):
            tag_queued_prs_locking(repo)


@celery.task
def fetch_pr(
    pull_number: int,
    repo_name: str,
    action: str = "",
    label: str = "",
    head_sha: str = "",
    content: dict | None = None,
    timestamp: int = 0,
) -> None:
    """
    Handle a PR update from GitHub.

    This function will create a new PR object if it doesn't exist, or update
    an existing PR object if it does.

    :param pull_number: The GitHub PR number.
    :param repo_name: The GitHub repo name (in the form of `owner/repo`).
    :param action: The type of action (from the GitHub API).
    :param label: The label that was added or removed (if any).
    :param head_sha: The git SHA of the head commit.
    :param content: The content of the PR.
    :param timestamp: The timestamp when the fetch_pr method was queued.
    """
    logger.info(
        "Fetching PullRequest",
        repo_name=repo_name,
        pr_number=pull_number,
        pr_action=action,
    )
    repo = common.get_repo_by_name(repo_name, require_billing_active=False)
    if not repo:
        # only log error if this is an entirely missing repo.
        logger.info(
            "Repository was not found while fetching PullRequest",
            repo_name=repo_name,
            pr_number=pull_number,
        )
        return
    with (
        repo.bound_contextvars(),
        structlog.contextvars.bound_contextvars(pr_action=action),
    ):
        return fetch_pr_with_repo(
            pull_number,
            repo,
            action,
            label,
            head_sha,
            content,
            timestamp=timestamp,
        )


def fetch_pr_with_repo(
    pull_number: int,
    repo: GithubRepo,
    action: str = "",
    label: str = "",
    head_sha: str = "",
    content: dict | None = None,
    *,
    timestamp: int = 0,
    attempt: int = 1,
) -> None:
    # Save unnecessary API calls by skipping for unuseful actions.
    if action and action not in VALID_ACTIONS:
        return

    logger.info("Fetching latest pull request data from GitHub")
    if not repo.account.billing_active:
        logger.info("Repository does not belong to an active account, aborting fetch")
        return
    if not repo.active:
        logger.info("Repository is not active, aborting fetch")
        return

    try:
        access_token, client = common.get_client(repo)
        if not client:
            logger.info("Failed to instantiate GitHub client for repository")
            return
        pull = None
        if content:
            pull = common.get_pull_from_payload(client, content, timestamp=timestamp)
        if not pull:
            pull = client.get_pull(pull_number)
        force_validate_mergeability = attempt >= MERGEABILITY_CHECK_MAX_ATTEMPTS - 1

        if common.is_av_bot_pr(
            account_id=repo.account_id,
            author_login=pull.user.login,
            title=pull.title,
        ):
            ensure_bot_pull_status(client, repo, pull)
            return

        git_user, pr = common.ensure_pull_request(repo, pull)
        if not pr:
            return
        pr.bind_contextvars()
        with locks.for_pr(pr, ignore_not_acquired=True):
            db.session.refresh(pr)
            marked_ready = did_mark_ready(pr)
            did_queue = _process_fetched_pull(
                access_token,
                pr,
                pull,
                repo,
                git_user,
                client,
                action,
                label,
                head_sha,
                force_validate_mergeability=force_validate_mergeability,
                marked_ready=marked_ready,
            )

            logger.info(
                "Processed fetched pull request",
                pr_status=pr.status,
                did_queue=did_queue,
            )
            if repo.parallel_mode:
                if pr.status == "queued" or marked_ready:
                    # In case of parallel mode, a PR should not stay in queued state for long.
                    # So any time an event happens that might change the status, we should
                    # try tagging the PR.
                    tag_queued_prs_async.delay(repo.id)
            elif did_queue and marked_ready:
                process_top_async.delay(repo.id, pull.base.ref)
            if did_queue and repo.is_no_queue_mode:
                review_pr_async.delay(pr.id)
            return
    except pygithub.BadCredentialsException as exc:
        logger.warning("Authentication failed", exc_info=exc)
        db.session.commit()
    except errors.GithubMergeabilityPendingException as exc:
        pr_object = common.get_pr_by_number(repo, pull_number)
        if (
            pr_object
            and pr_object.status == "pending"
            and pr_object.status_code != StatusCode.PENDING_MERGEABILITY
        ):
            logger.info(
                "Changing PR status to pending mergeability",
                repo_id=repo.id,
                pr_number=pr_object.number,
                status_code=pr_object.status_code,
            )
            pr_object.status_code = StatusCode.PENDING_MERGEABILITY
            db.session.commit()
        if attempt < MERGEABILITY_CHECK_MAX_ATTEMPTS:
            logger.info(
                "Re-triggering fetch_pr due to pending mergeability", exc_info=exc
            )
            time.sleep(5 * attempt)
            fetch_pr_with_repo(
                pull_number,
                repo,
                action,
                label,
                head_sha,
                content,
                timestamp=timestamp,
                attempt=attempt + 1,
            )
            return
        logger.error(
            "Failed to detect PR mergeability",
            attempts=attempt,
        )
    except redis_lock.NotAcquired as exc:
        logger.info("Lock was already released", pr_number=pull_number, exc_info=exc)
    except Exception as exc:
        logger.error(
            "Failed to process PullRequest",
            pr_number=pull_number,
            exc_info=exc,
        )


@celery.task
def fetch_pr_from_status(
    *, repo_name: str, context: str, status: str, head_sha: str, branches: list[dict]
) -> None:
    """
    Handle a PR status update from GitHub.

    This function should be called when a GitHub PR has an updated status
    (e.g. a test suite has completed).

    :param repo_name: The GitHub repo name (in the form of `owner/repo`).
    :param context: The name of the status check (e.g. `code-coverage`).
        For GitHub actions, this is the name of the job; for other checks
        (e.g., CircleCI), it is defined by the check itself (e.g.,
        `ci/circleci: build-and-deploy` for a CircleCI job named
        `build-and-deploy`).
    :param status: The result of the status check
    :param head_sha: The head sha of the status check
    :param branches: The list of branches that the status check applies to.
        Each branch should be a dict with a `name` field that contains the name
        of the branch.
    """
    repo = common.get_repo_by_name(repo_name)
    if not repo:
        repo = GithubRepo.query.filter_by(name=repo_name).first()
        if not repo:
            # only log error if this is an entirely missing repo.
            logger.info("GithubRepo not found", repo_name=repo_name)
        return

    for branch in branches:
        branch_name = branch["name"]
        pr: PullRequest | None = (
            PullRequest.query.filter_by(
                repo_id=repo.id,
                branch_name=branch_name,
                head_commit_sha=head_sha,
            )
            .filter(PullRequest.status.in_(["pending", "queued", "open"]))
            .order_by(PullRequest.id.desc())
            .first()
        )
        if pr:
            if pr.status == "open":
                notify_if_mergeable(pr)
                return
            if should_process_pr(pr, context, is_pr_pending=(pr.status == "pending")):
                logger.info(
                    "Repo %d Found queued PR %d with status change for context %s: %s",
                    repo.id,
                    pr.number,
                    context,
                    status,
                )
                if repo.is_no_queue_mode:
                    if pr.status == "queued":
                        try:
                            review_pr(repo, pr)
                        except errors.GithubMergeabilityPendingException:
                            logger.info(
                                "Repo %d PR %d Retriggering for pending mergeability",
                                repo.id,
                                pr.number,
                            )
                            review_pr_async.delay(pr.id)
                    else:
                        # pending state.
                        fetch_pr(pr.number, repo.name)
                    return

                if (
                    repo.parallel_mode_config.check_mergeability_to_queue
                    and pr.status == "pending"
                ):
                    # add a 5 seconds delay to ensure that GH provides updated test results
                    fetch_pr.with_countdown(5).delay(pr.number, repo.name)
                    return

                # optimize to only process the top PRs.
                assert pr.target_branch_name, (
                    f"PullRequest id={pr.id} has no target_branch_name"
                )
                top_pr = common.get_top_pr(repo.id, pr.target_branch_name)
                if top_pr and pr.id == top_pr.id:
                    logger.info(
                        "Repo %d top PR %d updated with a valid test. Initiating review.",
                        repo.id,
                        pr.number,
                    )
                    # add a 5 seconds delay to ensure that GH provides updated test results
                    process_top_async.with_countdown(5).delay(
                        repo.id, pr.target_branch_name
                    )
                return
        elif repo.parallel_mode:
            # get the *latest* queued botpr for this repo and branch
            bot_pr = (
                BotPr.query.filter_by(
                    repo_id=repo.id,
                    branch_name=branch_name,
                    status="queued",
                    head_commit_sha=head_sha,
                )
                .order_by(BotPr.id.desc())
                .first()
            )
            if bot_pr:
                custom_checks.update_test_statuses_for_botpr(bot_pr)
            if not bot_pr or not is_bot_pr_top(bot_pr, repo):
                continue

            if should_process_pr(bot_pr, context, is_bot_pr=True):
                logger.info(
                    "Repo %d top bot PR %d updated with a valid test. Initiating review.",
                    repo.id,
                    bot_pr.number,
                )
                # add a 5 seconds delay to ensure that GH provides updated test results
                process_top_async.with_countdown(5).delay(
                    repo.id, bot_pr.pull_request.target_branch_name
                )


@celery.task(
    autoretry_for=(errors.GithubMergeabilityPendingException,),
    retry_backoff=10,
    retry_kwargs={"max_retries": 5},
    retry_jitter=True,
)
def review_pr_async(pr_id: int) -> None:
    pr = PullRequest.get_by_id_x(pr_id)
    review_pr(pr.repo, pr)


def review_pr(repo: GithubRepo, pr: PullRequest) -> None:
    access_token, client = common.get_client(repo)
    if not client:
        return None
    review_pr_for_merge(access_token, repo, client, pr)


def _get_queued_bot_prs(repo: GithubRepo, target_branch_name: str) -> list[BotPr]:
    bot_prs: list[BotPr] = (
        BotPr.query.filter_by(repo_id=repo.id, status="queued")
        .join(PullRequest, BotPr.pull_request_id == PullRequest.id)
        .filter(PullRequest.target_branch_name == target_branch_name)
        .order_by(BotPr.id.asc())
        .all()
    )
    return bot_prs


def _transition_pr_to_queued(
    client: GithubClient,
    repo: GithubRepo,
    pr: PullRequest,
    pull: pygithub.PullRequest,
) -> None:
    """
    Transition a PR to the queued state.

    This only updates the state of the PR. It's up to the caller to still call
    `process_top_async` to process the PR.

    :param repo: The repository the PR belongs to.
    :param pull: The PR to transition.
    """
    # get latest copy of the pr to make sure are updating the status correctly.
    db.session.refresh(pr)
    if pr.status in ("queued", "tagged"):
        logger.info(
            "Skipping pull transition to queued",
            repo_id=repo.id,
            pr_number=pr.number,
            pr_status=pr.status,
        )
        return
    logger.info("Repo %d PR %d PR queued", repo.id, pr.number)
    if stack_manager.is_in_stack(pr):
        _transition_stacked_pr_queued(pr)
    else:
        pr.set_status("queued", StatusCode.QUEUED)
        pr.queued_at = time_util.now()
        db.session.commit()
        _publish_queued(pr)

    _remove_blocked_label(client, repo, pull)

    # post a message when the PR is queued but the repo is paused.
    assert pr.target_branch_name, f"PR id={pr.id} has no target branch"
    if _is_queue_paused(repo, pr.target_branch_name):
        comment_for_paused_async.delay(pr.id)


def _transition_stacked_pr_queued(current_pr: PullRequest) -> None:
    """
    Queueing stacked PRs is a bit more complicated. We will move the PR to a pending
    state while setting `StatusCode.PENDING_STACK_READY` to capture that this PR is
    ready to be queued but it's waiting on other PRs in the stack.
    Once all the PRs are good to go, we will set all of them as queued together.
    """
    eligible, pending_prs = stack_manager.is_stack_queue_eligible(current_pr)
    logger.info(
        "Transitioning stacked PRs to queued",
        current_pr=current_pr.number,
        repo_id=current_pr.repo_id,
        eligible=eligible,
        pending_prs=pending_prs,
    )
    if not eligible:
        current_pr.status = "pending"
        current_pr.status_code = StatusCode.PENDING_STACK_READY
        db.session.commit()
        return

    # Commit all status to DB and then publish the notifications to avoid any race conditions.
    for pr in pending_prs + [current_pr]:
        pr.set_status("queued", StatusCode.QUEUED)
        pr.queued_at = time_util.now()
    db.session.commit()
    for pr in pending_prs + [current_pr]:
        _publish_queued(pr)


# TODO(ankit): we can define this as a publish service and pull out all activities
#  and notifications in a single entry point.
def _publish_queued(pr: PullRequest) -> None:
    comments.post_pull_comment.delay(pr.id, "queued")
    pilot_data = common.get_pilot_data(pr, "queued")
    hooks.call_master_webhook.delay(
        pr.id, "queued", pilot_data=pilot_data, ci_map={}, note=""
    )
    send_pr_update_to_channel.delay(pr.id)


def _reset_bot_pr_for_not_ready(
    client: GithubClient,
    repo: GithubRepo,
    pr: PullRequest,
) -> None:
    """
    Reset a BotPR after its target PR entered not_ready state.
    """
    bot_pr = pr.latest_bot_pr
    if not bot_pr:
        logger.info(
            "Not resetting BotPR for Repo %d PR %d (no open BotPR found)",
            repo.id,
            pr.number,
        )
        return
    bot_pr.bind_contextvars()

    with locks.for_repo(repo):
        # refresh the BotPR as it might be stale
        # https://docs.sqlalchemy.org/en/13/orm/session_state_management.html#when-to-expire-or-refresh
        db.session.refresh(bot_pr)
        handle_failed_bot_pr(
            client,
            bot_pr.repo,
            bot_pr,
            ci_map={},
            update_pr_status=False,
        )


def _transition_pr_not_ready(
    client: GithubClient,
    repo: GithubRepo,
    pr: PullRequest,
    pull: pygithub.PullRequest,
    status_code: StatusCode,
    *,
    new_status: str | None = None,
    note: str | None = None,
    status_reason: str | None = None,
) -> None:
    """
    Transition a PR to not ready.

    This transitions a PR as follows:
        {open, pending} -> pending
        {queued, tagged, blocked} -> blocked

    It's important that we don't transition a PR from queued back to pending in
    order to make sure we don't auto-requeue things when not desired.
    """
    old_status = pr.status
    old_status_code = pr.status_code
    if not new_status:
        if pr.status in ("open", "pending"):
            new_status = "pending"
        elif pr.status in ("queued", "blocked", "tagged"):
            new_status = "blocked"
        else:
            raise Exception(
                f"Illegal PR transition: can't transition {pr.status} to not ready"
            )

    logger.info(
        "Transitioning pull request to not ready state",
        old_status=old_status,
        old_status_code=old_status_code,
        new_status=new_status,
        new_status_code=status_code,
        new_status_reason=status_reason,
    )
    if pr.status == new_status and status_code == StatusCode.BLOCKED_BY_LABEL:
        # This is the scenario where new status code is possibly just overriding
        # existing blocked event that happened within Aviator.
        return

    if (
        pr.status == new_status
        and pr.status_code == status_code
        and pr.status_reason == status_reason
    ):
        # Early exit if we're not actually changing anything
        return

    if new_status == "pending":
        pr.set_status("pending", status_code, status_reason)
        _remove_blocked_label(client, repo, pull)
        db.session.commit()
    else:
        if pr.status == "tagged":
            # Do not call `mark_as_blocked` here since `_reset_bot_pr_for_not_ready`
            # will do some additional handling.
            # TODO: Can this be all combined for one place to manage blocked behavior.
            common.set_pr_blocked(pr, status_code, status_reason)
            db.session.commit()
            mark_ancestor_prs_blocked_by_top(client, pr)
            _reset_bot_pr_for_not_ready(client, repo, pr)
        else:
            mark_as_blocked(
                client,
                repo,
                pr,
                pull,
                status_code,
                note=note,
                status_reason=status_reason,
            )


def _remove_blocked_label(
    client: GithubClient, repo: GithubRepo, pull: pygithub.PullRequest
) -> None:
    """
    Remove the blocked label from a PR (if it is set).
    """
    is_marked_blocked = common.has_label(pull, repo.blocked_label)
    if is_marked_blocked:
        try:
            client.remove_label(pull, repo.blocked_label)
        except pygithub.GithubException as e:
            if e.status != 404:
                raise
            logger.info(
                "Blocked label already removed",
                repo_id=repo.id,
                pr_number=pull.number,
                exc_info=e,
            )


def remove_ready_labels(
    client: GithubClient, repo: GithubRepo, pull: pygithub.PullRequest
) -> list[str]:
    """
    Remove the ready labels from a PR (if it is set).

    Returns removed ready labels if any.
    """
    labels = [l.name for l in pull.labels]
    removed = []
    for l in repo.queue_labels:
        try:
            client.remove_label(pull, l)
            removed.append(l)
        except pygithub.GithubException as e:
            if e.status != 404:
                raise
    logger.info("Repo %d PR %d removed labels %s", repo.id, pull.number, removed)
    return removed


def _pr_has_exceeded_max_requeue_attempts(repo: GithubRepo, pr: PullRequest) -> bool:
    # We do not want to requeue blocked PRs in non-parallel mode. If the PR is blocked, we should respect that.
    # If the config has been changed from parallel -> no-queue (ex. Figma), this avoids requeuing PRs that
    # have failed tests in the corresponding draft PR.
    if not repo.parallel_mode:
        return True
    if not repo.parallel_mode_config.max_requeue_attempts:
        # do not try to requeue in parallel mode unless requeue attempts is set.
        return True
    attempts = get_requeue_attempts(repo.id, pr.id)
    if attempts - 1 >= repo.parallel_mode_config.max_requeue_attempts:
        # exclude first attempt
        logger.info(
            "Repo %d PR %d exceeded max requeue attempts %d",
            repo.id,
            pr.number,
            attempts - 1,
        )
        return True
    return False


def _process_fetched_pull(
    access_token: AccessToken,
    pr: PullRequest,
    pull: pygithub.PullRequest,
    repo: GithubRepo,
    git_user: GithubUser,
    client: GithubClient,
    action: str | None,
    label: str | None,
    head_sha: str | None,
    *,
    force_validate_mergeability: bool,
    marked_ready: bool,
) -> bool:
    if action in ("opened", "reopened") and capability.is_supported(
        pr.account_id, Feature.CHANGE_SET
    ):
        cs_config = ChangeSetConfig.for_account(pr.account_id)
        if cs_config and cs_config.enable_auto_creation:
            from core.changesets import create_changeset_from_relevant_prs

            create_changeset_from_relevant_prs(pr)
        comments.new_pr_changeset.delay(pr.id)

    did_queue_pr = _process_fetched_pull_for_state_transition(
        access_token,
        pr,
        pull,
        repo,
        git_user,
        client,
        action,
        label,
        head_sha,
        force_validate_mergeability=force_validate_mergeability,
        marked_ready=marked_ready,
    )

    if action == "synchronize" and pr.change_sets:
        # If the PR head has been moved, the associated queued ChangeSets must
        # be cancelled.
        for cs in pr.change_sets:
            if cs.status == "queued":
                cs.status = "pending"
        db.session.commit()

    # Enqueue any necessary further actions
    update_gh_pr_status.delay(pr.id)
    _queue_process_stack_dependents(repo, pr)
    _queue_process_stack_ancestors(repo, pr)

    # TODO:
    #     Why are some of these checks (where we then enqueue further action)
    #     here in this function while others are in fetch_pr_with_repo?
    if did_queue_pr:
        if repo.is_no_queue_mode and repo.update_latest:
            # in unordered mode, merged base branch once when PR is queued.
            logger.info("Updating pull request for no-queue mode with update-latest")
            _merge_once(repo, client, pr, pull)

    if pr.status == "queued" and not repo.parallel_mode:
        top_pr = common.get_top_pr(
            repo.id,
            pr.target_branch_name,
        )
        if top_pr and top_pr.id == pr.id:
            logger.info("Scheduling queue processing for repository")
            process_top_async.delay(
                repo.id,
                pr.target_branch_name,
            )

    return did_queue_pr


def _verify_mergeability(
    client: GithubClient, repo: GithubRepo, pull: pygithub.PullRequest
) -> bool:
    """
    Verify that a PR is mergeable by creating a dummy branch and triggering a merge.
    This is a workaround for the fact that the GitHub API doesn't return the `pull.mergeable`
    state correctly.
    """
    logger.info(
        "Verifying mergeability using tmp branch",
        repo_id=repo.id,
        pr_number=pull.number,
    )
    try:
        tmp_branch = client.create_latest_branch(pull, pull.base.ref, tmp_only=True)
        client.delete_ref(tmp_branch)
        logger.info(
            "Mergeability verified using tmp branch",
            repo_id=repo.id,
            pr_number=pull.number,
        )
        return True
    except pygithub.GithubException as e:
        if common.is_network_issue(e):
            logger.info(
                "Failed to create branch for mergeability check",
                repo_id=repo.id,
                pr_number=pull.number,
            )
            raise e
        if e.status == 409:
            logger.info(
                "Mergeability failed using tmp branch: merge conflict",
                repo_id=repo.id,
                pr_number=pull.number,
                exc_info=e,
            )
            return False
        logger.info(
            "Mergeability failed using tmp branch",
            repo_id=repo.id,
            pr_number=pull.number,
            exc_info=e,
        )
        raise errors.GithubMergeabilityPendingException(
            "Unknown Github mergeability for repo %d PR %d" % (repo.id, pull.number)
        )


def _is_valid_labeled_action(
    client: GithubClient,
    repo: GithubRepo,
    pr: PullRequest,
    pull: pygithub.PullRequest,
    label: str | None,
) -> bool:
    """
    We are only using this function as a check for Doordash.
    This should be removed once the prequeue hook is production ready.
    """
    if not label or not repo.skip_line_label:
        return True
    if (
        label == repo.skip_line_label.name
        and repo.current_config.merge_rules.require_skip_line_reason
    ):
        if should_post(repo.id, pr.number, "skip_line"):
            client.create_issue_comment(
                pr.number,
                f"Error: Cannot queue PR with the label `{repo.skip_line_label.name}`. To skip the line in the queue, "
                "add a comment on the PR using `/aviator merge --skip-line=<insert reason for skipping>`.",
            )
        client.remove_label(pull, repo.skip_line_label.name)
        common.ensure_merge_operation(pr, ready=False, ready_source=ReadySource.UNKNOWN)
        return False
    return True


@dbutil.autocommit
def _process_fetched_pull_for_state_transition(
    access_token: AccessToken,
    pr: PullRequest,
    pull: pygithub.PullRequest,
    repo: GithubRepo,
    git_user: GithubUser,
    client: GithubClient,
    action: str | None,
    label: str | None,
    head_sha: str | None,
    *,
    force_validate_mergeability: bool,
    marked_ready: bool,
) -> bool:
    """
    Process a pull request and determine which actions to take next.

    This is useful when we know a PR has been updated in some way (e.g., was
    labeled with the ready label, approved, passed CI, etc.).

    This function checks several pre-requisites for the PR (e.g., is it labeled
    for merging, does it passing all required CI checks, etc.) and then either
    queues the PR for merge or informs the user that the PR cannot be queued (if
    applicable).

    See :func:`fetch_pr` for a description of the parameters.

    TODO:
        Separate all of the fetch (internal state updates) logic from the logic
        that works through the queue of PRs that are ready to merge (move into
        two files).
    TODO:
        Make a lot of this more purely-functional (for testability).

    :return: `True` if the PR was queued for merging, `False` otherwise.
    """
    # If the GitHub pull request is marked as closed, we always want to mark it
    # internally as merged/closed. This needs to be at the top of the function
    # since "closed" has higher precedence than most other things (such as draft
    # status).
    if pull.state == "closed":
        # TODO:
        #     Introduce new "closed" state
        #     Currently, "closed without merging" is stored in our database as
        #     `state = 'merged'` with "merged_at IS NULL".
        logger.info("Pull request is closed, skipping processing and updating database")
        set_pr_as_closed_or_merged(
            client,
            pr,
            merged=pull.merged,
            merged_at=pull.merged_at,
            merged_by_login=pull.merged_by.login if pull.merged_by else None,
            merge_commit_sha=pull.merge_commit_sha,
            lock=True,
        )
        bot_pr = pr.latest_bot_pr
        if bot_pr:
            update_bot_pr_on_pr_merge(client, repo, pr, pull, bot_pr)
        return False

    # If we marked it closed (i.e., status == merged and merged_at is None)
    # but GitHub reports it as open, it probably(?) means that the PR was
    # re-opened. We'll continue processing it below to check if we need to do
    # anything else with it now that it's open again.
    # We are checking both merged and closed states because historically we have
    # marked closed PR as merged.
    # IMPORTANT: This needs to happen before most of the things below, otherwise
    # we'll get illegal state transition errors (e.g., merged -> blocked is not
    # allowed without going through the open state).
    if pr.status in ("merged", "closed") and pull.state == "open":
        if pr.merged_at:
            time_since_merged = time_util.now() - pr.merged_at
            if time_since_merged < datetime.timedelta(seconds=10):
                logger.info(
                    "Pull request is open shortly after we marked it as merged, "
                    "skipping status change to avoid race condition",
                    pr_merged_at=pr.merged_at,
                )
                return False
            logger.error(
                "Pull request was reported as open after merging",
                github_pull_state=pull.state,
                pr_merged_at=pr.merged_at,
            )
        pr.set_status("open", StatusCode.NOT_QUEUED)
        db.session.commit()

    old_commit_sha = pr.head_commit_sha
    pr.head_commit_sha = head_sha or pull.head.sha

    if pull.draft:
        if pr.status == "tagged" or pr.status == "queued":
            _transition_pr_not_ready(
                client,
                repo,
                pr,
                pull,
                StatusCode.IN_DRAFT,
                status_reason="PR was marked as draft after queueing",
            )
            return False
        previously_draft = pr.status_code == StatusCode.IN_DRAFT
        logger.info("Pull request is a draft, skipping processing")
        pr.set_status("open", StatusCode.IN_DRAFT)
        removed_ready_labels = remove_ready_labels(client, repo, pull)
        if removed_ready_labels or marked_ready:
            common.ensure_merge_operation(
                pr, ready=False, ready_source=ReadySource.UNKNOWN
            )
            client.create_issue_comment(
                pr.number,
                "This pull request can't be queued because it's currently a draft.",
            )
        db.session.commit()

        # HACK for Stacked PRs
        # Because of fast transitions of PR draft state, sometimes we end up in a bad state
        # reporting the PR as draft. Call fetch_pr with a delay to ensure the correct status after
        # transition.
        if not previously_draft:
            fetch_pr.with_countdown(5).delay(pr.number, repo.name)
        return False
    elif pr.status_code == StatusCode.IN_DRAFT:
        pr.set_status("open", StatusCode.NOT_QUEUED)
        db.session.commit()

    _ensure_skip_line(repo, pull, pr, action, label)

    if head_sha and head_sha != pull.head.sha:
        # skip processing the PR as a new commit is created. Github sometimes
        # gives stale commit ID at this stage, and causes inconsistent behavior.
        logger.info("Pull request HEAD OID does not match, skipping processing")
        return False
    if (
        repo.parallel_mode
        and pr.status in ("tagged", "queued")
        and old_commit_sha != pr.head_commit_sha
    ):
        logger.info(
            "Pull request HEAD OID changed",
            old_commit_oid=old_commit_sha,
            new_commit_oid=pr.head_commit_sha,
        )
        db.session.commit()  # save the updated commit sha
        if pr.status == "tagged":
            bot_pr_set_failure_locking(client, repo, pr, StatusCode.COMMIT_ADDED)
            mark_ancestor_prs_blocked_by_top(client, pr)
        else:
            # If the PR is still in queued state, just move it back to pending state so it can be validated again.
            _transition_pr_not_ready(
                client,
                repo,
                pr,
                pull,
                StatusCode.PENDING_MERGEABILITY,
                new_status="pending",
            )
        return False

    if not repo.active or not repo.account.billing_active:
        return False

    # Make sure the PR passes "preflight" checks.
    # The preflight checks are made on every PR regardless of its status.
    try:
        _assert_pr_preflight_checks(access_token, repo, pr)
    except errors.PRStatusException as exc:
        mark_as_blocked(client, repo, pr, pull, exc.code, note=exc.message)
        return False

    if marked_ready:
        # In this case an invalid skip line label can cause a ready and immediately
        # non-ready event. Technically from the notion of how labeling action works, this is
        # an acceptable behavior, but probably not-ideal.
        if not _is_valid_labeled_action(client, repo, pr, pull, label):
            return False

    merge_operation = _ensure_pr_merge_operation_if_ready(repo, pr, pull)
    logger.bind(merge_operation_id=merge_operation.id if merge_operation else None)

    # Check custom validations before the PR is queued since we want to surface
    # that information early.
    validation_issues = core.custom_validations.check_custom_validations(repo, pr)
    if validation_issues:
        # The custom validation issues are surfaced by the sticky comment. Here,
        # if the PR is not in the open state, mark this PR as blocked.
        if pr.status != "open" or merge_operation:
            mark_as_blocked(
                client, repo, pr, pull, StatusCode.CUSTOM_VALIDATIONS_FAILED
            )
        else:
            logger.info(
                "Pull request is failing the custom validation, skipping processing"
            )
        return False
    elif pr.status_code == StatusCode.CUSTOM_VALIDATIONS_FAILED:
        # Remove the custom validation error. If the PR is not requested to
        # be queued, set to open state, otherwise move to pending.
        if not merge_operation:
            pr.set_status("open", StatusCode.NOT_QUEUED)
            db.session.commit()
            _remove_blocked_label(client, repo, pull)
        else:
            # If the PR has a merge operation, just move it back to pending state so it can be validated again.
            _transition_pr_not_ready(
                client,
                repo,
                pr,
                pull,
                StatusCode.PENDING_MERGEABILITY,
                new_status="pending",
            )
        return False

    # Execute the ready hook if we need to!
    if merge_operation:
        did_execute_ready_hook = False
        with locks.lock(
            "merge-operation",
            f"merge-operation/{merge_operation.id}",
        ):
            try:
                if merge_operation.ready_hook_state is ReadyHookState.PENDING:
                    did_execute_ready_hook = av_hooks.ready(
                        client, access_token, repo, pr, pull
                    )
                    merge_operation.ready_hook_state = ReadyHookState.SUCCEEDED
            except av_hooks.HookExecutionError as exc:
                logger.info("Ready hook failed", exc_info=exc)
                merge_operation.ready_hook_state = ReadyHookState.FAILED
                _transition_pr_not_ready(
                    client,
                    repo,
                    pr,
                    pull,
                    StatusCode.BLOCKED_BY_READY_HOOK,
                    new_status="blocked",
                    status_reason=f"An error occurred while executing the repository's ready hook: {exc.error}",
                )
                return False
            finally:
                db.session.commit()
        if did_execute_ready_hook:
            # If we executed the hook, it might have mutated state that we're
            # looking at here. To be safe, we just re-fetch the PR.
            fetch_pr.delay(pr.number, repo.name)
            return False

    # If the PR is in open state, skip rest of the processing to avoid extra API calls.
    if pr.status == "open" and not merge_operation:
        logger.info(
            "Pull request is open and not marked as ready-to-merge, skipping processing"
        )
        return False

    pr_is_marked_blocked = common.has_label(pull, repo.blocked_label)
    gql = _get_graphql(repo)
    pr_codeowner_approved, pr_changes_requested = _update_approval(gql, pr)

    # Only returns False if both the repo is configured to require all review
    # conversations to be resolved AND there are actually unresolved comments
    pr_conv_resolved = is_conv_resolved(gql, repo, pr.number)

    precondition_prs = common.get_pending_preconditions(pr)
    if precondition_prs:
        logger.info(
            "PR has pending preconditions",
            pr_number=pr.number,
            repo_id=pr.repo_id,
            blocking_prs=[pp.number for pp in precondition_prs],
        )
        _transition_pr_not_ready(
            client,
            repo,
            pr,
            pull,
            StatusCode.PENDING_PRECONDITION_PRS,
            status_reason=(
                "Blocked by the following PRs:\n"
                + _format_pr_list_markdown(precondition_prs)
            ),
        )
        return False

    if merge_operation:
        git_user = billing_common.update_git_user(git_user)
        if not git_user.active:
            comments.add_inactive_user_comment.delay(repo.id, pr.id, git_user.id)
            return False

    if not repo.is_valid_base_branch(pull.base.ref) and not stack_manager.is_in_stack(
        pr
    ):
        # If the PR is has a different base branch, we should not change it to pending state, since
        # we would not expect this to ever be merged by MQ.
        if pr.status == "open":
            pr.status_code = StatusCode.UNSUPPORTED_BASE_BRANCH
            db.session.commit()
            if marked_ready:
                client.create_issue_comment(
                    pr.number,
                    f"The base branch ({pull.base.ref}) of this pull request is not configured as a base branch. "
                    f"Please edit the base branch of this PR if you wish to merge using Aviator.",
                )
            logger.info(
                "Pull request base branch is not valid, skipping processing",
                pull_request_base_branch=pull.base.ref,
            )
            return False
        _transition_pr_not_ready(
            client,
            repo,
            pr,
            pull,
            StatusCode.UNSUPPORTED_BASE_BRANCH,
        )
        return False

    # Check if the blocked label was manually removed. If so, we transition from
    # blocked to open (and continue processing below).
    if pr.status == "blocked" and not pr_is_marked_blocked:
        logger.info(
            "Pull request is blocked but label is not present, transitioning to open"
        )
        pr.set_status("open", StatusCode.NOT_QUEUED)

    if marked_ready:
        labeled_activity = Activity(
            repo_id=repo.id,
            name=ActivityType.LABELED,
            pull_request=pr,
            status_code=pr.status_code,
        )
        db.session.add(labeled_activity)
        util.posthog_util.capture_pull_request_event(
            util.posthog_util.PostHogEvent.TRIGGER_PULL_REQUEST,
            pr,
        )
        pilot_data = common.get_pilot_data(pr, "ready")
        hooks.call_master_webhook.delay(
            pr.id,
            "labeled",
            pilot_data=pilot_data,
            ci_map={},
            note="",
            status_code_int=pr.status_code.value,
        )
    elif pr.status in ("queued", "tagged") and not merge_operation:
        # either we received an unlabeled event, or if the PR somehow turned not-ready, trigger dequeue event.
        unlabeled_activity = Activity(
            repo_id=repo.id,
            name=ActivityType.UNLABELED,
            pull_request=pr,
            status_code=pr.status_code,
        )
        db.session.add(unlabeled_activity)
        handle_dequeue_locking(repo, pr)
        pilot_data = common.get_pilot_data(pr, "dequeued")
        hooks.call_master_webhook.delay(
            pr.id,
            "dequeued",
            pilot_data=pilot_data,
            ci_map={},
            note="",
            status_code_int=pr.status_code.value,
        )

    if not merge_operation:
        # Since all the checks above have passed, and the PR isn't marked as
        # ready, it should be in state "open" here.
        logger.info("Pull request is not marked as ready-to-merge, marking as open")
        pr.set_status("open", StatusCode.NOT_QUEUED)
        return False

    # Make sure we don't switch between open and pending
    # open - PR was opened
    # pending - somebody has labeled it queued, but it needs a pre-req (e.g., approval)
    # blocked - was queued but failed
    # FUTURE: We might want to consolidate "pending" and "blocked" into one
    # state (since from the users perspective, they both mean "this thing can't
    # actually be merged yet).

    # Check pre-requisites first.
    skip_validation = _is_skip_validation(pr)
    if pr_changes_requested and not skip_validation:
        _transition_pr_not_ready(
            client,
            repo,
            pr,
            pull,
            StatusCode.CHANGES_REQUESTED,
        )
        return False
    if not pr_codeowner_approved and not skip_validation:
        _transition_pr_not_ready(
            client,
            repo,
            pr,
            pull,
            StatusCode.NOT_APPROVED,
        )
        return False
    if not pr_conv_resolved and not skip_validation:
        _transition_pr_not_ready(
            client,
            repo,
            pr,
            pull,
            StatusCode.UNRESOLVED_CONVERSATIONS,
        )
        return False

    if repo.parallel_mode_config.use_affected_targets and not pr.affected_targets:
        _transition_pr_not_ready(
            client,
            repo,
            pr,
            pull,
            StatusCode.MISSING_AFFECTED_TARGETS,
        )
        return False

    # Check for special cases where we can get out of the blocked state.
    # ignore these optimizations when the PR is stacked to avoid any corner cases.
    if pr.status == "blocked" and not pr.stack_parent_id:
        # Respect max_requeue_attempts setting
        if _pr_has_exceeded_max_requeue_attempts(repo, pr):
            return False

        # We check these cases above, so if we have these status codes we can
        # just switch to ready.
        if pr.status_code in (
            StatusCode.NOT_APPROVED,
            StatusCode.UNRESOLVED_CONVERSATIONS,
        ):
            logger.info(
                "Pull request is blocked but can be automatically transitioned to pending",
                old_status_code=StatusCode(pr.status_code),
            )
            _transition_pr_not_ready(
                client,
                repo,
                pr,
                pull,
                StatusCode.PENDING_MERGEABILITY,
                new_status="pending",
            )
            return True
        # Check if we can get out of a MERGE_CONFLICT case.
        # We check for action == 'synchronize' here because we can't rely only
        # on pull.mergeable (since in batch mode, the merge conflict might have
        # been with a PR before it in the queue).
        elif (
            pr.status_code in (StatusCode.MERGE_CONFLICT, StatusCode.REBASE_FAILED)
            and action == "synchronize"
            and pull.mergeable is not False  # this can be True or None
        ):
            logger.info(
                "Pull request got synchronize event, automatically transitioning to pending",
                old_status_code=pr.status_code,
            )
            _transition_pr_not_ready(
                client,
                repo,
                pr,
                pull,
                StatusCode.PENDING_MERGEABILITY,
                new_status="pending",
            )
            return True

        # Do not requeue the PR with FAILED_TESTS here as that should be handled
        # by requeue_or_comment method. Otherwise this will go into double loop.
        return False

    # PR isn't blocked in DB but is marked blocked with label. This sometimes
    # happens if a PR was blocked, is moved into a draft (so the dev can work on
    # it), then transitioned back to ready (non-draft) state. We lose the
    # information about why the PR was blocked originally.
    if pr_is_marked_blocked:
        _transition_pr_not_ready(
            client,
            repo,
            pr,
            pull,
            StatusCode.BLOCKED_BY_LABEL,
            # Force blocked rather than pending if the blocked label is added
            new_status="blocked",
        )
        return False

    if pr.status != "queued" and pr.status != "tagged" and pr.status != "blocked":
        mergeability_decision = check_mergeability_for_queue(
            client, repo, pull, pr, force_validate=force_validate_mergeability
        )
        if (
            repo.parallel_mode_config.check_mergeability_to_queue
            # Check for mergeability in all modes for consistent behavior.
            # Although in a FIFO / non-queue mode it doesn't make much difference
            # whether PR stays in pending or queued state, it provides consistent
            # behavior if a user switches between modes.
            and not mergeability_decision.mergeable
        ):
            _transition_pr_not_ready(
                client,
                repo,
                pr,
                pull,
                StatusCode.PENDING_MERGEABILITY,
                note=pull.mergeable_state,
                status_reason=mergeability_decision.reason,
            )
            return False
        _transition_pr_to_queued(client, repo, pr, pull)
        return True

    return False


def update_bot_pr_on_pr_merge(
    client: GithubClient,
    repo: GithubRepo,
    pr: PullRequest,
    pull: pygithub.PullRequest,
    bot_pr: BotPr,
) -> bool:
    """
    Check all the PRs in the batch, if all of them have merged, close the bot PR.
    Returns True if bot PR was closed via this method, False otherwise.
    """
    if pr.status_code != StatusCode.MERGED_BY_MQ:
        return False
    if bot_pr.status == "closed":
        return False

    if not all(p.status == "merged" for p in bot_pr.batch_pr_list):
        logger.info(
            "Batch partially merged, ignoring bot PR handling",
            bot_pr_number=bot_pr.number,
            pr_number=pr.number,
            all_prs=[p.number for p in bot_pr.batch_pr_list],
        )
        return False

    logger.info(
        "Batch merged, closing bot PR",
        bot_pr_number=bot_pr.number,
        pr_number=pr.number,
        all_prs=[p.number for p in bot_pr.batch_pr_list],
    )
    bot_pull = client.get_pull(bot_pr.number)
    handle_post_merge(client, repo, pr, pull, bot_pr, bot_pull)
    return True


def ensure_bot_pull_status(
    client: GithubClient, repo: GithubRepo, bot_pull: pygithub.PullRequest
) -> None:
    """
    Ensures that the BotPr status in the db is consistent with the actual bot pull state.
    Handles updating the db status in cases such as the BotPr being closed manually,
        or if it was merged and we failed to receive the GH webhook.
    """
    # if the PR is closed on Github but the BotPR is open in our db, we need to mark it as manually closed.
    bot_pr: BotPr | None = BotPr.query.filter_by(
        repo_id=repo.id, number=bot_pull.number
    ).first()
    if bot_pull.state == "closed" and bot_pr and bot_pr.status != "closed":
        # this handles the case where the bot pull was merged in FF mode and we didn't close the PR (ex. timeout)
        if bot_pull.merged:
            first_pr = bot_pr.batch_pr_list[0]
            if (
                not repo.parallel_mode_config.use_fast_forwarding
                and bot_pr.number != first_pr.number
            ):
                logger.warning(
                    "Repo %d bot pr %d should not be merged if repo is not ff",
                    repo.id,
                    bot_pull.number,
                )
                return
            logger.info(
                "Bot pr %d repo %d already merged successfully",
                bot_pull.number,
                repo.id,
            )
            # if the bot_pr is already merged, make sure to perform update PR actions
            first_pull = client.get_pull(first_pr.number)
            update_prs_post_merge(
                client,
                repo,
                first_pr,
                first_pull,
                bot_pr.batch_pr_list,
                optimistic_botpr=None,
            )
            for pr in bot_pr.batch_pr_list:
                handle_post_merge(
                    client, repo, pr, client.get_pull(pr.number), bot_pr, bot_pull
                )
            return
        bot_pr.set_status("closed", StatusCode.PR_CLOSED_MANUALLY)
        db.session.commit()
        # we do not call _removed_from_batch here because if this causes a queue
        # reset, then the webhook is triggered from the full_queue_reset.

        # if any of the associated PRs are tagged, we should reset the queue
        if any(pr.status == "tagged" for pr in bot_pr.batch_pr_list):
            full_queue_reset(
                repo,
                StatusCode.RESET_BY_PR_MANUALLY_CLOSED,
                # This column is nullable because reasons, but should never happen
                bot_pr.batch_pr_list[0].target_branch_name,
                bot_pr=bot_pr,
            )
        return

    # If all PRs in the batch are already merged/closed, make sure to handle post merge.
    if bot_pr and all(pr.status in ("merged", "closed") for pr in bot_pr.batch_pr_list):
        logger.info(
            "Repo %d batch %s already merged/closed, closing bot pr %d now",
            repo.id,
            bot_pr.batch_pr_list,
            bot_pr.number,
        )
        client.close_pull(bot_pull)
        delete_branch_if_applicable(client, repo, bot_pull)
        client.delete_ref(bot_pull.head.ref)
        bot_pr.set_status("closed", bot_pr.batch_pr_list[0].status_code)
        db.session.commit()


def _assert_pr_preflight_checks(
    access_token: AccessToken, repo: GithubRepo, pr: PullRequest
) -> None:
    """
    Assert that all relevant checks are passed for a PR to be valid.

    These checks are for issues that can be checked as soon as the PR is opened.
    We run more checks when we're trying to queue the PR (e.g., checking that
    the required reviews have been completed).

    :raises errors.PRStatusError
    """
    if repo.require_verified_commits:
        _assert_verified_commits(access_token, repo, pr)


def _queue_process_stack_dependents(
    repo: GithubRepo,
    pr: PullRequest,
) -> None:
    """
    Update all dependent PRs.

    :param repo: The GithubRepo that the PR belongs to.
    :param pr: The PullRequest to update.
    """
    dependents = pr.stack_dependents
    if not dependents:
        return
    logger.info(
        "Repo %d PR %d has %d stack dependents to update",
        repo.id,
        pr.number,
        len(dependents),
    )
    for dependent in pr.stack_dependents:
        update_stack_dependent.delay(repo.id, pr.id, dependent.id)


def _ensure_skip_line(
    repo: GithubRepo,
    pull: pygithub.PullRequest,
    pr: PullRequest,
    action: str | None,
    label: str | None,
) -> None:
    skip_line_label: str | None = (
        repo.skip_line_label.name if repo.skip_line_label else None
    )
    if skip_line_label:
        has_skip_line = common.has_label(pull, skip_line_label)

        if not has_skip_line and pr.skip_line:
            label_is_skip_line = label and label == skip_line_label
            if action == "unlabeled" and label_is_skip_line:
                # The skip label was removed, so continue with setting pr.skip_line.
                pass
            else:
                # skip_line must have been set via slash command in MergeOperation,
                # so do not take any action to unset.
                return

        # Set pr.skip_line to be consistent with the label.
        if has_skip_line != pr.skip_line:
            pr.skip_line = has_skip_line
            db.session.commit()
            update_gh_pr_status.delay(pr.id)


@celery.task
def update_stack_dependent(repo_id: int, pr_id: int, desc_pr_id: int) -> None:
    """
    Update a dependent PR with changes from its stack parent.
    """
    repo = GithubRepo.get_by_id_x(repo_id)
    pr = PullRequest.get_by_id_x(pr_id)
    descendant = PullRequest.get_by_id_x(desc_pr_id)
    if descendant.status == "merged":
        logger.info(
            "Repo %d descendant %d was already merged!",
            repo_id,
            desc_pr_id,
        )
        return

    # Make sure we update all our dependents (asynchronously)
    _queue_process_stack_dependents(repo, descendant)
    update_gh_pr_status.delay(descendant.id)


def _queue_process_stack_ancestors(
    repo: GithubRepo,
    pr: PullRequest,
) -> None:
    """
    Update parent PR.
    :param repo: The GithubRepo that the PR belongs to.
    :param pr: The PullRequest to update.
    """
    parent = pr.stack_parent
    if not parent:
        return
    logger.info(
        "Repo %d PR %d has %s stack parent to update",
        repo.id,
        pr.number,
        parent,
    )
    update_stack_parent.delay(repo.id, pr.id, parent.id)


@celery.task
def update_stack_parent(repo_id: int, pr_id: int, parent_pr_id: int) -> None:
    """
    Update a dependent PR with changes from its stack parent.
    """
    repo: GithubRepo = GithubRepo.get_by_id_x(repo_id)
    pr: PullRequest = PullRequest.get_by_id_x(pr_id)
    parent: PullRequest = PullRequest.get_by_id_x(parent_pr_id)
    if parent.status == "merged":
        logger.info(
            "Repo %d parent %d was already merged!",
            repo_id,
            parent_pr_id,
        )
        return

    # Make sure we update all our parents (asynchronously)
    _queue_process_stack_ancestors(repo, parent)

    # update the parent sticky comment
    # we do this for ancestors and don't fetch_pr to avoid infinite loops
    update_gh_pr_status.delay(parent.id)


def get_requeue_attempts(repo_id: int, pr_id: int) -> int:
    activities: list[Activity] = (
        Activity.query.filter(
            Activity.repo_id == repo_id,
            Activity.pull_request_id == pr_id,
        )
        .order_by(Activity.id.desc())
        .all()
    )
    attempts = 0
    for activity in activities:
        if activity.name is ActivityType.LABELED:
            break
        if activity.name is ActivityType.QUEUED:
            attempts += 1
    return attempts


def _pull_has_passed_required_tests(
    client: GithubClient,
    repo: GithubRepo,
    pull: pygithub.PullRequest,
    pr_id: int,
) -> bool:
    details = checks.fetch_latest_test_statuses(
        client,
        repo,
        pull,
        botpr=False,
    )
    update_gh_pr_status.delay(pr_id)
    return details.result != "failure"


def set_pr_as_closed_or_merged(
    client: GithubClient,
    pr: PullRequest,
    merged: bool,
    merged_at: datetime.datetime | None,
    merged_by_login: str | None,
    merge_commit_sha: str | None = None,
    set_bot_pr_failure: bool = True,
    lock: bool = False,
) -> StatusCode:
    """
    Sets the PR status if it has been closed. This method may acquire a lock twice.
    First to update the PR object and later to update botPR.

    :param set_bot_pr_failure: If true, this will update the associated BotPr as well.
    :param lock: If true, this will lock the repo to prevent race conditions.
    """
    if pr.status in ("merged", "closed"):
        return pr.status_code

    old_status = pr.status
    if lock:
        with locks.for_repo(pr.repo):
            _sync_pr_status_on_close_or_merge(
                pr, merged, merged_at, merged_by_login, merge_commit_sha
            )
    else:
        _sync_pr_status_on_close_or_merge(
            pr, merged, merged_at, merged_by_login, merge_commit_sha
        )

    if not set_bot_pr_failure or pr.status_code == StatusCode.MERGED_BY_MQ:
        return pr.status_code

    if set_bot_pr_failure and old_status == "tagged":
        # This means there's a bot PR as well. We need to flag failure here.
        if lock:
            bot_pr_set_failure_locking(client, pr.repo, pr, pr.status_code)
        else:
            bot_pr_set_failure(client, pr.repo, pr, pr.status_code)

    if pr.merged_at and pr.merge_commit_sha is None:
        logger.error("Missing merge commit SHA for a merged PR", pr_number=pr.number)
    return pr.status_code


def _sync_pr_status_on_close_or_merge(
    pr: PullRequest,
    merged: bool,
    merged_at: datetime.datetime | None,
    merged_by_login: str | None,
    merge_commit_sha: str | None,
) -> None:
    """
    Updates the internal state of the PR to match the state of the PR on GitHub.
    This does not make any API calls to GitHub.
    """
    if not merged:
        pr.set_status("closed", StatusCode.PR_CLOSED_MANUALLY)
    else:
        pr.merged_at = merged_at
        if pr.merge_commit_sha is None:
            pr.merge_commit_sha = merge_commit_sha
        if merged_by_login and common.is_bot_user(pr.account_id, merged_by_login):
            util.posthog_util.capture_pull_request_event(
                util.posthog_util.PostHogEvent.MERGE_PULL_REQUEST,
                pr,
            )
            pr.set_status("merged", StatusCode.MERGED_BY_MQ)
            # should not report failure if it was merged by MQ.
        else:
            util.posthog_util.capture_pull_request_event(
                util.posthog_util.PostHogEvent.MERGE_PULL_REQUEST_NO_MQ,
                pr,
            )
            pr.set_status("merged", StatusCode.MERGED_MANUALLY)
            logger.info(
                "PR manually merged",
                merged_by=merged_by_login or "unknown",
            )
    db.session.commit()
    flexreview.celery_tasks.process_merged_pull.delay(pr.id)
    atc.non_released_prs.update_non_released_prs.delay(pr.repo_id)


def handle_dequeue_locking(repo: GithubRepo, pr: PullRequest) -> None:
    with locks.for_repo(repo):
        # refresh the PR as it might be stale
        # https://docs.sqlalchemy.org/en/13/orm/session_state_management.html#when-to-expire-or-refresh
        db.session.refresh(pr)
        db.session.refresh(repo)
        logger.info("Repo %d PR %d dequeue detected", repo.id, pr.number)
        if repo.parallel_mode and pr.status != "tagged":
            logger.info(
                f"Repo {repo.id} PR {pr.number} was marked for dequeue but not tagged: {pr.status}"
            )
            return
        elif repo.parallel_mode and pr.status == "tagged":
            associated_bot_pr = pr.latest_bot_pr
            if not associated_bot_pr:
                # we should still dequeue the PR so we do not merge it.
                logger.error(f"No bot PR found for tagged PR ID {pr.id}")
            else:
                for batch_pr in associated_bot_pr.batch_pr_list:
                    # requeue all other PRs in the batch that were not dequeued
                    if batch_pr.id != pr.id:
                        batch_pr.set_status("queued", StatusCode.QUEUED)

            pr.set_status("open", StatusCode.NOT_QUEUED)
            db.session.commit()

            # now we need to reset all existing bot-PRs
            bot_prs: list[BotPr]
            if repo.parallel_mode_config.use_affected_targets:
                bot_prs = target_manager.get_dependent_bot_prs(repo, [pr.id])
                # Also add this botPR itself. This won't be returned in the caller
                # because we have already changed the status of the PR.
                if associated_bot_pr:
                    bot_prs.append(associated_bot_pr)
            else:
                associated_bot_pr_id = associated_bot_pr.id if associated_bot_pr else 0
                bot_prs = (
                    BotPr.query.filter_by(status="queued", repo_id=repo.id)
                    .join(PullRequest, BotPr.pull_request_id == PullRequest.id)
                    .filter(PullRequest.target_branch_name == pr.target_branch_name)
                    .filter(BotPr.id >= associated_bot_pr_id)
                    .order_by(BotPr.id.asc())
                    .all()
                )
            if repo.parallel_mode_config.use_parallel_bisection:
                # Exclude the bisected PRs.
                bot_prs = [bp for bp in bot_prs if not bp.is_bisected_batch]

            requeued_pr_numbers: list[int] = []
            bot_prs_to_close: list[int] = []
            for bot_pr in bot_prs:
                for batch_pr in bot_pr.batch_pr_list:
                    if pr.number != batch_pr.number:
                        batch_pr.set_status("queued", StatusCode.QUEUED)
                        requeued_pr_numbers.append(batch_pr.number)
                        activity.create_activity(
                            repo_id=repo.id,
                            pr_id=batch_pr.id,
                            activity_type=ActivityType.RESYNC,
                            status_code=StatusCode.RESET_BY_DEQUEUE,
                            payload=ActivityResyncPayload(
                                triggering_pr_id=pr.id,
                            ),
                        )
                if bot_pr.pull_request.number != bot_pr.number:
                    bot_prs_to_close.append(bot_pr.number)
                bot_pr.set_status("closed", StatusCode.RESET_BY_DEQUEUE)
            db.session.commit()

            _record_reset_event(
                repo.id,
                StatusCode.RESET_BY_DEQUEUE,
                pr,
                reset_count=len(requeued_pr_numbers),
                closed_bot_pr_count=len(bot_prs_to_close),
            )
            target_manager.refresh_dependency_graph_on_reset(repo, [pr])
            for bp in bot_prs:
                _removed_from_batch(bp.batch_pr_list)
            logger.info(
                "Dequeue complete",
                requeued_pr_numbers=requeued_pr_numbers,
                closed_bot_pr_numbers=bot_prs_to_close,
            )
            if bot_prs_to_close:
                close_pulls.delay(
                    repo.id,
                    bot_prs_to_close,
                    close_reason=StatusCode.RESET_BY_DEQUEUE.message(),
                )
            tag_queued_prs_async.delay(repo.id)


def requeue_pr_if_approved(
    client: GithubClient,
    repo: GithubRepo,
    pull: pygithub.PullRequest,
    pr: PullRequest,
) -> bool:
    reqd_approvers = [u.username for u in repo.required_users]
    if not client.is_approved(
        pull, repo.preconditions.number_of_approvals, reqd_approvers
    ):
        logger.info("Repo %d PR %d requeue failed", repo.id, pr.number)
        return False
    logger.info(
        "Requeuing PR repo %d PR %d, moving to pending state", repo.id, pr.number
    )
    if repo.parallel_mode_config.update_before_requeue:
        client.update_pull(pull)
    _transition_pr_not_ready(
        client,
        repo,
        pr,
        pull,
        StatusCode.PENDING_MERGEABILITY,
        new_status="pending",
    )
    return True


def _requeue_pr_if_ci_updated(
    client: GithubClient,
    repo: GithubRepo,
    pull: pygithub.PullRequest,
    pr: PullRequest,
) -> bool:
    details = checks.fetch_latest_test_statuses(
        client,
        repo,
        pull,
        botpr=False,
    )
    update_gh_pr_status.delay(pr.id)
    if details.result != "failure":
        logger.info(
            "Repo %d PR %d status passed %s, trying requeue",
            repo.id,
            pr.number,
            pull.base.ref,
        )
        return requeue_pr_if_approved(client, repo, pull, pr)
    return False


def process_all(repo: GithubRepo) -> None:
    logger.info("Repo %d start process unordered", repo.id)
    access_token, client = common.get_client(repo)
    if not client:
        return

    pr_list = (
        PullRequest.query.filter_by(repo_id=repo.id)
        .filter(PullRequest.status.in_(["queued", "tagged"]))
        .order_by(PullRequest.queued_at)
        .all()
    )
    if not pr_list:
        logger.info("nothing to process")
        return

    for pr in pr_list:
        try:
            review_pr_for_merge(access_token, repo, client, pr)
        except errors.GithubMergeabilityPendingException:
            logger.info("Repo %d PR %d github mergeability pending", repo.id, pr.number)
            review_pr_async.delay(pr.id)
        time.sleep(0.5)


def process_top_fifo_for_branch(repo: GithubRepo, branch_name: str) -> None:
    logger.info(
        "Repo %d start process top fifo with branch_name %s", repo.id, branch_name
    )
    process_emergency_merge_prs(repo, branch_name)
    pr = common.get_top_pr(repo.id, branch_name)

    if not pr:
        logger.info("Repo %d nothing to process", repo.id)
        return

    # For stacked PRs, we need to find the highest queued PR.
    # The other PRs will be closed with the merged-by-mq label.
    if stack_manager.is_in_stack(pr):
        ready_stack = stack_manager.get_ready_stack_after_pr(pr)
        pr = ready_stack[-1]

    if top_changed(repo.id, pr.number):
        pilot_data = common.get_pilot_data(pr, "top_of_queue")
        hooks.call_master_webhook.delay(
            pr.id,
            "top_of_queue",
            pilot_data=pilot_data,
            ci_map={},
            note="",
            status_code_int=pr.status_code.value,
        )
        pr.last_processed = time_util.now()

    if can_skip_process_top(pr.id):
        logger.info("Repo %d PR %d skipping process top", repo.id, pr.number)
        return

    access_token, client = common.get_client(repo)
    if not client:
        return

    try:
        result_pr = review_pr_for_merge(access_token, repo, client, pr)
    except errors.GithubMergeabilityPendingException as e:
        logger.info("Repo %d PR %d github mergeability pending", repo.id, pr.number)
        result_pr = None
    if result_pr and result_pr.status in ["merged", "blocked", "closed"]:
        # this PR process finished, process top again.
        process_top_async.delay(repo.id, branch_name)

    update_gh_pr_status.delay(pr.id)


def review_pr_for_merge(
    access_token: AccessToken,
    repo: GithubRepo,
    client: GithubClient,
    pr: PullRequest,
) -> PullRequest | None:
    with locks.for_pr(pr):
        return review_pr_for_merge_blocking(access_token, repo, client, pr)


def _get_all_target_branches(repo: GithubRepo) -> set[str]:
    """
    Return the set of all active target branches for a repo.
    """
    prs: list[PullRequest] = (
        PullRequest.query.filter_by(repo_id=repo.id)
        .filter(PullRequest.status.in_(["queued", "tagged"]))
        .all()
    )
    if repo.parallel_mode:
        bot_prs: list[BotPr] = (
            BotPr.query.filter_by(repo_id=repo.id, status="queued")
            .options(joinedload(BotPr.batch_pr_list))
            .all()
        )
        for bot_pr in bot_prs:
            prs.extend(bot_pr.batch_pr_list)

    # For very old data, target_branch_name may be null
    return {pr.target_branch_name for pr in prs if pr.target_branch_name}


def review_pr_for_merge_blocking(
    access_token: AccessToken,
    repo: GithubRepo,
    client: GithubClient,
    pr: PullRequest,
    *,
    optimistic_botpr: BotPr | None = None,
) -> PullRequest | None:
    """
    Verify relevant pre-merge checks and then merge the PR.
    """
    # refresh the PR as it might be stale
    # https://docs.sqlalchemy.org/en/13/orm/session_state_management.html#when-to-expire-or-refresh
    db.session.refresh(pr)
    if pr.status not in ["queued", "tagged"]:
        logger.info(
            "Repo %d PR %d skipping PR as status has changed", repo.id, pr.number
        )
        return pr
    pull = client.get_pull(pr.number)
    if common.is_av_bot_pr(
        account_id=repo.account_id,
        author_login=pull.user.login,
        title=pull.title,
    ):
        logger.warning("Found bot owner PR to process %d %d", repo.id, pr.number)
        pr.deleted = True
        db.session.commit()
        return None

    reqd_approvers = [u.username for u in repo.required_users]
    gql = graphql.GithubGql(access_token.token, access_token.account_id)

    if pr.head_commit_sha != pull.head.sha:
        pr.head_commit_sha = pull.head.sha

    # Make sure that pull request is still queued before continuing.
    merge_operation = _get_ready_merge_operation(pr)
    if not merge_operation:
        mark_as_blocked(client, repo, pr, pull, StatusCode.QUEUE_LABEL_REMOVED)
        return pr

    if common.has_label(pull, repo.blocked_label):
        mark_as_blocked(client, repo, pr, pull, StatusCode.BLOCKED_BY_LABEL)
        return pr

    if not merge_operation.skip_validation and not client.is_approved(
        pull, repo.preconditions.number_of_approvals, reqd_approvers
    ):
        mark_as_blocked(client, repo, pr, pull, StatusCode.NOT_APPROVED)
        return pr
    if not merge_operation.skip_validation and not is_conv_resolved(
        gql, repo, pr.number
    ):
        mark_as_blocked(client, repo, pr, pull, StatusCode.UNRESOLVED_CONVERSATIONS)
        return pr
    # This is checked before entering the queue, but checking here again in case
    # the title and body have changed since them.
    validation_issues = core.custom_validations.check_custom_validations(repo, pr)
    if validation_issues:
        mark_as_blocked(client, repo, pr, pull, StatusCode.CUSTOM_VALIDATIONS_FAILED)

    if pull.state == "closed":
        if pull.merged:
            util.posthog_util.capture_pull_request_event(
                util.posthog_util.PostHogEvent.MERGE_PULL_REQUEST_NO_MQ,
                pr,
            )
            pr.set_status("merged", StatusCode.MERGED_MANUALLY)
            pr.merged_at = pull.merged_at
            pr.merge_commit_sha = pull.merge_commit_sha
        else:
            pr.set_status("closed", StatusCode.PR_CLOSED_MANUALLY)
        db.session.commit()
        flexreview.celery_tasks.process_merged_pull.delay(pr.id)
        atc.non_released_prs.update_non_released_prs.delay(pr.repo_id)
        send_pr_update_to_channel.delay(pr.id)

        if pr.merged_at and pr.merge_commit_sha is None:
            logger.error(
                "Missing merge commit SHA for a merged PR", pr_number=pr.number
            )
        return pr

    try:
        if repo.update_latest and not repo.is_no_queue_mode:
            client, result = sync.merge_or_rebase_head(
                repo,
                client,
                pull,
                pr.target_branch_name,
            )
            # comment_on_latest was a config used to post a comment in default sequential mode if the PR
            # is already up-to-date. There is no way to set this property today, but it has been set to True
            # for a handful of users. We have deprecated this config but only continue to use it here.
            if repo.DEPRECATED_comment_on_latest and (
                not result or should_post(repo.id, pr.number, "pull_head")
            ):
                pull.create_issue_comment("Branch up-to-date, waiting for checks.")
            if not result:
                pr.merge_base_count += 1
                db.session.commit()
                logger.info(
                    "Repo %d PR %d merged %s, will circle back",
                    repo.id,
                    pr.number,
                    pull.base.ref,
                )
                return pr
    except errors.PRStatusException as e:
        logger.info("Repo %d PR %d merge/rebase conflict %s", repo.id, pr.number, e)
        mark_as_blocked(client, repo, pr, pull, e.code, note=e.message)
        return pr

    mergeability_result = check_mergeability_for_merge(client, repo, pr, pull)
    if not mergeability_result.test_result == "success":
        return pr
    _did_merge = merge_pr(client, repo, pr, pull, optimistic_botpr=optimistic_botpr)
    return pr


@dataclasses.dataclass(frozen=True)
class MergeMergeabilityResult:
    test_result: checks.TestResult
    ci_map: dict[str, str]
    status_code: StatusCode | None


def check_mergeability_for_merge(
    client: GithubClient,
    repo: GithubRepo,
    pr: PullRequest,
    pull: pygithub.PullRequest,
    mark_blocked: bool = True,
) -> MergeMergeabilityResult:
    """
    Checks if the given PR is mergeable.

    This check is intended to be called on PR's that will actually be merged
    (see ``check_mergeability_for_queue`` to check whether or not a PR passes
    the mergeability criterion to be queued in parallel mode).

    :param mark_blocked: If true, the PR will be marked as blocked in the case of failure.
    """
    ci_map: dict[str, str] = {}
    # If use_github_mergeability is set, try using the state first, otherwise fallback
    # to Github required tests.
    if (
        pull.mergeable_state == "clean"
        and pull.mergeable
        and repo.preconditions.use_github_mergeability
    ):
        return MergeMergeabilityResult(
            test_result="success", ci_map=ci_map, status_code=None
        )
    if pull.mergeable_state == "dirty":
        logger.info(
            "Repo %d PR %d cannot be merged. merge conflict %s",
            repo.id,
            pr.number,
            pull.mergeable_state,
        )
        if mark_blocked:
            mark_as_blocked(client, repo, pr, pull, StatusCode.MERGE_CONFLICT)
        return MergeMergeabilityResult(
            test_result="failure", ci_map=ci_map, status_code=StatusCode.MERGE_CONFLICT
        )
    # If we  cannot tell mergeability at the time of merging the PR, we will ignore
    # the check and still try to merge the PR. In case of a failure to merge, we will then handle
    # the failure gracefully.
    if pull.mergeable is None or pull.mergeable_state == "unknown":
        logger.info(
            "Repo %d PR %d is pending mergeability but still proceeding, state: %s",
            repo.id,
            pr.number,
            pull.mergeable_state,
        )
    if pull.mergeable is False:
        # This should not happen as we have already checked for merge conflict above.
        logger.error(
            "PR is not mergeable but does not have a merge conflict",
            repo_id=repo.id,
            pr_number=pr.number,
            mergeable_state=pull.mergeable_state,
        )
        return MergeMergeabilityResult(
            test_result="failure", ci_map=ci_map, status_code=StatusCode.MERGE_CONFLICT
        )

    if _is_skip_validation(pr):
        # In case of skip_validation we will bypass the validation of original PR.
        # Here, this works very similar to the emergency merge setup.
        logger.info(
            "Skip CI PR found while checking mergeability to merge",
            pr_number=pr.number,
            repo_id=repo.id,
        )
        return MergeMergeabilityResult(
            test_result="success", ci_map={}, status_code=None
        )
    result, ci_map = check_ci_status(client, repo, pr, pull, mark_blocked=mark_blocked)
    status_code = StatusCode.FAILED_TESTS if result == "failure" else None
    return MergeMergeabilityResult(
        test_result=result, ci_map=ci_map, status_code=status_code
    )


def check_ci_status(
    client: GithubClient,
    repo: GithubRepo,
    pr: PullRequest,
    pull: pygithub.PullRequest,
    *,
    mark_blocked: bool,
) -> tuple[checks.TestResult, dict[str, str]]:
    test_summary = checks.fetch_latest_test_statuses(
        client,
        repo,
        pull,
        botpr=False,
    )
    logger.info(
        "Checking pull request CI status",
        repo_id=repo.id,
        pr_number=pr.number,
        mergeable_state=pull.mergeable_state,
        commit_sha=pull.head.sha,
        test_result=test_summary.result,
        test_result_reason=test_summary.reason,
        pending_tests=list(test_summary.pending_urls.keys()),
        failed_tests=list(test_summary.failure_urls.keys()),
    )
    update_gh_pr_status.delay(pr.id)
    if test_summary.result == "failure":
        if mark_blocked:
            mark_as_blocked(
                client,
                repo,
                pr,
                pull,
                StatusCode.FAILED_TESTS,
                test_summary.check_urls,
            )
        return "failure", test_summary.failure_urls
    elif test_summary.result == "pending":
        pending = list(test_summary.pending_urls.keys())
        logger.info("Repo %d PR %d has pending tests: %s", repo.id, pr.number, pending)
        if not check_pr_ci_within_timeout(client, repo, pull, pr) and mark_blocked:
            mark_as_blocked(
                client,
                repo,
                pr,
                pull,
                StatusCode.CI_TIMEDOUT,
                status_reason=(
                    "These checks were still pending when the timeout occurred:\n"
                    + _format_ci_list_markdown(pending)
                ),
            )
        return "pending", test_summary.check_urls
    elif test_summary.result == "success":
        return "success", test_summary.check_urls
    else:
        TE.assert_never(test_summary.result)
        # assert_never also raises an assertion error, but mypy still complains
        # that we need to return something if we omit this. :shrug:
        # Probably an issue with using typing_extensions instead of a more
        # recent Python version.
        assert False, "unreachable"


def _assert_verified_commits(
    access_token: AccessToken,
    repo: GithubRepo,
    pr: PullRequest,
) -> None:
    """
    Asserts whether or not the commits in the PR are verified.

    :param access_token:
    :param repo:
    :param pr:
    :raises PRStatusException: If the assertion failed.
    """
    gql = _get_graphql(repo)
    try:
        commits = gql.get_pr_commits_info(repo.name, pr.number)
    except Exception as exc:
        if common.is_network_issue(exc):
            raise exc
        logger.error(
            "Failed to get commits info",
            repo_id=repo.id,
            pr_number=pr.number,
            exc_info=exc,
        )
        raise errors.PRStatusException(
            StatusCode.FAILED_UNKNOWN,
            "Failed to get commits data from GitHub",
        )
    if not commits:
        raise errors.PRStatusException(
            StatusCode.FAILED_UNKNOWN,
            "Unable to load commit list from GitHub",
        )

    error_messages = []
    for commit in commits:
        if not commit.verified:
            error_messages.append(f" * Commit `{commit.oid_short}` is not verified")
    if error_messages:
        raise errors.PRStatusException(
            StatusCode.UNVERIFIED_COMMITS,
            "\n" + ("\n".join(error_messages)),
        )


def mark_as_blocked(
    client: GithubClient,
    repo: GithubRepo,
    pr: PullRequest,
    pull: pygithub.PullRequest,
    status_code: StatusCode,
    ci_map: dict[str, str] = {},
    *,
    note: str | None = "",
    status_reason: str | None = None,
) -> None:
    db.session.refresh(pr)
    if pr.status in ("merged", "closed"):
        logger.info(
            "PR is already merged or closed, ignoring the block workflow",
            repo_id=repo.id,
            pr_number=pr.number,
            status=pr.status,
        )
        return
    common.set_pr_blocked(pr, status_code, status_reason)
    db.session.commit()
    client.add_label(pull, repo.blocked_label)
    mark_ancestor_prs_blocked_by_top(client, pr)

    # when pr has failed tests, a separate message is sent
    if status_code != StatusCode.FAILED_TESTS:
        notify_pr_blocked.delay(pr.id)
    pilot_data = common.get_pilot_data(pr, "blocked")
    hooks.call_master_webhook.delay(
        pr.id,
        "blocked",
        pilot_data=pilot_data,
        ci_map=ci_map,
        note=note,
        status_code_int=status_code.value,
    )
    update_gh_pr_status.delay(pr.id)

    # We're in a weird uncanny valley between note/status_reason.
    # We should eliminate reason and note and replace it with just status_reason.
    comments.post_pull_comment.delay(
        pr.id,
        "blocked",
        ci_map=ci_map,
        note=note or status_reason,
    )


def _check_for_reset_in_lock(repo: GithubRepo) -> bool:
    """
    This job is only querying the DB state for checking if the queue
    should be reset. It only makes API calls if the queue is actually reset.
    """
    if not repo.parallel_mode:
        return False
    for target_branch in _get_all_target_branches(repo):
        non_skip_pr: int | None = None
        bot_pr_list = _get_queued_bot_prs(repo, target_branch)
        for bp in bot_pr_list:
            if not bp.pull_request.skip_line:
                non_skip_pr = bp.pull_request.number
            if (
                non_skip_pr
                and bp.pull_request.skip_line
                and target_manager.should_reset(repo, bp.pull_request)
            ):
                # found a skip line after a regular PR.
                logger.info(
                    "Repo %d skip line PR %d found after non-skip PR %d",
                    repo.id,
                    bp.pull_request.number,
                    non_skip_pr,
                )
                partial_queue_reset_by_skip_line_pr(repo, bp.pull_request)
                return True
    return False


def tag_queued_prs_locking(repo: GithubRepo) -> None:
    with locks.for_repo(repo):
        db.session.refresh(repo)
        _check_for_reset_in_lock(repo)
        if not repo.parallel_mode:
            logger.info("Repo %d not in parallel mode, aborting tagging", repo.id)
            return
        should_call_again_delay = tag_queued_prs(repo)
    # Check outside the lock if we need to call this again.
    if should_call_again_delay:
        tag_queued_prs_async.with_countdown(
            int(should_call_again_delay.total_seconds()),
        ).delay(repo.id)


def tag_queued_prs(repo: GithubRepo) -> datetime.timedelta | None:
    """
    Find any existing queued PRs and see if those can be tagged. It does not perform
    any operation if no queued PRs are found.

    Returns a timedelta if the function should be called again.
    """
    logger.info("Checking for pull requests to tag in parallel mode")
    prs = _get_prs_to_tag(repo)
    if not prs:
        logger.info("Repo has no pull requests to tag")
        return None

    has_reached_parallel_mode_cap, prs = _has_reached_parallel_mode_cap(repo, prs)
    if has_reached_parallel_mode_cap:
        return None

    if _has_a_blocking_queued_pr(repo):
        logger.info("Repo has a blocking PR in the queue, aborting tagging")
        return None

    # do pre-validation before validation to avoid unnecessary api calls
    has_exceeded, remaining = has_exceeded_wait_time(repo, prs=prs)
    if not has_exceeded:
        # we should only proceed if there is a skip line or enough PRs for a full batch
        contains_skip = any(p.skip_line for p in prs)
        if not contains_skip and len(prs) < repo.batch_size:
            logger.info(
                "Repo is not yet ready to tag pull requests (waiting for batch wait window before tagging)",
                n_queued_prs=len(prs),
            )
            if not remaining:
                remaining = datetime.timedelta(seconds=1)
            else:
                remaining += datetime.timedelta(seconds=1)
            return remaining

    access_token, client = common.get_client(repo)
    reqd_approvers = [u.username for u in repo.required_users]

    logger.info(
        "Repo found queued PRs in parallel mode",
        n_queued_prs=len(prs),
    )
    try:
        target_branch_to_prs: dict[str, list[PullRequest]] = {}
        skip_line_prs: list[PullRequest] = []
        for pr in prs:
            with pr.bound_contextvars():
                if not validate_parallel_mode_pr(client, repo, pr, reqd_approvers):
                    continue
                if stack_manager.is_in_stack(pr):
                    # If the PR is part of a stack, see if the stack is properly queued and
                    # if this is the top most PR.
                    # TODO(ankit): This should now be unnecessary. Leaving it here to ensure
                    #  existing queued stacked PRs don't misbehave.
                    if not stack_manager.is_stack_queued(pr):
                        logger.info("Skipping stacked PR, stack is not queued")
                        continue

                    if not stack_manager.is_top_queued_pr(pr):
                        logger.info("Skipping stacked PR as it's not the top queued PR")
                        continue

                # skip line PRs should NOT be included in other batches
                if pr.skip_line:
                    skip_line_prs.append(pr)
                elif pr.target_branch_name in target_branch_to_prs:
                    target_branch_to_prs[pr.target_branch_name].append(pr)
                else:
                    target_branch_to_prs[pr.target_branch_name] = [pr]

        # skip_line PRs are for critical changes so should be in their own batch
        if skip_line_prs:
            logger.info(
                "Found skip line prs",
                n_skip_line_prs=len(skip_line_prs),
                skip_line_pr_numbers=[pr.number for pr in skip_line_prs],
            )
            for skip_line_pr in skip_line_prs:
                batch = Batch(client, [skip_line_pr], skip_line=True)
                tag_new_batch(client, repo, batch)
                # return here so we can release the lock
                return datetime.timedelta(seconds=1)

        if not target_branch_to_prs:
            return None

        # PRs should only be in the same batch if they have the same base branch
        for target_branch_name in target_branch_to_prs:
            prs_to_tag = target_branch_to_prs[target_branch_name]
            sorted_by_batch = sort_by_batch_id(prs_to_tag)
            create_batches(client, repo, sorted_by_batch)
    except errors.ResetException as exc:
        logger.info(
            "Repo reset triggered",
            exc_info=exc,
        )
    except errors.InvalidGithubStateException as exc:
        logger.info(
            "Got invalid GitHub state, will retry tagging...",
            exc_info=exc,
        )
    except errors.PRStatusException:
        logger.info("Restarting batch creation after a merge conflict was detected")

    # In the end returning true to handle scenario where
    # new PRs that have been added since the last call.
    return datetime.timedelta(seconds=1)


def _get_prs_to_tag(repo: GithubRepo) -> list[PullRequest]:
    return db.session.scalars(
        sa.select(PullRequest)
        .where(
            PullRequest.status == "queued",
            PullRequest.repo_id == repo.id,
            PullRequest.deleted.is_(False),
        )
        .order_by(PullRequest.skip_line.desc(), PullRequest.queued_at.asc()),
    ).all()


def create_batches(
    client: GithubClient,
    repo: GithubRepo,
    sorted_by_batch: dict[str, list[PullRequest]],
) -> None:
    """
    Creates and tag batches. New PRs are batched in groups of repo.parallel_mode_config.batch_size.
    PRs that have been queued previously (and have the same latest_bot_pr) are batched in groups of repo.parallel_mode_config.batch_size/2.
    We prioritize the largest batches first to improve throughput.
    """
    logger.info("Sorted batches count #############", sorted_by_batch=sorted_by_batch)
    new_prs = sorted_by_batch[NEW_BATCH_KEY]
    new_batches: list[Batch] = []
    bisected_batches: list[Batch] = []
    if new_prs:
        new_batches = create_new_batches(client, repo, new_prs)
    for batch_id in sorted_by_batch:
        if batch_id != NEW_BATCH_KEY:
            # create bisected batches
            # PRs that have reached this state have already been tagged before. We don't need to sort them again
            # (in the case of target_mode, PRs with the same affected targets will have the same botpr_id).
            batch_prs = sorted_by_batch[batch_id]
            logger.info(
                "Creating bisected batches",
                repo_id=repo.id,
                batch_prs=batch_prs,
            )
            if batch_prs:
                bisected_batches.append(Batch(client, batch_prs))

    bisected_batches.sort(key=lambda b: len(b.prs), reverse=True)

    # we do not want to sort the new batches as they are already sorted.
    # instead we check if the bisected batches should go somewhere in the middle of new batches.
    # We will only create one new batch a time to avoid holding lock for too long.
    # But we will let bisected batches go through.
    for batch in new_batches:
        while len(bisected_batches) > 0:
            bisected = bisected_batches[0]
            if len(bisected.prs) > len(batch.prs):
                tag_new_batch(client, repo, bisected)
                bisected_batches.pop(0)
            else:
                break
        tag_new_batch(client, repo, batch)
        break
    if len(new_batches) > 1:
        # If we have multiple batches, let's try to go over time in additional cycles
        return

    if bisected_batches:
        for batch in bisected_batches:
            tag_new_batch(client, repo, batch)


def create_new_batches(
    client: GithubClient,
    repo: GithubRepo,
    prs_to_tag: list[PullRequest],
) -> list[Batch]:
    """
    Used to create batches for newly queued PRs.
    """
    sorted_prs: list[list[PullRequest]] = [prs_to_tag]
    if repo.parallel_mode_config.use_affected_targets and repo.batch_size > 1:
        sorted_prs = target_manager.sort_for_batching(prs_to_tag)

    batches: list[Batch] = []
    for pr_list in sorted_prs:
        for i in range(0, len(pr_list), repo.batch_size):
            batched_prs = pr_list[i : i + repo.batch_size]

            # if not a full batch, check the wait time
            has_exceeded, remaining = has_exceeded_wait_time(repo, prs=batched_prs)
            if len(batched_prs) < repo.batch_size and not has_exceeded:
                logger.info(
                    "Waiting for a full batch",
                    remaining=remaining,
                    repo_id=repo.id,
                    batched_prs=batched_prs,
                )
                break

            # create batch
            batches.append(Batch(client, batched_prs))
    return batches


def sort_by_batch_id(prs: list[PullRequest]) -> dict[str, list[PullRequest]]:
    # PRs without a batch ID will default to NEW_BATCH_KEY
    sorted_batch: dict[str, list[PullRequest]] = {NEW_BATCH_KEY: []}
    for pr in prs:
        if pr.merge_operations and pr.merge_operations.is_bisected:
            if not pr.merge_operations.bisected_batch_id:
                logger.error(
                    "PR requested bisection but has no batch ID",
                    repo_id=pr.repo.id,
                    pr_number=pr.number,
                    status_code=pr.status_code,
                )
                sorted_batch[NEW_BATCH_KEY].append(pr)
                continue
            if pr.merge_operations.bisected_batch_id not in sorted_batch:
                sorted_batch[pr.merge_operations.bisected_batch_id] = [pr]
            else:
                sorted_batch[pr.merge_operations.bisected_batch_id].append(pr)
        else:
            sorted_batch[NEW_BATCH_KEY].append(pr)
    return sorted_batch


def _has_reached_parallel_mode_cap(
    repo: GithubRepo,
    prs: list[PullRequest],
) -> tuple[bool, list[PullRequest]]:
    """
    Checks if the queue has reached repo.parallel_mode_config.max_parallel_builds.
    If there are skip_line PRs, we should continue and tag those regardless of the cap.
    Otherwise, we wait until the queue is no longer at the max_parallel_builds length.

    :return (bool, list of PRs): True if the cap is reached and we should not continue.
        False if we should continue with tagging.
        The list of PRs contains the PRs that we should tag.
    """
    all_bot_prs: list[BotPr] = (
        BotPr.query.filter_by(status="queued", repo_id=repo.id)
        .order_by(BotPr.id.desc())
        .all()
    )

    prs_to_process = _check_max_parallel_paused_builds(repo, prs)

    if (
        repo.parallel_mode_config.max_parallel_builds
        and not repo.parallel_mode_config.use_affected_targets
    ):
        has_capacity = False
        bot_prs_by_target_branch = collections.Counter(
            [bp.target_branch_name for bp in all_bot_prs]
        )
        target_branches = {pr.target_branch_name for pr in prs}

        # Now check if any of the target branch have capacity to queue.
        # If we also have a max bisected builds set, we should account for that as well.
        max_parallel_builds = repo.parallel_mode_config.max_parallel_builds
        if repo.parallel_mode_config.max_parallel_bisected_builds:
            max_parallel_builds += (
                repo.parallel_mode_config.max_parallel_bisected_builds
            )
        for branch in target_branches:
            if bot_prs_by_target_branch.get(branch, 0) < max_parallel_builds:
                has_capacity = True

        if not has_capacity:
            # If max_parallel_builds is reached, we should still queue skip_line PRs.
            skip_lines = [p for p in prs_to_process if p.skip_line]
            if skip_lines:
                prs_to_process = skip_lines
                return False, prs_to_process
            else:
                # this initial check is to avoid excessive logs and queries.
                # note that the check in `tag_new_batches` is still required.
                _handle_queue_capacity_reached(repo, prs_to_process, len(all_bot_prs))
                return True, []
    return False, prs_to_process


def _has_a_blocking_queued_pr(repo: GithubRepo) -> bool:
    """
    Checks if there is a blocking PR / barrier PR in the queue.
    This one does not account for skip line pending PRs and also
    does not handle the case of split queue in target mode.
    """
    blocking_pr_ct: int = BotPr.query.filter_by(
        status="queued", repo_id=repo.id, is_blocking=True
    ).count()
    return blocking_pr_ct > 0


def _check_max_parallel_paused_builds(
    repo: GithubRepo, prs: list[PullRequest]
) -> list[PullRequest]:
    """
    Ensures that we respect max_parallel_paused_builds if it has been set.
    max_parallel_paused_builds must be less than max_parallel_builds.

        If None, there is no change in behavior - we count all PRs in calculating queue depth.
        If 0, no PRs should be queued/processed on a paused base branch
        Otherwise, this value should represent the max queue depth for all paused base branches combined.

    :return (List of PRs): The PRs that we should proceed with tagging.
    """
    merge_mode = repo.current_config.merge_rules.merge_mode
    if not merge_mode or not merge_mode.parallel_mode:
        return prs
    max_parallel_paused_builds = merge_mode.parallel_mode.max_parallel_paused_builds

    # Note that None and 0 are different configurations.
    if max_parallel_paused_builds is None:
        # no change in behavior - we count all PRs in calculating queue depth
        return prs

    if (
        repo.parallel_mode_config.max_parallel_builds
        and max_parallel_paused_builds >= repo.parallel_mode_config.max_parallel_builds
    ):
        logger.error(
            "max_parallel_paused_builds cannot be greater than max_parallel_builds",
            repo_id=repo.id,
        )
        return prs

    prs_to_process = []
    paused_prs = []

    # Regardless of the value of max_parallel_paused_builds,
    # we need to process all PRs on enabled base branches.
    for pr in prs:
        if _is_queue_paused(repo, pr.target_branch_name):
            paused_prs.append(pr)
        else:
            prs_to_process.append(pr)

    # max_parallel_paused_builds should be the max queue depth for all paused base branches combined.
    if max_parallel_paused_builds > 0:
        paused_base_branches = BaseBranch.query.filter_by(
            repo_id=repo.id, paused=True
        ).all()

        if paused_base_branches:
            paused_queue_depth = common.get_paused_queue_depth(
                repo, paused_base_branches
            )
            if paused_queue_depth < max_parallel_paused_builds:
                prs_to_process.extend(
                    paused_prs[: max_parallel_paused_builds - paused_queue_depth]
                )
    return prs_to_process


def has_exceeded_wait_time(
    repo: GithubRepo,
    prs: list[PullRequest],
) -> tuple[bool, datetime.timedelta | None]:
    """Returns True if the repo has exceeded the wait time for a batch.

    If not exceeded the wait time, it returns the minimum time remaining for the batch
    to be created. This is minimum time because there are multiple base branches, and
    the time is managed per base branch.
    """
    wait_time: int | None = repo.parallel_mode_config.batch_max_wait_minutes
    if not wait_time:
        return True, None

    for pr in prs:
        if pr.merge_operations and pr.merge_operations.is_bisected:
            logger.info(
                "Found a bisected PR, skipping wait time check",
                all_prs=prs,
                bisected_pr=pr,
            )
            return True, None

    base_branches = {pr.target_branch_name for pr in prs}

    # BotPR status can be "queued" or "closed". For batching, we are only
    # looking at queued PRs, so if there's no queued botPR, we should just
    # create the batch immediately.
    current_bot_prs: list[BotPr] = db.session.scalars(
        sa.select(BotPr)
        .where(
            BotPr.repo_id == repo.id,
            BotPr.deleted.is_(False),
            BotPr.target_branch_name.in_(base_branches),
        )
        .where(BotPr.status == "queued", BotPr.is_bisected_batch.is_(False))
        .order_by(BotPr.created.desc()),
    ).all()

    min_remaining: datetime.timedelta | None = None
    for base_branch in base_branches:
        last_bot_pr = next(
            (bp for bp in current_bot_prs if bp.target_branch_name == base_branch),
            None,
        )
        if not last_bot_pr:
            return True, None
        elapsed = time_util.now() - last_bot_pr.created
        if elapsed > datetime.timedelta(minutes=wait_time):
            return True, None
        remaining = datetime.timedelta(minutes=wait_time) - elapsed
        logger.info(
            "Repo wait time has not exceeded for base branch",
            repo_id=repo.id,
            base_branch=base_branch,
            elapsed=elapsed,
            remaining=remaining,
            wait_time=wait_time,
        )
        if min_remaining is None or min_remaining > remaining:
            min_remaining = remaining

    return False, min_remaining


def validate_parallel_mode_pr(
    client: GithubClient,
    repo: GithubRepo,
    pr: PullRequest,
    required_approvers: list[str],
) -> bool:
    # refresh the PR as it might be stale
    # https://docs.sqlalchemy.org/en/13/orm/session_state_management.html#when-to-expire-or-refresh
    db.session.refresh(pr)
    if pr.status != "queued":
        logger.info(
            "Repo %d PR %d Skipping processing as not queued", repo.id, pr.number
        )
        return False

    pull = client.get_pull(pr.number)
    if common.is_av_bot_pr(
        account_id=repo.account_id,
        author_login=pull.user.login,
        title=pull.title,
    ):
        logger.warning("Found bot owner PR to process %d %d", repo.id, pr.number)
        pr.deleted = True
        db.session.commit()
        return False

    # We will only queue it after the rest of the steps are completed
    # ensure pull ready
    merge_operation = _get_ready_merge_operation(pr)
    if not merge_operation:
        common.set_pr_blocked(pr, StatusCode.QUEUE_LABEL_REMOVED)
        db.session.commit()
        hooks.send_blocked_webhook(pr)
        send_pr_update_to_channel.delay(pr.id)
        return False

    if common.has_label(pull, repo.blocked_label):
        common.set_pr_blocked(pr, StatusCode.BLOCKED_BY_LABEL)
        db.session.commit()
        hooks.send_blocked_webhook(pr)
        send_pr_update_to_channel.delay(pr.id)
        return False

    if not merge_operation.skip_validation and not client.is_approved(
        pull, repo.preconditions.number_of_approvals, required_approvers
    ):
        common.set_pr_blocked(pr, StatusCode.NOT_APPROVED)
        db.session.commit()
        hooks.send_blocked_webhook(pr)
        client.add_label(pull, repo.blocked_label)
        send_pr_update_to_channel.delay(pr.id)
        return False

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
            pr.set_status("closed", StatusCode.PR_CLOSED_MANUALLY)
        db.session.commit()
        flexreview.celery_tasks.process_merged_pull.delay(pr.id)
        atc.non_released_prs.update_non_released_prs.delay(pr.repo_id)
        send_pr_update_to_channel.delay(pr.id)
        return False

    return True


def tag_new_batch(client: GithubClient, repo: GithubRepo, batch: Batch) -> None:
    """
    Mark the batch of PRs as "tagged" and create a BotPR that batches all previous changes.

    A PR has status "tagged" when we've created a BotPR (i.e., a GitHub draft PR
    that contains the changes from that PR and all previous PRs in the queue).
    """
    all_bot_prs: list[BotPr] = (
        BotPr.query.filter_by(status="queued", repo_id=repo.id)
        .filter(BotPr.target_branch_name == batch.target_branch_name)
        .order_by(BotPr.id.desc())
        .all()
    )

    # how many tagged PRs are there
    if repo.parallel_mode_config.use_affected_targets:
        tagged_prs: list[PullRequest] = []
        # TODO: optimize batching for target mode
        for pr in batch.prs:
            for stacked_pr in stack_manager.get_current_stack_ancestors(
                pr, skip_merged=True
            ):
                target_manager.update_dependency_graph(repo, stacked_pr)
                tagged_prs.extend(
                    target_manager.get_dependent_tagged_prs(repo, stacked_pr)
                )
        tagged_prs = list(set(tagged_prs))
    else:
        tagged_prs = common.get_all_tagged(repo.id, batch.target_branch_name)

    if not bisection.can_tag_batch(repo, batch, all_bot_prs):
        _handle_queue_capacity_reached(repo, batch.prs, len(all_bot_prs))
        return

    if repo.parallel_mode_config.use_parallel_bisection:
        if batch.is_bisected_batch():
            tagged_prs = []
        else:
            tagged_prs = [p for p in tagged_prs if not p.is_bisected()]

    parallel_mode_barrier_label = repo.parallel_mode_barrier_label
    target_branch = batch.target_branch_name
    # Get all the previous changes that we're also including in this PR
    previous_prs = [ep for ep in tagged_prs if ep.target_branch_name == target_branch]
    logger.info(
        "Repo found tagged PR,relevant PRs while tagging",
        repo_id=repo.id,
        tagged_prs_no=len(tagged_prs),
        tagged_prs=tagged_prs,
        previous_prs=len(previous_prs),
        bot_prs_no=len(all_bot_prs),
        bot_prs=all_bot_prs,
        pr_numbers=batch.pr_numbers,
        target_branch=batch.target_branch_name,
    )

    # For some PRs, we don't actually need to create a BotPR.
    # The most common case of this is when there are no previous PRs in the
    # queue and the batch_size = 1 (so if we hypothetically were to create a BotPR, it would only
    # contain the changes from the user PR, and we'd run the exact same CI
    # checks as the user PR, which is useless).
    use_same_pull = False
    new_branch = ""

    if len(batch.prs) == 1 and common.has_label(
        batch.get_first_pull(), SKIP_UPDATE_LABEL
    ):
        use_same_pull = True
    elif not previous_prs and len(batch.prs) == 1:
        # special case where there are no previous PRs and batch size = 1
        pr = batch.get_first_pr()
        pull = batch.get_first_pull()
        try:
            # There are no previous PRs in the queue, so we can take some shortcuts.
            # first check if the PR is up to date.
            if (
                repo.parallel_mode_config.skip_draft_when_up_to_date
                and client.is_up_to_date(pull, pr.target_branch_name)
                and pull.mergeable_state != "behind"
                # Don't do the optimization for PRs with stack parents. We want
                # the history of master to contain a commit-per-PR so we still
                # need to always create a draft PR.
                and not pr.stack_parent_id
            ):
                # For first PR, use the same PR as BotPR if it's latest.
                use_same_pull = True
            elif repo.parallel_mode_config.use_fast_forwarding:
                new_branch = create_bot_branch_for_fast_forward(
                    client,
                    repo,
                    batch,
                )
            else:
                tmp_branch, new_sha = client.create_latest_branch_with_squash_using_tmp(
                    pull,
                    batch.target_branch_name,
                    use_branch_in_title=True,
                )
                new_branch = client.copy_to_new_branch(
                    new_sha,
                    str(batch.get_first_pr().number),
                    prefix=_get_bot_branch_prefix(batch),
                )
                client.delete_ref(tmp_branch)

        except Exception as e:
            logger.info(
                "Failed to create latest bot branch",
                repo_id=repo.id,
                pr_number=batch.get_first_pr().number,
                exc_info=e,
            )
            if common.is_github_gone_crazy(e):
                _update_and_requeue(
                    client, batch.get_first_pr(), batch.get_first_pull()
                )
                raise

            if common.is_network_issue(e):
                # Raise error here so that we don't report this incorrectly as
                # a merge conflict.
                raise e
            if common.is_merge_conflict(e):
                bot_pr_merge_conflict(client, repo, batch, previous_prs=[])
                return

            if isinstance(e, pygithub.GithubException):
                # We still have a GH exception, let's handle this as an internal error
                # with GH and provide a debug message to the user.
                logger.error("Repo %d PR %d Internal error", repo.id, pr.number)

                mark_as_blocked(
                    client,
                    repo,
                    pr,
                    pull,
                    StatusCode.GITHUB_API_ERROR,
                    note=format_github_exception(e),
                )

            raise
    else:
        # We will try to create a new BotPR that contains the changes from the
        # batched PRs as well as all the previous PRs (if previous PRs exist).

        # If there are previous PRs in the queue, make sure we can still proceed tagging new PRs.
        if previous_prs:
            if any([p.skip_line for p in batch.prs]) and any(
                [not tp.skip_line for tp in previous_prs]
            ):
                # Found a new skip line with existing queued PRs.
                partial_queue_reset_by_skip_line_pr(repo, batch.get_first_pr())
                raise errors.ResetException("")

            if not all_bot_prs:
                logger.error(
                    "Existing tagged PRs but no queued BotPRs found %s %s",
                    repo.name,
                    batch.prs,
                )
                return

        # This is a long process, let's make sure access_token will be valid.
        access_token, client = common.get_client(repo)

        logger.info(
            "Starting creating branch for repo %d batched PR %s",
            repo.id,
            batch.pr_numbers,
        )
        try:
            new_branch = create_bot_branch(client, repo, batch, previous_prs)
            if _has_equal_commit_hash(
                client, repo, batch.target_branch_name, new_branch
            ):
                client.delete_ref(new_branch)
                _handle_empty_batch(client, repo, batch, previous_prs)
                raise errors.InvalidGithubStateException(
                    "Empty bot PR branch created. Retrying."
                )
            logger.info(
                "Finished creating branch for repo %d PR %s %s",
                repo.id,
                batch.pr_numbers,
                new_branch,
            )
        except errors.PRStatusException as e:
            logger.info(
                "Repo %d Found a merge conflict of PR %s: %s",
                repo.id,
                batch.pr_numbers,
                e.message,
            )
            # TODO[doratzeng]: Make sure this error message is right when using
            #   batches (e.g., if the batch has a merge conflict versus a
            #   conflict with a previous PR? not sure if we can figure out
            #   exactly which PR is conflicting with which but :shrug:)
            bot_pr_merge_conflict(
                client,
                repo,
                batch,
                conflicting_pr=e.conflicting_pr,
                previous_prs=previous_prs,
            )
            # Raise the error here for merge conflict in large batches
            # to make sure that the subsequent batch construction also
            # accounts for non-conflicting PRs that were in this batch.
            if e.conflicting_pr and e.conflicting_pr in batch.pr_numbers:
                raise
            return
        except Exception as e:
            if common.is_github_gone_crazy(e):
                _update_and_requeue(
                    client, batch.get_first_pr(), batch.get_first_pull()
                )
                raise e
            if common.is_network_issue(e):
                logger.info(
                    "Failed to connect with GH while creating bot PR",
                    repo_id=repo.id,
                    pr_numbers=batch.pr_numbers,
                    exc_info=e,
                )
                tag_queued_prs_async.with_countdown(10).delay(
                    repo.id,
                )
                raise e

            logger.info(
                "Found an issue while creating bot PR",
                repo_id=repo.id,
                pr_numbers=batch.pr_numbers,
                exc_info=e,
            )
            if isinstance(e, errors.InvalidGithubStateException):
                # In this case, we will just retry to construct PRs.
                raise e
            if common.is_merge_conflict(e):
                bot_pr_merge_conflict(client, repo, batch, previous_prs=previous_prs)
                return
            if isinstance(e, pygithub.GithubException):
                # We still have a GH exception, let's handle this as an internal error
                # with GH and provide a debug message to the user.
                logger.error(
                    "GitHub Internal Error",
                    repo_id=repo.id,
                    pr_numbers=batch.pr_numbers,
                    exc_info=e,
                )

                note = format_github_exception(e)
                for pr in batch.prs:
                    mark_as_blocked(
                        client,
                        repo,
                        pr,
                        batch.get_pull(pr.number),
                        StatusCode.GITHUB_API_ERROR,
                        note=note,
                    )
            raise

    if use_same_pull:
        logger.info(
            "Repo %d PR %d using the same PR in parallel mode",
            repo.id,
            batch.get_first_pr().number,
        )
        bot_pull = batch.get_first_pull()
    else:
        bot_pr_description = _bot_pr_body_text(repo, batch, previous_prs)
        try:
            title = _get_batch_pr_title(batch.prs)
            regex_configs = RegexConfig.query.filter_by(repo_id=repo.id).all()
            for config in regex_configs:
                title = title.replace(config.pattern, config.replace)
            bot_pull = client.create_pull(
                new_branch,
                batch.target_branch_name,
                title,
                bot_pr_description,
                draft=not repo.parallel_mode_config.use_active_reviewable_pr,
            )
            logger.info(
                "Repo %d PR %s created bot PR %d",
                repo.id,
                batch.pr_numbers,
                bot_pull.number,
            )
        except pygithub.GithubException as e:
            # clean up branch if we failed to create a draft PR
            client.delete_ref(new_branch)
            logger.info(
                "Repo %d deleted stale branch due to failed bot pr creation for PR %s",
                repo.id,
                batch.pr_numbers,
            )
            if (
                e.status == 422
                and isinstance(e.data, dict)
                and e.data["message"]
                == "Draft pull requests are not supported in this repository."
            ):
                for batch_pr in batch.prs:
                    mark_as_blocked(
                        client,
                        repo,
                        batch_pr,
                        batch.get_pull(batch_pr.number),
                        StatusCode.DRAFT_PRS_UNSUPPORTED,
                    )
                return
            raise

    bot_pr: BotPr | None = BotPr.query.filter_by(
        repo_id=repo.id, number=int(bot_pull.number)
    ).first()
    if not bot_pr:
        bot_pr = BotPr(
            number=int(bot_pull.number),
            repo_id=repo.id,
            account_id=repo.account_id,
            title=bot_pull.title,
            pull_request_id=batch.get_first_pr().id,
            head_commit_sha=bot_pull.head.sha,
            branch_name=bot_pull.head.ref,
            target_branch_name=bot_pull.base.ref,
            is_bisected_batch=batch.is_bisected_batch(),
        )
        db.session.add(bot_pr)
    else:
        bot_pr.title = bot_pull.title
        bot_pr.pull_request_id = batch.get_first_pr().id
        bot_pr.set_status("queued", StatusCode.QUEUED)
        bot_pr.head_commit_sha = bot_pull.head.sha
        bot_pr.branch_name = bot_pull.head.ref
        bot_pr.target_branch_name = bot_pull.base.ref
        bot_pr.is_bisected_batch = batch.is_bisected_batch()
        # reset the created time because we use this time to
        # identify if the bot PR is active or stale.
        # bot_pr.created > pr.queued_at
        bot_pr.created = time_util.now()
    if parallel_mode_barrier_label and any(
        common.has_label(pull, parallel_mode_barrier_label.name)
        for pull in batch.get_all_pulls()
    ):
        bot_pr.is_blocking = True
    # Update pr_list (these are previous PR commits that are included)
    bot_pr.pr_list = previous_prs.copy()
    bot_pr.pr_list.extend(batch.prs)

    # update batch_pr_list (these are PRs included in the current batch)
    bot_pr.batch_pr_list = batch.prs.copy()
    db.session.commit()

    for batch_pr in batch.prs:
        batch_pr.set_status("tagged", StatusCode.QUEUED)
        activity.create_activity(
            repo_id=repo.id,
            pr_id=batch_pr.id,
            activity_type=ActivityType.BOT_PR_CREATED,
            status_code=batch_pr.status_code,
            payload=ActivityBotPullRequestCreatedPayload(
                bot_pr_id=bot_pr.id,
                # We capture the HEAD OID now in case it changes for whatever reason
                bot_pr_head_oid=bot_pr.head_commit_sha,
            ),
            commit=False,
        )
        pilot_data = common.get_pilot_data(batch_pr, "added_to_batch")
        hooks.call_master_webhook.delay(
            batch_pr.id,
            "added_to_batch",
            pilot_data=pilot_data,
            ci_map={},
            note="",
            bot_pr_id=bot_pr.id,
            blocking_pr_numbers=[p.number for p in previous_prs],
        )
    db.session.commit()
    if use_same_pull:
        comments.post_parallel_mode_same_pr_comment.delay(batch.get_first_pr().id)


def _get_batch_pr_title(pr_list: list[PullRequest]) -> str:
    assert len(pr_list) > 0, "Empty PR list detected"
    if len(pr_list) == 1:
        return str(app.config["PR_TITLE_PREFIX"] + pr_list[0].title)

    return str(
        app.config["PR_TITLE_PREFIX"]
        + f"Codemix of {len(pr_list)} pull requests used for validation"
    )


def _handle_empty_batch(
    client: GithubClient,
    repo: GithubRepo,
    batch: Batch,
    previous_prs: list[PullRequest],
) -> None:
    """
    Handles the situation where even though a batch has some PRs, making a new
    BotPR branch doesn't make a new merge commit.

    This can happen if a PR in the batch is completely included in
    another PR already queued, but it seems this happens when
    GitHub API doesn't do the job according to the commit history.

    We retry this operation to some extent, and then mark PRs
    blocked if we retried enough.
    """
    logger.info(
        "A new BotPR branch doesn't add a new commit on top of the previous PRs",
        pr_numbers=batch.pr_numbers,
        previous_pr_numbers=[pr.number for pr in previous_prs],
    )
    for pr in batch.prs:
        mo = pr.merge_operations
        assert mo
        with pr.bound_contextvars(), mo.bound_contextvars():
            if (
                mo.transient_error_retry_count
                < MergeOperation.MAX_TRANSIENT_ERROR_RETRY_COUNT
            ):
                mo.transient_error_retry_count += 1
                logger.info(
                    "Retrying MergeOperation",
                    transient_error_retry_count=mo.transient_error_retry_count,
                )
            else:
                logger.info(
                    "MergeOperation exceeded the retry cap, marking as blocked",
                )
                mark_as_blocked(
                    client,
                    repo,
                    pr,
                    batch.get_pull(pr.number),
                    StatusCode.BLOCKED_BY_EMPTY_DRAFT_PR,
                )
    db.session.commit()


def _update_and_requeue(
    client: GithubClient, pr: PullRequest, pull: pygithub.PullRequest
) -> None:
    redis_key = f"pr:{pr.id}:requeue"
    # Avoid our requeueing logic in a loop.
    if redis_client.get(redis_key):
        logger.error(
            "Repo %d PR %d already updated/requeued, transitioning to blocked",
            pr.repo_id,
            pr.number,
        )
        _transition_pr_not_ready(
            client,
            pr.repo,
            pr,
            pull,
            StatusCode.GITHUB_API_ERROR,
            new_status="blocked",
            status_reason=(
                "Aviator is unable to merge this pull request due to internal "
                "errors (HTTP 500) from GitHub. This pull request may need to "
                "be merged manually using the GitHub UI (depending on your "
                "repository setup you may need to ask a user with the "
                "maintainer role to merge this pull request)."
            ),
        )
        return
    redis_client.set(redis_key, 1, ex=datetime.timedelta(hours=6))
    # This is bananas. GH returns 500 randomly for PRs. Forcing a branch
    # update if this happens
    repo = pr.repo
    logger.info(
        "Repo %d PR %d got 500, forcing branch update",
        repo.id,
        pull.number,
    )
    # Add a comment explaining that we are going to do this, although this might surface this issue
    # more to the user, but it at least explains that we are doing this as a workaround.
    comment = (
        "Aviator was unable to process this pull request due to errors with GitHub. "
        "This has nothing to do with your specific code changes. "
        "Aviator will try to update the pull request and try again."
    )
    pull.create_issue_comment(comment)
    # This method exists but seems to be missing from the type definitions.
    pull.update_branch(pull.head.sha)  # type: ignore[attr-defined]
    # if we are updating the branch, we should also move the PR back to pending state.
    _transition_pr_not_ready(
        client,
        repo,
        pr,
        pull,
        StatusCode.GITHUB_API_ERROR,
        new_status="pending",
        status_reason="Failed create draft branch due to Github error, updated and requeued the PR.",
    )


def _handle_queue_capacity_reached(
    repo: GithubRepo,
    prs: list[PullRequest],
    queue_size: int,
) -> None:
    logger.info(
        "MergeQueue is at capacity",
        repo_id=repo.id,
        queue_size=queue_size,
        capacity=repo.parallel_mode_config.max_parallel_builds,
        bisection_capacity=repo.parallel_mode_config.max_parallel_bisected_builds,
    )
    for pr in prs:
        pr.status_code = StatusCode.THROTTLED_BY_MAX_CAP
        activity.create_activity(
            repo.id,
            pr.id,
            ActivityType.DELAYED,
            pr.status_code,
            commit=False,
        )
    db.session.commit()
    for pr in prs:
        update_gh_pr_status.delay(pr.id)


def create_bot_branch(
    client: GithubClient,
    repo: GithubRepo,
    batch: Batch,
    previous_prs: list[PullRequest],
) -> str:
    """
    Creates a branch to be used as draft PR in parallel mode. There are three modes to create branches,
    in fast forwarding mode, we start from the last bot PR as the baseline and then "squash merge"
    the current PR on top. This way the history would remain linear and this commit, when passed, can
    be fast forwarded into the main / master branch.

    For target mode, we start with the current PR, add the latest master branch and then one at a time
    add the other PRs. This strategy helps us identify if there's a merge conflict.

    The default mode is very similar to the fast forwarding mode, except we create the draft PR using
    "merge commits" instead of squash merge. This helps reduce the number of API calls needed compared
    to fast forwarding mode or target mode.

    :return: the newly created branch name
    """
    if repo.parallel_mode_config.use_fast_forwarding:
        return create_bot_branch_for_fast_forward(client, repo, batch)

    # Fetch the oldest bot PR that satisfies all previous PRs
    oldest_bot_pr = get_oldest_bot_pr(repo, batch.target_branch_name, previous_prs)
    if _should_recreate_from_target_branch(repo, batch, oldest_bot_pr):
        return create_bot_branch_from_target_branch(client, repo, batch, previous_prs)

    return create_bot_branch_default(client, repo, batch, oldest_bot_pr)


def _should_recreate_from_target_branch(
    repo: GithubRepo, batch: Batch, bot_pr: BotPr | None
) -> bool:
    """
    Returns true if we should recreate the bot branch from the target branch instead
    of using the previous bot PR as the base.
    """
    if repo.parallel_mode_config.use_affected_targets:
        if not bot_pr:
            # If we found all PRs in a previous bot PR, we can use the default mode (faster).
            logger.info(
                "No valid previous BotPR found to build on top of",
                repo_id=repo.id,
                pr_numbers=batch.pr_numbers,
            )
            return True
        logger.info(
            "Affected target mode can be recreated using bot PR",
            repo_id=repo.id,
            bot_pr=bot_pr.number,
            batch_prs=batch.pr_numbers,
        )
        # Now we will check for merge conflict.

    for pr in batch.prs:
        if _has_merge_conflict_with_no_conflicting_pr(pr.id):
            logger.info(
                "Repo %d PR %d will recreate from target branch", pr.repo_id, pr.number
            )
            return True
    return False


def _are_previous_prs_included(
    previous_prs: list[PullRequest],
    *,
    latest_bot_pr: BotPr,
) -> bool:
    """
    Check if the latest bot PR includes all the previous PRs in the batch.
    """
    prev_bot_pr_prs = {p.number for p in latest_bot_pr.pr_list}
    return all(pr.number in prev_bot_pr_prs for pr in previous_prs)


def create_bot_branch_for_fast_forward(
    client: GithubClient,
    repo: GithubRepo,
    batch: Batch,
) -> str:
    # always refetch the latest bot PR because FF cannot work with a stale bot PR.
    latest_bot_pr = _get_latest_bot_pr(repo, batch.target_branch_name)
    tmp_branch = _get_tmp_branch_for_bot_pr(client, repo, batch, latest_bot_pr)
    try:
        head_sha = _squash_batch_prs_to_branch(client, batch, tmp_branch)
        new_branch = client.copy_to_new_branch(
            head_sha, str(batch.get_first_pr().number), prefix="mq-bot-"
        )
        return new_branch
    except Exception as e:
        logger.info(
            "Deleted stale branch due to failed bot pr creation for PR",
            repo_id=repo.id,
            pr_numbers=batch.pr_numbers,
            exc_info=e,
        )
        raise
    finally:
        client.delete_ref(tmp_branch)


def _squash_batch_prs_to_branch(
    client: GithubClient, batch: Batch, branch_name: str
) -> str:
    """
    Add each PR in the batch to the branch using a squash merge.

    This results for one commit per PR in the batch.
    :param batch:
    :param branch_name:
    :return: the latest SHA at the top of the branch
    """
    assert batch.prs, "Expected batch.prs to be non-empty"
    latest_sha = None
    for pr in batch.prs:
        pull = batch.get_pull(pr.number)
        try:
            pr_to_merge_sha = _squash_pull_to_branch(client, pr, pull, branch_name)
            _, latest_sha = pr_to_merge_sha[-1]
        except Exception as exc:
            logger.info(
                "Failed to squash onto batch",
                repo_id=pr.repo.id,
                pr_number=pr.number,
                branch_name=branch_name,
                pr_numbers=batch.pr_numbers,
                exc_info=exc,
            )
            raise
    assert latest_sha, "Expected latest_sha to be non-null"
    return latest_sha


def _get_tmp_branch_for_bot_pr(
    client: GithubClient,
    repo: GithubRepo,
    batch: Batch,
    bot_pr: BotPr | None,
) -> str:
    base_branch = bot_pr.branch_name if bot_pr else batch.target_branch_name
    base_sha = client.get_branch_head_sha(base_branch)
    logger.info(
        "PR created temp branch",
        repo_id=repo.id,
        batch_prs=str(batch.pr_numbers),
        base_branch=base_branch,
        base_sha=base_sha,
    )

    return client.copy_to_new_branch(
        base_sha, str(batch.get_first_pr().number), prefix="mq-tmp-"
    )


def _handle_invalid_bot_pr(bot_pr: BotPr) -> None:
    """
    Verify that the PR associated with this bot_pr is still valid.
    If not, we should  close this bot_pr.

    ankit: Leaving it out for now, but we may want to call
    removed_from_batch here as well.
    """
    for pr in bot_pr.batch_pr_list:
        if pr.status == "tagged":
            logger.info(
                "Repo %d bot PR %d Found a valid PR in this batch: %d",
                bot_pr.repo_id,
                bot_pr.number,
                pr.number,
            )
            return
    logger.info(
        "Repo %d bot PR %d No valid PRs found in this batch. Closing.",
        bot_pr.repo_id,
        bot_pr.number,
    )
    bot_pr.set_status("closed", StatusCode.UNKNOWN)
    db.session.commit()
    close_pulls.delay(
        bot_pr.repo_id, [bot_pr.number], close_reason="Invalid codemix detected"
    )


def create_bot_branch_default(
    client: GithubClient, repo: GithubRepo, batch: Batch, oldest_bot_pr: BotPr | None
) -> str:
    """
    Constructs the bot branch by applying the batch PRs on top of the oldest valid bot PR.
    This is useful to keep the number of API calls low.
    """
    tmp_branch = _get_tmp_branch_for_bot_pr(client, repo, batch, oldest_bot_pr)
    try:
        apply_prs_to_branch(client, repo, batch.get_first_pr(), batch.prs, tmp_branch)
        new_branch = client.copy_branch_to_new_branch(
            tmp_branch,
            str(batch.get_first_pr().number),
            prefix=_get_bot_branch_prefix(batch),
        )
        return new_branch
    except Exception as e:
        logger.info(
            "Deleted stale branch due to failed bot pr creation",
            repo_id=repo.id,
            pr_numbers=batch.pr_numbers,
            exc_info=e,
        )
        raise
    finally:
        client.delete_ref(tmp_branch)


def create_bot_branch_from_target_branch(
    client: GithubClient,
    repo: GithubRepo,
    batch: Batch,
    previous_prs: list[PullRequest],
) -> str:
    """
    Constructs the bot branch by applying all the previous PRs and current
    batch PRs on top of target branch. This is useful to create the bot PR from scratch
    instead of using the latest bot PR as the base.
    """
    try:
        tmp_branch, _ = client.create_latest_branch_with_squash_using_tmp(
            batch.get_first_pull(),
            batch.target_branch_name,
            use_branch_in_title=True,
        )
    except pygithub.GithubException as e:
        if e.status == 409:
            conflicting_pr = (
                str(batch.get_first_pr().number) if batch.get_first_pr() else "Unknown"
            )
            debug_message = "Conflicting PR #" + conflicting_pr
            if conflicting_pr != "Unknown":
                debug_message += "\n Attempting: " + _get_compare_url(
                    repo, batch.target_branch_name, batch.get_first_pull().head.ref
                )

            raise errors.PRStatusException(
                StatusCode.MERGE_CONFLICT,
                debug_message,
                conflicting_pr=(
                    batch.get_first_pr().number if batch.get_first_pr() else 0
                ),
            )
        raise

    try:
        if previous_prs:
            apply_prs_to_branch(
                client, repo, batch.get_first_pr(), previous_prs, tmp_branch
            )

        apply_prs_to_branch(
            client, repo, batch.get_first_pr(), batch.prs[1:], tmp_branch
        )

        latest_sha = client.get_branch_head_sha(tmp_branch)
        new_branch = client.copy_to_new_branch(
            latest_sha,
            str(batch.get_first_pr().number),
            prefix=_get_bot_branch_prefix(batch),
        )
        client.delete_ref(tmp_branch)
    except Exception as e:
        # clean up branch if we failed to create draft PR
        if not isinstance(e, errors.PRStatusException) or not e.conflicting_pr:
            client.delete_ref(tmp_branch)
            logger.info(
                "Deleted stale branch due to failed bot pr creation for PR",
                repo_id=repo.id,
                pr_numbers=batch.pr_numbers,
                exc_info=e,
            )
        else:
            logger.info(
                "Failed to create bot pr. Branch will be deleted later.",
                repo_id=repo.id,
                pr_numbers=batch.pr_numbers,
                branch=tmp_branch,
            )
            # Delete this branch after 2 days.
            delete_stale_branch.with_countdown(2 * 24 * 60 * 60).delay(
                repo.id, tmp_branch
            )
        raise e
    return new_branch


def _get_bot_branch_prefix(batch: Batch) -> str:
    pr = batch.get_first_pr()
    if pr.merge_operations and pr.merge_operations.skip_validation:
        return "mq-bot-skip-ci-"
    return "mq-bot-"


def _is_skip_validation(pr: PullRequest) -> bool:
    return bool(pr.merge_operations and pr.merge_operations.skip_validation)


def apply_prs_to_branch(
    client: GithubClient,
    repo: GithubRepo,
    original_pr: PullRequest,
    dependent_prs: list[PullRequest],
    target_branch: str,
) -> str:
    """
    Takes a list of PRs (dependent_prs) and applies those commits on top of the
    target_branch. Note that the target_branch should not be confused with
    pr.target_branch_name. The passed target_branch typically is a temp branch
    used for staging the commits.

    :param client: GitHub client
    :param repo: GithubRepo object
    :param original_pr: The PR for which this construction is happening. This is
      only used for logging and validation, and not used for the PR construction
    :param dependent_prs: List of PRs that are actually applied to the target_branch
    :param target_branch: The branch where the commits are to be applied
    """
    ep = None
    pr_list = []
    merge_commit: pygithub.Commit | None = None
    try:
        for ep in dependent_prs:
            if ep.target_branch_name != original_pr.target_branch_name:
                logger.info(
                    "Repo %d Skipping PR %d branch %s for batch of PR %d branch %s",
                    repo.id,
                    ep.number,
                    ep.target_branch_name,
                    original_pr.number,
                    original_pr.target_branch_name,
                )
                continue
            if ep.status in ("merged", "closed"):
                logger.info(
                    "Skipping to apply this PR as it is already merged / closed",
                    repo_id=repo.id,
                    pr_number=ep.number,
                    pr_status=ep.status,
                    original_pr=original_pr.number,
                )
                continue
            merge_commit = client.merge_branch_to_branch(
                str(ep.branch_name), target_branch
            )
            logger.info(
                "Repo %d merged PR %d on branch %s for original PR %d successfully",
                repo.id,
                ep.number,
                target_branch,
                original_pr.number,
            )
            pr_list.append(ep)
        return merge_commit.sha if merge_commit else ""
    except pygithub.GithubException as e:
        if e.status == 409:
            debug_message = "Conflicting PR #" + str(ep.number if ep else "Unknown")
            if ep:
                debug_message += "\n Attempting: " + _get_compare_url(
                    repo, target_branch, str(ep.branch_name)
                )

            raise errors.PRStatusException(
                StatusCode.MERGE_CONFLICT,
                debug_message,
                conflicting_pr=(ep.number if ep else 0),
            )
        raise


def _find_potential_merge_conflicts(
    pr: PullRequest, other_prs: list[PullRequest]
) -> list[PullRequest]:
    """
    Determine which PRs could potentially conflict with the target PR.

    This works heuristically by checking the modified files of all the PRs
    involved. A PR is considered potentially conflicting if it modifies at least
    one of the same files.

    This is generally necessary because we construct test branches (BotPr's) by
    applying PRs in order. So if we apply #10 on top of main->#7->#8->#9, we
    can't tell if #10 conflicts with main, #7, #8, #9, or any combination of
    them.
    """
    files_map = _fetch_modified_files_map(other_prs + [pr])
    if not files_map:
        logger.error("Repo %d PR %d empty map", pr.repo.id, pr.number)
        return []
    current_pr_files = files_map.get(pr.number)
    if not current_pr_files:
        logger.error("Repo %d PR %d no modified files found", pr.repo.id, pr.number)
        return []

    potential_conflicts = []
    for other_pr in other_prs:
        dpr_files: set[str] | None = files_map.get(other_pr.number)
        if dpr_files and dpr_files.intersection(current_pr_files):
            potential_conflicts.append(other_pr)
    logger.info(
        "Repo %d PR %d heuristically identified potential conflicting PRs: %s",
        pr.repo.id,
        pr.number,
        [p.number for p in potential_conflicts],
    )
    return potential_conflicts


def _fetch_modified_files_map(
    pr_list: list[PullRequest],
) -> dict[int, set[str]]:
    """
    Given a list of PRs, return a map of PR number to a set of modified files.
    """
    query = (
        """
        query ($ids: [ID!]!) {
          %s
          nodes(ids: $ids){
            ... on PullRequest {
                number
                files(first: 100) {
                  nodes {
                    path
                  }
                }
            }
          }
        }
        """
        % graphql.RATE_LIMIT_SUB_QUERY
    )
    if not pr_list:
        return {}

    gql = _get_graphql(pr_list[0].repo)
    pr_ids = [pr.gh_node_id for pr in pr_list if pr.gh_node_id]
    result = gql.fetch(
        query,
        ids=pr_ids[:100],  # max 100 ids per query
    )

    if not result.data:
        logger.error(
            "Failed to fetch data for repo %s from github graphql %s",
            pr_list[0].repo,
            result,
        )
        return {}

    try:
        nodes = result.data.get("nodes", [])
    except AttributeError:
        logger.error(
            "Failed to fetch nodes for repo %s from github graphql %s",
            pr_list[0].repo,
            result,
        )
        return {}

    files_map = {}
    for node in nodes:
        files_map[node["number"]] = {node["path"] for node in node["files"]["nodes"]}
    return files_map


@celery.task
def config_consistency_check(repo_id: int) -> None:
    """
    The objective of this method is to make sure that if the config has
    been updated, that all the PRs are in correct state. Currently it only
    validates that if a PR has moved from a parallel mode to FIFO mode, that
    we close / invalidate corresponding botPRs.
    """
    repo = GithubRepo.get_by_id_x(repo_id)
    if repo.parallel_mode:
        # If Repo is still in parallel mode, we don't need to do anything.
        return

    # If Repo is in FIFO mode, we need to make sure that all the bot PRs are closed.
    bot_prs: list[BotPr] = BotPr.query.filter_by(status="queued", repo_id=repo.id).all()
    logger.info("Repo %d found %d bot PRs with no parallel mode", repo.id, len(bot_prs))
    if not bot_prs:
        return

    with locks.for_repo(repo):
        full_queue_reset(repo, StatusCode.RESET_BY_CONFIG_CHANGE)


@celery.task
def delete_stale_branch(repo_id: int, branch_name: str) -> None:
    repo = GithubRepo.get_by_id_x(repo_id)
    _, client = common.get_client(repo)
    client.delete_ref(branch_name)


def _get_compare_url(repo: GithubRepo, target_branch: str, pr_branch: str) -> str:
    return str(
        app.config["GITHUB_BASE_URL"]
        + "/"
        + repo.name
        + "/compare/"
        + target_branch
        + "..."
        + pr_branch
    )


def _block_all_batch_prs(
    client: GithubClient,
    repo: GithubRepo,
    batch: Batch,
    note: str,
) -> None:
    new_line = "\n"

    for pr in batch.prs:
        code = (
            StatusCode.REBASE_FAILED if repo.use_rebase else StatusCode.MERGE_CONFLICT
        )
        final_note = (
            note
            + f"{new_line}Other PRs in the batch: {_formatted_batch_pr_list(pr, batch.prs)}."
        )
        mark_as_blocked(
            client,
            repo,
            pr,
            batch.get_pull(pr.number),
            code,
            ci_map={},
            note=final_note,
        )
        logger.info(
            "Repo %d PR %d status code %s. PRs in batch %s",
            repo.id,
            pr.number,
            pr.status_code,
            batch.pr_numbers,
        )


def _block_conflicting_pr(
    client: GithubClient,
    repo: GithubRepo,
    batch: Batch,
    note: str,
    conflicting_pr: int,
) -> None:
    new_line = "\n"

    for pr in batch.prs:
        if conflicting_pr == pr.number:
            final_note = (
                note
                + f"{new_line}Other PRs in the batch (these PRs will be automatically re-queued): {_formatted_batch_pr_list(pr, batch.prs)}. "
            )
            code = (
                StatusCode.REBASE_FAILED
                if repo.use_rebase
                else StatusCode.MERGE_CONFLICT
            )
            mark_as_blocked(
                client,
                repo,
                pr,
                batch.get_pull(pr.number),
                code,
                ci_map={},
                note=final_note,
            )

            logger.info(
                "Repo %d PR %d status code %s", repo.id, pr.number, pr.status_code
            )
        else:
            # The PRs in the batch should already be in `queued` state (since we were attempting to create the bot pr).
            # If not, we log the error and make sure it's queued.
            if pr.status != "queued":
                logger.error(
                    "Repo %d PR %d, PR should be in queued state but is %s",
                    repo.id,
                    pr.number,
                    pr.status,
                )
                pr.set_status("queued", StatusCode.QUEUED)
                pr.queued_at = time_util.now()
    db.session.commit()


def bot_pr_merge_conflict(
    client: GithubClient,
    repo: GithubRepo,
    batch: Batch,
    previous_prs: list[PullRequest],
    conflicting_pr: int = 0,
) -> None:
    logger.info(
        "Repo %d PR %s merge conflict while creating bot PR, conflicting PR: %d",
        repo.id,
        batch.pr_numbers,
        conflicting_pr,
    )

    if len(batch.prs) > 1:
        _handle_batch_merge_conflict(client, repo, batch, previous_prs, conflicting_pr)
    else:
        _handle_single_pr_merge_conflict(
            client, repo, batch, previous_prs, conflicting_pr
        )

    tag_queued_prs_async.delay(repo.id)


def _handle_batch_merge_conflict(
    client: GithubClient,
    repo: GithubRepo,
    batch: Batch,
    previous_prs: list[PullRequest],
    conflicting_pr: int = 0,
) -> None:
    """
    Handle merge conflicts when batch_size > 1.

    See `create_bot_branch_default`.

    We first call `apply_prs_to_branch` for the `previous_prs`.
    That means we add previous_prs to the tmp_branch (which contains the target branch and first PR in batch).
    Therefore, if conflicting_pr comes from previous_prs, (aka conflicting_pr is not in the batch),
    then conflicting_pr conflicts with the first PR in the batch.

    Then we call `apply_prs_to_branch` for `batch.prs[1:]`.
    That means we add batch.prs to the tmp_branch.
    Therefore, if conflicting_pr comes from batch.prs, (aka conflicting_pr is in the batch),
    conflicting_pr conflicts with any of: master/main, previous_prs, or another PR in the batch,
    since those have already been added to the tmp_branch.
    """
    if conflicting_pr == 0:
        # No conflicting_pr means the conflict is with the base branch. Block all PRs in the batch.
        note = (
            f"There is a conflict in this batch with `{repo.head_branch}`, please resolve manually and requeue. "
            f"If resolving manually does not work, you may need to wait for previous PRs in the queue to merge."
        )
        _block_all_batch_prs(client, repo, batch, note)
    elif conflicting_pr in batch.pr_numbers:
        # Conflicts with another PR in the batch, target branch, or previous PR,
        # block conflicting_pr and requeue other PRs in the batch.
        pr = batch.get_pr_by_number_x(conflicting_pr)
        other_batch_prs = [p for p in batch.prs if p.number != conflicting_pr]
        potential_conflicts = _find_potential_merge_conflicts(
            pr, previous_prs + other_batch_prs
        )
        if potential_conflicts:
            text = "\n ".join(
                f"* [{pr.number} {pr.title}]({pr.number})" for pr in potential_conflicts
            )
            note = (
                f"A merge conflict occurred while queueing this pull request. Potential conflicting pull requests:\n{text}. "
                f"\n\nPlease resolve manually and requeue."
                f"If resolving manually does not work, you may need to wait for previous PRs in the queue to merge. "
                f"If you determine that the issue is with a different PR in the batch, remove the `{repo.blocked_label}` label to re-queue this PR."
            )
        else:
            note = (
                f"This PR conflicts with one of the following: another PR in the batch, `{repo.head_branch}`, or a previous PR in the queue. "
                f"\n\nThe PR that caused the merge conflict should be reported in the sticky comment shortly."
            )
        _block_conflicting_pr(client, repo, batch, note, conflicting_pr)
    else:
        # One of the PRs in this batch conflicts with conflicting_pr. Block all PRs in the batch.
        # We have identified that some PR in this batch conflicts with <conflicting_pr>. Other PRs in batch: <list>
        note = (
            f"There is a conflict in this batch with [{conflicting_pr}]({conflicting_pr}). "
            f"\n\nPlease resolve manually and requeue. "
            f"If resolving manually does not work, you may need to wait for previous PRs in the queue to merge. "
            f"If you determine that the issue is with a different PR in the batch, remove the `{repo.blocked_label}` label to re-queue this PR."
        )
        _block_all_batch_prs(client, repo, batch, note)


def _handle_single_pr_merge_conflict(
    client: GithubClient,
    repo: GithubRepo,
    batch: Batch,
    previous_prs: list[PullRequest],
    conflicting_pr: int = 0,
) -> None:
    # Handle single PR in the batch
    batch_pr = batch.get_first_pr()
    if batch_pr.number != conflicting_pr and conflicting_pr != 0:
        # conflicts with another PR
        note = f"We have identified that this PR conflicts with [{conflicting_pr}]({conflicting_pr}). "
    else:
        potential_conflicts = _find_potential_merge_conflicts(batch_pr, previous_prs)
        if potential_conflicts:
            text = "\n".join(
                f"* [{pr.number} {pr.title}]({pr.number})" for pr in potential_conflicts
            )
            note = f"A merge conflict occurred while queueing this pull request. Potential conflicting pull requests:\n{text}. "
        elif not _should_retry_bot_pr_creation(batch):
            # if no conflicting_pr or conflicts with itself, the conflict is with base branch.
            note = f"There is a conflict with `{repo.head_branch}` or with another PR in the queue. "
        else:
            logger.info(
                "Repo %d PR %d found a batch with unknown merge conflict",
                repo.id,
                batch_pr.number,
            )
            # At this point, it's possible that we are failing to create a batch because the last bot PR
            # still has changes from a  conflicting PR but that conflicting PR has already been merged. In this case,
            # we should try to build the bot PR from scratch.
            _set_merge_conflict_with_no_conflicting_pr(batch_pr.id)
            # Return without blocking the PR.
            return
    note += (
        "\n\nPlease resolve manually and requeue. "
        "If resolving manually does not work, you may need to wait for previous PRs in the queue to merge."
    )

    code = StatusCode.REBASE_FAILED if repo.use_rebase else StatusCode.MERGE_CONFLICT
    mark_as_blocked(
        client,
        repo,
        batch_pr,
        batch.get_first_pull(),
        code,
        ci_map={},
        note=note,
    )
    logger.info(
        "Repo %d PR %d status code %s",
        repo.id,
        batch_pr.number,
        batch_pr.status_code,
    )


def _should_retry_bot_pr_creation(batch: Batch) -> bool:
    """
    Returns True if we can prove that the merge conflict was invalid. Return False
    if the merge conflict was genuine. We return False if this was previously reported
    as an invalid merge conflict, but we still could not create a draft PR for it.
    """
    for pull in batch.get_all_pulls():
        if pull.mergeable_state == "dirty":
            return False

    # To avoid cyclic loop, let's log if we hit this situation again and return True.
    for pr in batch.prs:
        if _has_merge_conflict_with_no_conflicting_pr(pr.id):
            logger.error(
                "Repo %d PR %d has an unverified merge conflict", pr.repo_id, pr.number
            )
            return False
    return True


def _formatted_batch_pr_list(pr: PullRequest, batch_prs: list[PullRequest]) -> str:
    result = []
    for batch_pr in batch_prs:
        if batch_pr.number != pr.number:
            result.append(f"[{batch_pr.number}]({batch_pr.number})")
    return ", ".join(result)


def bot_pr_set_failure_locking(
    client: GithubClient,
    repo: GithubRepo,
    pr: PullRequest,
    status_code: StatusCode,
) -> None:
    with locks.for_repo(repo):
        # refresh the PR as it might be stale
        # https://docs.sqlalchemy.org/en/13/orm/session_state_management.html#when-to-expire-or-refresh
        db.session.refresh(pr)
        bot_pr_set_failure(client, repo, pr, status_code)


def bot_pr_set_failure(
    client: GithubClient,
    repo: GithubRepo,
    pr: PullRequest,
    status_code: StatusCode,
) -> None:
    db.session.refresh(pr)
    bot_pr = pr.latest_bot_pr
    if bot_pr:
        bot_pr.bind_contextvars()
    if bot_pr and bot_pr.status == "closed" and pr.status != "tagged":
        # if the bot PR is already closed, we have already handled this failure.
        # There's also a possibility that the associated PR is about to be merged
        # in a batch, in which case the bot PR may already be closed. So if the PR is
        # tagged, we still want to dequeue it.
        logger.info(
            "Repo %d PR %d bot PR %d is already closed, skipping handling failure",
            repo.id,
            pr.number,
            bot_pr.number,
        )
        return

    if pr.status not in ("merged", "closed"):
        # Do not call `mark_as_blocked` here since we attempt requeuing
        # with bisection in `handle_failed_bot_pr`.
        common.set_pr_blocked(pr, status_code)
        db.session.commit()
    logger.info(
        "Repo %d PR %d marking as blocked, reason %s",
        repo.id,
        pr.number,
        pr.status_code,
    )
    if bot_pr:
        handle_failed_bot_pr(
            client,
            repo,
            bot_pr,
            False,
            ci_map={},
            failed_batch_pr=pr,
            skip_reset_queue=(
                pr.status_code == StatusCode.MERGED_MANUALLY
                and len(bot_pr.batch_pr_list) == 1
                and is_bot_pr_top(bot_pr, repo)
            ),
        )


def process_top_parallel_mode(repo: GithubRepo, branch_name: str) -> None:
    if repo.parallel_mode_config.use_affected_targets:
        top_prs = target_manager.get_all_top_prs(repo, branch_name)
        for pr in top_prs:
            parallel_mode_check_head_with_targets(repo, pr)
    else:
        parallel_mode_check_head_for_branch(repo, branch_name)


def parallel_mode_check_head_with_targets(repo: GithubRepo, pr: PullRequest) -> None:
    bot_pr = pr.latest_bot_pr
    if not bot_pr:
        logger.error("Repo %d Found tagged PR %d with no bot PR", repo.id, pr.number)
    else:
        parallel_mode_process_head(repo, [bot_pr])


def parallel_mode_check_head_for_branch(
    repo: GithubRepo, target_branch_name: str
) -> None:
    logger.info(
        "Repo %d checking parallel mode head for branch %s", repo.id, target_branch_name
    )
    process_emergency_merge_prs(repo, target_branch_name)
    bot_pr_list = common.get_top_bot_prs(repo, target_branch_name)
    if not bot_pr_list:
        logger.info("Repo %d nothing to process", repo.id)
        # If there's nothing to process, there may be PRs queued but not tagged.
        # This may happen if we have an incomplete batch that has reached its timeout,
        # but there are no other actions triggering tagging.
        # Also intentionally not using the async function since we are already using a lock in process_top_async.
        tag_queued_prs(repo)
        return
        # If we are doing parallel mode processing
    regular_bot_prs = bot_pr_list
    if repo.parallel_mode_config.use_parallel_bisection:
        # Exclude the bisected PRs.
        regular_bot_prs = [bp for bp in bot_pr_list if not bp.is_bisected_batch]
        bisected_bot_prs = [bp for bp in bot_pr_list if bp.is_bisected_batch]
        if bisected_bot_prs:
            parallel_mode_process_head(repo, bisected_bot_prs)
    parallel_mode_process_head(repo, regular_bot_prs)


def process_emergency_merge_prs(repo: GithubRepo, target_branch_name: str) -> None:
    emergency_merge_prs = common.get_emergency_merge_prs(repo.id, target_branch_name)
    if emergency_merge_prs:
        logger.info(
            "Repo %d found %d emergency merge PRs %s",
            repo.id,
            len(emergency_merge_prs),
            emergency_merge_prs,
        )
        for pr in emergency_merge_prs:
            if not _is_queue_paused(repo, pr.target_branch_name):
                handle_emergency_merge.delay(pr.id)


def parallel_mode_process_head(repo: GithubRepo, bot_pr_list: list[BotPr]) -> None:
    bot_pr = bot_pr_list[0]
    bot_pr.bind_contextvars()
    is_bisection_batch = bisection.is_parallel_bisection_batch(repo, bot_pr)

    if can_skip_process_top(bot_pr.id):
        logger.info("Repo %d draft PR %d skipping process top", repo.id, bot_pr.number)
        return

    access_token, client = common.get_client(repo)

    if not is_bisection_batch and top_changed(repo.id, bot_pr.pull_request.number):
        comments.post_pull_comment.delay(bot_pr.pull_request_id, "top")
        pilot_data = common.get_pilot_data(bot_pr.pull_request, "top_of_queue")
        hooks.call_master_webhook.delay(
            bot_pr.pull_request_id,
            "top_of_queue",
            pilot_data=pilot_data,
            ci_map={},
            note="",
        )
        bot_pr.pull_request.last_processed = time_util.now()
        db.session.commit()

    if _bot_pr_recently_created(bot_pr):
        logger.info(
            "Repo %d skipping parallel mode head check for PR %d as it was recently created",
            repo.id,
            bot_pr.number,
        )
        return

    if not is_bisection_batch and _check_top_pr_stuck(client, repo, bot_pr):
        logger.info(
            "Repo %d PR %d was stuck at the top, skipping parallel mode head check due to reset",
            repo.id,
            bot_pr.pull_request.number,
        )
        return

    bot_pull = client.get_pull(bot_pr.number)
    ensure_bot_pull_status(client, repo, bot_pull)
    db.session.refresh(bot_pr)
    if bot_pr.status == "closed":
        logger.info("BotPR was already closed")
        return

    decision = parallel_mode_check_top_botpr(
        client, repo, bot_pr, bot_pull, bot_pr_list[1:]
    )

    if decision.result == "failure":
        handle_failed_bot_pr(
            client,
            repo,
            bot_pr,
            True,
            decision.ci_map,
            status_code=StatusCode.FAILED_TESTS,
        )
        return
    elif decision.result == "pending":
        if check_pr_ci_within_timeout(client, repo, bot_pull, bot_pr.pull_request):
            # PR hasn't exceeded timeout and is still pending, nothing to do for now.
            return
        for pr in bot_pr.batch_pr_list:
            pr.set_status(
                "blocked",
                StatusCode.CI_TIMEDOUT,
                status_reason=(
                    "These checks were still pending when the timeout occurred:\n"
                    + _format_ci_list_markdown(list(decision.ci_map.keys()))
                ),
            )
        db.session.commit()
        handle_failed_bot_pr(client, repo, bot_pr, False, decision.ci_map)
        for batch_pr in bot_pr.batch_pr_list:
            update_gh_pr_status.delay(batch_pr.id)
        return
    elif decision.result == "success":
        pass
    else:
        TE.assert_never(decision.result)

    batch = Batch(client, bot_pr.batch_pr_list)
    # check that all PRs are mergeable and do not have new commits
    for batch_pr in bot_pr.batch_pr_list:
        if batch_pr.status == "merged":
            # The batch may have been partially merged, if so,
            # ignore the PRs that are already merged so we don't re-process them.
            continue
        pull = batch.get_pull(batch_pr.number)
        if pull.state == "closed":
            # if pr is merged, set the correct status, then handle failed botpr
            status_code = set_pr_as_closed_or_merged(
                client,
                batch_pr,
                merged=pull.merged,
                merged_at=pull.merged_at,
                merged_by_login=pull.merged_by.login if pull.merged_by else None,
                merge_commit_sha=pull.merge_commit_sha,
                set_bot_pr_failure=False,
            )
            if status_code == StatusCode.MERGED_MANUALLY:
                handle_failed_bot_pr(
                    client,
                    repo,
                    bot_pr,
                    False,
                    decision.ci_map,
                    failed_batch_pr=batch_pr,
                    skip_reset_queue=(
                        len(bot_pr.batch_pr_list) == 1 and is_bot_pr_top(bot_pr, repo)
                    ),
                )
                return
            if status_code == StatusCode.PR_CLOSED_MANUALLY:
                handle_failed_bot_pr(
                    client,
                    repo,
                    bot_pr,
                    False,
                    decision.ci_map,
                    failed_batch_pr=batch_pr,
                )
                return
            if update_bot_pr_on_pr_merge(client, repo, batch_pr, pull, bot_pr):
                return
        if batch_pr.head_commit_sha != pull.head.sha:
            # set the correct failure status code, then handle failed botpr
            logger.info(
                "Batch old commit sha %s and new commit sha %s",
                batch_pr.head_commit_sha,
                pull.head.sha,
            )
            bot_pr_set_failure(client, repo, batch_pr, StatusCode.COMMIT_ADDED)
            mark_ancestor_prs_blocked_by_top(client, batch_pr)
            logger.info(
                "Repo %d PR %d cannot be merged due to new commit, resetting batch with PRs %s",
                repo.id,
                batch_pr.number,
                bot_pr.batch_pr_list,
            )
            return

        if is_bisection_batch:
            bisection.handle_successful_bisection(client, bot_pr)
            close_pulls.delay(
                repo.id,
                [bot_pr.number],
                close_reason=(
                    "The parallel bisected batch passed successfully. "
                    "Associated PRs from the batch will be requeued."
                ),
            )
            return

        result = check_mergeability_for_merge(
            client, repo, batch_pr, pull, mark_blocked=False
        )
        status = result.test_result
        if status == "pending":
            if not result.ci_map:
                logger.info(
                    "Repo %d PR %d no pending CI checks", repo.id, batch_pr.number
                )
            # if ci is pending, report as stuck
            timed_out = parallel_mode_check_pr_stuck(
                client, repo, batch_pr, batch.get_pull(batch_pr.number), result.ci_map
            )
            if timed_out:
                common.set_pr_blocked(batch_pr, StatusCode.STUCK_TIMEDOUT)
                handle_failed_bot_pr(
                    client,
                    repo,
                    bot_pr,
                    False,
                    result.ci_map,
                    failed_batch_pr=batch_pr,
                )
            return
        elif status == "failure":
            handle_failed_bot_pr(
                client,
                repo,
                bot_pr,
                True,
                result.ci_map,
                failed_batch_pr=batch_pr,
                status_code=result.status_code,
            )
            logger.info(
                "PR is not mergeable, resetting the batch",
                repo_id=repo.id,
                pr_number=batch_pr.number,
                batch_pr_list=bot_pr.batch_pr_list,
                ci_map=list(result.ci_map.keys()),
            )
            return
        elif status == "success":
            # merge all PRs since they should be mergeable
            if repo.parallel_mode_config.use_fast_forwarding:
                batch_fast_forward_merge(
                    client,
                    repo,
                    batch,
                    bot_pull,
                    bot_pr,
                    ci_map={},
                    optimistic_botpr=decision.optimistic_botpr,
                )
            else:
                batch_merge(
                    client,
                    repo,
                    batch,
                    bot_pr,
                    bot_pull,
                    ci_map={},
                    optimistic_botpr=decision.optimistic_botpr,
                )
        else:
            TE.assert_never(status)


def _check_top_pr_stuck(client: GithubClient, repo: GithubRepo, bot_pr: BotPr) -> bool:
    """
    Check if the top PR is stuck.
    Automatically dequeue the PR if it has been at the top for over `stuck_pr_timeout_mins + ci_timeout` (if defined),
    and send a Slack alert.

    Determine the PR as not stuck if both `stuck_pr_timeout_mins` and `ci_timeout` are not set.

    :return bool: True if the PR is stuck, otherwise False.
    """
    pr: PullRequest = bot_pr.pull_request
    if _is_queue_paused(repo, pr.target_branch_name):
        return False

    # Make sure "top" Activity is the most recent for this pr
    most_recent_activity: Activity | None = (
        Activity.query.filter(
            Activity.repo_id == repo.id,
            Activity.pull_request_id == pr.id,
        )
        .order_by(Activity.id.desc())
        .first()
    )
    if not (most_recent_activity and most_recent_activity.name is ActivityType.TOP):
        return False

    time_pr_reached_top: datetime.datetime = _get_time_at_top(
        repo, pr, most_recent_activity
    )

    min_at_top = (time_util.now() - time_pr_reached_top).total_seconds() / 60

    # If no timeout is set, PR is not stuck.
    if not repo.parallel_mode_config.stuck_pr_timeout_mins and not repo.ci_timeout_mins:
        return False

    # We only dequeue PRs if there's a configured timeout.
    should_dequeue_pr = False
    allowed_timeout = 120
    if repo.parallel_mode_config.stuck_pr_timeout_mins and repo.ci_timeout_mins:
        should_dequeue_pr = True
        allowed_timeout = (
            repo.parallel_mode_config.stuck_pr_timeout_mins + repo.ci_timeout_mins
        )

    if min_at_top < allowed_timeout:
        return False

    logger.info(
        "Repo %d top PR %d exceeded timeout %d, at top for %d min",
        repo.id,
        pr.number,
        allowed_timeout,
        min_at_top,
    )
    # Send Slack alert
    if (
        repo.parallel_mode_config.stuck_pr_timeout_mins
        and repo.ci_timeout_mins
        and should_notify(repo.id, pr.number)
        and app.config.get("SLACK_NOTIFY_PROD_ALERTS_WEBHOOK")
    ):
        internal_webhooks.slack_notify_stuck_top_pr(
            repo=repo,
            pr=bot_pr.pull_request,
            bot_pr=bot_pr,
            pr_time=int(min_at_top),
            timeout=allowed_timeout,
        )

    if should_dequeue_pr:
        # Dequeue PR before resetting the queue.
        mark_as_blocked(
            client,
            repo,
            pr,
            client.get_pull(pr.number),
            StatusCode.STUCK_TIMEDOUT,
            ci_map={},
        )
        full_queue_reset(
            repo, StatusCode.RESET_BY_TIMEOUT, pr.target_branch_name, bot_pr=bot_pr
        )

    return should_dequeue_pr


def _get_time_at_top(
    repo: GithubRepo, pr: PullRequest, top_activity: Activity
) -> datetime.datetime:
    # Make sure we check for both repo unpaused and base branch unpaused.
    # Return the time associated with the unpaused or top activity (whichever is most recent).
    repo_unpause_activity: Activity | None = (
        Activity.query.filter(
            Activity.repo_id == repo.id,
            Activity.name == ActivityType.UNPAUSED,
            Activity.base_branch.is_(None),
            Activity.created > top_activity.created,
        )
        .order_by(Activity.id.desc())
        .first()
    )
    branch_unpause_activities: list[Activity] = (
        Activity.query.filter(
            Activity.repo_id == repo.id,
            Activity.name == ActivityType.UNPAUSED,
            Activity.created > top_activity.created,
        )
        .order_by(Activity.id.desc())
        .all()
    )
    branch_unpause_activities = [
        activity
        for activity in branch_unpause_activities
        if activity.base_branch
        and fnmatch.fnmatch(pr.target_branch_name, activity.base_branch)
    ]

    branch_unpause_activity: Activity | None = (
        branch_unpause_activities[0] if branch_unpause_activities else None
    )

    final_activity: Activity = top_activity

    if repo_unpause_activity:
        final_activity = _most_recent_activity(final_activity, repo_unpause_activity)
    if branch_unpause_activity:
        final_activity = _most_recent_activity(final_activity, branch_unpause_activity)

    return final_activity.created


def _most_recent_activity(activity1: Activity, activity2: Activity) -> Activity:
    return activity1 if activity1.created > activity2.created else activity2


def _format_ci_list_markdown(ci_list: list[str]) -> str:
    formatted = ""
    for ci in ci_list[:5]:
        formatted += f"\n- `{ci}`"
    if len(ci_list) > 5:
        formatted += f"\n- _{len(ci_list) - 5} checks omitted_"
    return formatted


def _format_pr_list_markdown(pr_list: list[PullRequest]) -> str:
    formatted = ""
    for pr in pr_list[:5]:
        formatted += f"\n- {pr.url}"
    if len(pr_list) > 5:
        formatted += f"\n- _{len(pr_list) - 5} more PRs_"
    return formatted


def batch_fast_forward_merge(
    client: GithubClient,
    repo: GithubRepo,
    batch: Batch,
    bot_pull: pygithub.PullRequest,
    bot_pr: BotPr,
    ci_map: dict[str, str],
    *,
    optimistic_botpr: BotPr | None,
) -> None:
    if _is_queue_paused(repo, batch.get_first_pull().base.ref):
        _comment_for_paused(batch.get_first_pr())
        return
    pr = batch.get_first_pr()
    pull = batch.get_first_pull()
    did_merge = merge_pr(
        client,
        repo,
        pr,
        pull,
        parallel_mode=True,
        bot_pull=bot_pull,
        batch=batch,
        optimistic_botpr=optimistic_botpr,
    )
    pr_ids = [pr.id for pr in batch.prs]
    if did_merge:
        for pr in batch.prs:
            handle_post_merge(
                client, repo, pr, batch.get_pull(pr.number), bot_pr, bot_pull
            )
        hooks.call_webhook_for_batch.delay(bot_pr.id, pr_ids, "batch_merged")
        process_top_async.delay(repo.id, batch.target_branch_name)
        tag_queued_prs_async.delay(repo.id)
    elif pr.status == "blocked":
        # this should not happen, bot_pr status will be inconsistent
        logger.error(
            "Repo %d PR %d failed to fast forward, bot_pr %d",
            repo.id,
            batch.get_first_pr().number,
            bot_pr.number,
        )
        handle_failed_bot_pr(
            client,
            repo,
            bot_pr,
            False,
            ci_map,
        )
        hooks.call_webhook_for_batch.delay(bot_pr.id, pr_ids, "batch_failed")
        return
    elif pr.status == "tagged":
        # we should only retry merge when the PR is tagged. if the PR is queued, it will eventually get tagged again.
        logger.error(
            "Repo %d PR %d fast forward interrupted, scheduling retry",
            repo.id,
            batch.get_first_pr().number,
        )
        retry_merge.delay(batch.get_first_pr().id)
    elif _is_queue_paused(repo, pull.base.ref):
        logger.info("Repo %d PR %d queue is paused", repo.id, pr.number)
        return
    else:
        logger.info(
            "Repo %d PR %d failed to fast forward, status is %s",
            repo.id,
            pr.number,
            pr.status,
        )


def batch_merge(
    client: GithubClient,
    repo: GithubRepo,
    batch: Batch,
    bot_pr: BotPr,
    bot_pull: pygithub.PullRequest,
    ci_map: dict[str, str],
    *,
    optimistic_botpr: BotPr | None,
) -> None:
    if _is_queue_paused(repo, batch.get_first_pull().base.ref):
        _comment_for_paused(batch.get_first_pr())
        return
    pr_ids = [pr.id for pr in batch.prs]
    needs_retry = False
    for pr in batch.prs:
        if pr.status in ("merged", "closed"):
            continue
        pull = batch.get_pull(pr.number)
        did_merge = merge_pr(
            client,
            repo,
            pr,
            pull,
            parallel_mode=True,
            bot_pull=bot_pull,
            batch=batch,
            optimistic_botpr=optimistic_botpr,
        )
        if did_merge:
            continue
        elif pr.status == "blocked":
            # this should not happen, bot_pr status will be inconsistent
            logger.warning(
                "Repo %d PR %d failed to merge (partially), bot_pr %d",
                repo.id,
                pr.number,
                bot_pr.number,
            )
            handle_failed_bot_pr(
                client,
                repo,
                bot_pr,
                False,
                ci_map,
                failed_batch_pr=pr,
            )
            hooks.call_webhook_for_batch.delay(bot_pr.id, pr_ids, "batch_failed")
            return
        elif _is_queue_paused(repo, pull.base.ref):
            logger.info("Repo %d PR %d queue is paused", repo.id, pr.number)
            return
        else:
            logger.info(
                "PR merge didn't finish, scheduling retry.",
                repo_id=repo.id,
                pr_number=pr.number,
            )
            retry_merge.delay(pr.id)
            needs_retry = True

    if not needs_retry:
        # Only close bot_pr when all PRs are merged. In case of retry, the bot_pr is
        # closed after retry is complete.
        handle_post_merge(client, repo, batch.prs[0], None, bot_pr, bot_pull)
        hooks.call_webhook_for_batch.delay(bot_pr.id, pr_ids, "batch_merged")
    process_top_async.delay(repo.id, batch.target_branch_name)
    tag_queued_prs_async.delay(repo.id)


def set_emergency_merge_for_pr(pr: PullRequest) -> None:
    repo: GithubRepo = pr.repo
    if not pr.emergency_merge:
        pr.set_emergency_merge(True)
        db.session.commit()
    if _is_queue_paused(repo, pr.target_branch_name):
        _, client = common.get_client(repo)
        client.create_issue_comment(
            pr.number,
            "This PR cannot be merged right now because the queue is paused. "
            "It will be merged immediately once the queue is resumed.",
        )
        return
    if pr.status not in ("merged", "closed"):
        handle_emergency_merge.delay(pr.id)
    logger.info(
        "Triggered emergency merge", repo_id=repo.id, pr_id=pr.id, pr_number=pr.number
    )


@celery.task(
    autoretry_for=(Exception,),
    retry_backoff=10,
    retry_kwargs={"max_retries": 5},
    retry_jitter=True,
)
def handle_emergency_merge(pr_id: int) -> None:
    """
    This logic is similar to batch_merge, but during break-glass
    we need special handling to ensure that the bot PR is closed if it exists.
    We will also reset the queue
    """
    pr = PullRequest.get_by_id_x(pr_id)
    pr.bind_contextvars()
    repo = pr.repo
    repo.bind_contextvars()
    if not pr.emergency_merge:
        logger.error(
            "Invalid emergency merge (pull request is not marked for emergency merge)",
            repo_id=repo.id,
            pr_id=pr.id,
            pr_number=pr.number,
        )
        return
    _handle_emergency_merge_with_lock(pr)


def _handle_emergency_merge_with_lock(pr: PullRequest) -> None:
    repo = pr.repo
    with locks.for_repo(repo):
        _, client = common.get_client(repo)
        pull = client.get_pull(pr.number)
        # refresh the PR as it might be stale
        # https://docs.sqlalchemy.org/en/13/orm/session_state_management.html#when-to-expire-or-refresh
        db.session.refresh(pr)
        bot_pr: BotPr | None = _get_most_recent_bot_pr(pr)
        pr.queued_at = time_util.now()
        db.session.commit()
        did_merge = merge_pr(
            client,
            repo,
            pr,
            pull,
            repo.parallel_mode,
            optimistic_botpr=None,
        )
        logger.info(
            "Attempted emergency merge",
            repo_id=repo.id,
            pr_id=pr.id,
            pr_number=pr.number,
            did_merge=did_merge,
        )
        if did_merge:
            # The BotPR might not be needed anymore. Close it if needed.
            if bot_pr:
                bot_pr.bind_contextvars()
                if all(p.status == "merged" for p in bot_pr.batch_pr_list):
                    logger.info(
                        "Closing a BotPR since all of its PRs are merged/closed",
                        batch_pr_numbers=[p.number for p in bot_pr.batch_pr_list],
                    )
                    bot_pull = client.get_pull(bot_pr.number)
                    # mypy complains that latest_bot_pr may be null, but that's an
                    # application invariant if we're here, so just raise an assertion.
                    assert bot_pr, (
                        f"bot_pr should exist for repo {repo.id} PR {pr.number}"
                    )
                    handle_post_merge(
                        client,
                        repo,
                        pr,
                        client.get_pull(pr.number),
                        bot_pr,
                        bot_pull,
                    )
                else:
                    logger.info(
                        "Keeping a BotPR since some of its PRs are not merged/closed",
                        batch_pr_numbers=[pr.number for pr in bot_pr.batch_pr_list],
                    )

            # Not resetting here to avoid a lot of time wasted by the queue. This may
            # cause some issues if we try to construct the PR with the latest master and
            # one of the pending draft PRs. Today this only happens during partial resets,
            # so the chance of that happening is relatively low. We should also just redo
            # our logic on generating draft PRs, but that's a bigger change.
            return

        if pr.status == "blocked":
            # this should not happen, bot_pr status will be inconsistent
            logger.warning(
                "Break glass pull request failed to merge",
                repo_id=repo.id,
                pr_id=pr.id,
                pr_number=pr.number,
            )
            bot_pr = pr.latest_bot_pr
            if bot_pr:
                handle_failed_bot_pr(
                    client,
                    repo,
                    bot_pr,
                    False,
                    ci_map={},
                    failed_batch_pr=pr,
                )
                full_queue_reset(
                    repo,
                    pr.status_code,
                    pr.target_branch_name,
                )
                return
        if _is_queue_paused(repo, pull.base.ref):
            logger.info(
                "Queue is paused", repo_id=repo.id, pr_number=pr.number, pr_id=pr.id
            )
            return

        fetch_pr.delay(pr.number, repo.name)

        # if pr is still queued, we should raise exception so we can attempt again.
        raise Exception(
            "PR merge didn't finish, scheduling retry. Repo %d PR %d"
            % (repo.id, pr.number)
        )


def _get_most_recent_bot_pr(pr: PullRequest) -> BotPr | None:
    """
    This method is different from # pr.latest_bot_pr because this does not take
    into account the `pr.queued_at`. Only used by `handle_emergency_merge` to go
    around the scenario where we update the queued_at value.
    """
    if pr.status == "tagged":
        bot_pr: BotPr | None = (
            BotPr.query.filter(BotPr.batch_pr_list.any(PullRequest.id == pr.id))
            .order_by(BotPr.id.desc())
            .first()
        )
        return bot_pr
    return None


@celery.task(
    autoretry_for=(Exception,),
    retry_backoff=10,
    retry_kwargs={"max_retries": 5},
    retry_jitter=True,
)
def retry_merge(pr_id: int) -> None:
    pr: PullRequest | None = PullRequest.get_by_id_x(pr_id)
    assert pr, f"PR {pr_id} not found"
    repo = pr.repo
    with locks.for_repo(repo):
        # refresh the PR as it might be stale
        # https://docs.sqlalchemy.org/en/13/orm/session_state_management.html#when-to-expire-or-refresh
        db.session.refresh(pr)
        bot_pr = pr.latest_bot_pr
        if not bot_pr:
            logger.info(
                "Repo %d PR %d retry_merge called but has no latest BotPR",
                repo.id,
                pr.number,
            )
            return
        if pr.status in ("merged", "closed"):
            if bot_pr.status != "closed":
                bot_pr.set_status("closed", StatusCode.FAILED_UNKNOWN)
                db.session.commit()
            close_pulls(repo.id, [bot_pr.number])

            logger.info(
                "Repo %d PR %d retry_merge called but PR already merged",
                repo.id,
                pr.number,
            )
            return

        if pr.status not in ("queued", "tagged"):
            logger.info(
                "Repo %d PR %d retry_merge called (BotPR %d) but PR not ready (status %s)",
                repo.id,
                pr.number,
                bot_pr.number,
                pr.status,
            )
            return

        access_token, client = common.get_client(repo)
        pull = client.get_pull(pr.number)
        bot_pull = client.get_pull(bot_pr.number)

        if bot_pull.merged:
            logger.info(
                "Repo %d PR %d retry_merge called (BotPR %d) but BotPR already merged (ff)",
                repo.id,
                pr.number,
                bot_pr.number,
            )
            return

        _did_merge = merge_pr(
            client,
            repo,
            pr,
            pull,
            bot_pull=bot_pull,
            parallel_mode=True,
            # TODO: do we need to provide this?
            optimistic_botpr=None,
        )

        # Tell mypy that the pr might have changed.
        # Without this it complains because we checked for `pr.status == "merged"
        # above and are re-checking it below.
        pr = pr

        if pr.status == "blocked":
            handle_failed_bot_pr(
                client,
                repo,
                bot_pr,
                False,
                ci_map={},
                failed_batch_pr=pr,
            )
            pr_ids = [p.id for p in bot_pr.batch_pr_list]
            hooks.call_webhook_for_batch.delay(bot_pr.id, pr_ids, "batch_failed")
        if pr.status != "merged":
            raise errors.MergeFailed(
                "Failed to merge PR %d repo %d" % (pr.number, repo.id)
            )
        logger.info("Repo %d PR %d retry_merge success", repo.id, pr.number)

        # Ensure all merged
        for p in bot_pr.batch_pr_list:
            if p.status != "merged":
                logger.info(
                    "Retry merge: batch not entirely merged yet",
                    repo_id=repo.id,
                    pr_number=pr.number,
                    bot_pr=bot_pr.number,
                    pending_pr=p.number,
                )
                return

        handle_post_merge(
            client, repo, pr, pull, bot_pr, client.get_pull(bot_pr.number)
        )
        pr_ids = [p.id for p in bot_pr.batch_pr_list]
        hooks.call_webhook_for_batch(bot_pr.id, pr_ids, "batch_merged")


@dataclasses.dataclass
class CheckTopBotPRDecision:
    #: The result of the check.
    result: checks.TestResult
    #: A mapping of CI check name to URL.
    ci_map: dict[str, str]
    #: The BotPR that caused the optimistic check to succeed.
    #: This is set if and only if the overall result is "success" AND the target
    #: BotPR was considered successful because of a subsequent BotPR's success.
    #: If set, this is always a different BotPR than the `botpr` field.
    optimistic_botpr: BotPr | None


def parallel_mode_check_top_botpr(
    client: GithubClient,
    repo: GithubRepo,
    target_botpr: BotPr,
    target_bot_pull: pygithub.PullRequest,
    subsequent_botprs: list[BotPr],
) -> CheckTopBotPRDecision:
    """
    Determine if we should consider the target BotPR as success.

    For repositories configured with optimistic validation, we will check all
    subsequent BotPRs to see if any of them are passing. If so, we will consider
    the target BotPR as passing as well.
    """
    target_test_summary = checks.fetch_latest_test_statuses(
        client,
        repo,
        target_bot_pull,
        # If we're re-using the original PR (not creating a new BotPR), we use
        # the non-BotPR set of checks. If this is not desirable (the customer
        # wants to ensure that all BotPR checks are validated), they can disable
        # the `skip_draft_when_up_to_date` setting.
        # This only matters at all if the repository has configured the
        # `parallel_mode.override_required_checks` setting.
        botpr=target_botpr.pull_request.number != target_botpr.number,
    )
    custom_checks.update_test_statuses_from_summary(target_test_summary, target_botpr)

    should_perform_optimistic_check = (
        # Only perform the optimistic check if repositories have explicitly
        # opted-in to it.
        repo.parallel_mode_config.use_optimistic_validation
        # We don't check subsequent BotPRs if the target BotPR is a "skip when
        # up to date" BotPR (since if it's failing, it'll seem like MergeQueue
        # merged a failing PR into trunk).
        and target_botpr.pull_request.number != target_botpr.number
        # If the target BotPR is already passing, we don't need to check
        # subsequent BotPRs.
        and target_test_summary.result != "success"
    )
    if not should_perform_optimistic_check:
        return CheckTopBotPRDecision(
            target_test_summary.result,
            target_test_summary.check_urls,
            optimistic_botpr=None,
        )

    # To avoid excessive API calls, only read 3 levels down, or up to the failure depth.
    depth = max(3, repo.optimistic_validation_failure_depth)

    # Check if any of the subsequent BotPRs is still pending.
    # If any of them are passing, we assume that the target BotPR will also pass
    # (and/or just had a flaky test).
    has_pending_botpr: bool = False
    failure_count = 1 if target_test_summary.result == "failure" else 0
    for botpr in subsequent_botprs[:depth]:
        bot_pull = client.get_pull(botpr.number)
        if bot_pull.head.sha != botpr.head_commit_sha:
            # MER-2614: We have noticed that GH bot_pull returns a stale SHA sometimes.
            # In these scenarios, ideally we want to validate the SHA we have captured
            # in our DB vs what GH tells us. This requires a bit more surgery as we go by
            # pull object rather than SHA to fetch and validate statuses.
            # Logging this as error first to see how often this happens.
            logger.error(
                "Found BotPR head commit mismatch",
                repo_id=repo.id,
                botpr_number=botpr.number,
                expected_commit=botpr.head_commit_sha,
                actual_commit=bot_pull.head.sha,
            )
        test_summary = checks.fetch_latest_test_statuses(
            client,
            repo,
            bot_pull,
            botpr=True,
        )
        custom_checks.update_test_statuses_from_summary(test_summary, botpr)
        if test_summary.result == "pending":
            has_pending_botpr = True
        for pr in botpr.batch_pr_list:
            update_gh_pr_status.delay(pr.id)
        if test_summary.result == "failure":
            failure_count += 1
        logger.info(
            "Checking BotPR for optimistic check",
            repo_id=repo.id,
            target_botpr_number=target_bot_pull.number,
            current_botpr_number=botpr.number,
            current_botpr_result=test_summary.result,
        )
        if test_summary.result == "success":
            logger.info(
                "BotPR is success using optimistic check",
                repo_id=repo.id,
                target_botpr_number=target_botpr.number,
                current_botpr_number=botpr.number,
            )
            # TODO[travis]: We should probably move this comment posting closer
            #   to where we actually do the merging.
            comments.add_optimistic_comment.delay(
                repo.id, target_bot_pull.number, botpr.number
            )
            return CheckTopBotPRDecision(
                "success",
                target_test_summary.check_urls,
                optimistic_botpr=botpr,
            )

    # If the target BotPR is considered a failure, but we haven't yet hit the
    # configured failure depth, consider this pull request as pending.
    if (
        target_test_summary.result == "failure"
        and failure_count < repo.optimistic_validation_failure_depth
        # We also require at least some BotPR to be pending. If all of them are failure (it
        # can't be success here since we return early above in that case), then
        # there's no real way for the optimistic check to succeed. It makes more
        # sense to just fail now in that case.
        and has_pending_botpr
    ):
        return CheckTopBotPRDecision(
            "pending",
            target_test_summary.check_urls,
            optimistic_botpr=None,
        )

    # The pull request is either pending or failure, and none of the subsequent
    # BotPRs were passing.
    return CheckTopBotPRDecision(
        target_test_summary.result,
        target_test_summary.check_urls,
        optimistic_botpr=None,
    )


@dataclasses.dataclass
class MergeabilityDecision:
    mergeable: bool
    ci_map: dict[str, str]
    # A human readable description of why this decision was made
    # (only if mergeable is False).
    reason: str | None


def check_mergeability_for_queue(
    client: GithubClient,
    repo: GithubRepo,
    pull: pygithub.PullRequest,
    pr: PullRequest,
    force_validate: bool = False,
) -> MergeabilityDecision:
    """
    Check whether or not the PR passes the configured repo mergeability checks.

    This is mostly useful for parallel mode where a repository can have separate
    checks configured for original and draft PRs, and we wish to wait for the
    original PR to pass mergeability checks before creating a draft PR.
    """
    skip_validation = pr.merge_operations and pr.merge_operations.skip_validation
    if skip_validation:
        logger.info(
            "Skip CI PR found while checking mergeability to queue",
            pr_number=pr.number,
            repo_id=repo.id,
        )
    details = checks.fetch_latest_test_statuses(
        client,
        repo,
        pull,
        botpr=False,
    )
    update_gh_pr_status.delay(pr.id)

    # In case of skip_validation, we will still fetch the status, so we can update in the PR status
    # but always set the final status to success regardless of the actual result.
    status = "success" if skip_validation else details.result
    ci_map = details.check_urls
    reason = None
    if repo.require_all_checks_pass and not ci_map and not skip_validation:
        reason = "The HEAD commit of this pull request has no associated CI checks."

    if pull.mergeable is False or pull.mergeable_state == "dirty":
        logger.info(
            "Repo %d PR %d is not queueable (mergeable=%s, mergeable_state=%s)",
            repo.id,
            pr.number,
            pull.mergeable,
            pull.mergeable_state,
        )
        return MergeabilityDecision(mergeable=False, ci_map=ci_map, reason=reason)

    if (
        pull.mergeable is None or pull.mergeable_state == "unknown"
    ) and status == "success":
        # GitHub would sometimes not tell us whether the PR is mergeable (no merge conflict),
        # so we force validate it here by creating a merge commit if the CI has already passed.
        if not force_validate:
            raise errors.GithubMergeabilityPendingException(
                "Unknown Github mergeability for repo %d PR %d" % (repo.id, pr.number)
            )
        if not _verify_mergeability(client, repo, pull):
            return MergeabilityDecision(mergeable=False, ci_map=ci_map, reason=reason)

    logger.info(
        "Mergeability status for queue",
        repo_id=repo.id,
        pr_number=pr.number,
        status=status,
        ci_map=list(ci_map.keys()),
        sha=pull.head.sha,
    )
    return MergeabilityDecision(
        mergeable=(status == "success"), ci_map=ci_map, reason=reason
    )


def handle_failed_bot_pr(
    client: GithubClient,
    repo: GithubRepo,
    bot_pr: BotPr,
    update_pr_status: bool,
    ci_map: dict[str, str],
    failed_batch_pr: PullRequest | None = None,
    skip_reset_queue: bool = False,
    status_code: StatusCode | None = None,
) -> None:
    """
    This method will close the BotPR, and block failed_batch_pr if it exists (see param).
    The remaining PRs will be requeued with bisection.
    The queue is then reset by updating all subsequent BotPrs.

    :param failed_batch_pr: If this PR value is provided, we can assume that this PR caused the batch to fail.
        That means failed_batch_pr should be blocked and all other PRs in the batch should be requeued.
        If none, we assume that the BotPr failed, so all PRs in the batch should be requeued with bisection.
    :param skip_reset_queue: If this is set to true, the bot PR will be closed and the database will
        be updated but the queue will not be reset.
    :param status_code: StatusCode should be provided if update_pr_status is True
    """
    batch = Batch(client, bot_pr.batch_pr_list)

    db.session.refresh(repo)
    if not repo.parallel_mode:
        logger.info(
            "Not re-syncing Repo %d PR %d BotPR %d (repo is no longer in parallel mode)",
            repo.id,
            bot_pr.pull_request.number,
            bot_pr.number,
        )
        return

    if all(pr.status == "merged" and pr.emergency_merge for pr in batch.prs):
        # Figma uses emergency merge for the queued PRs. When this happens, the
        # emergency merge task closes the BotPR in GitHub, and later in another task
        # we detect the BotPR test failure (the failure is caused by the PR closure).
        #
        # Since emergency merge shouldn't cause disruption, we will just skip processing
        # this BotPR.
        logger.info(
            "All PRs in a batch are emergency-merged. Ignore this BotPR failure."
        )
        return

    prs_to_requeue = []
    failed_prs = []
    if failed_batch_pr:
        failed_prs = [failed_batch_pr]
        prs_to_requeue = [
            pr
            for pr in batch.prs
            if pr.number != failed_batch_pr.number and pr.status == "tagged"
        ]
    else:
        if len(bot_pr.batch_pr_list) == 1:
            failed_prs = bot_pr.batch_pr_list
        else:
            # we assume the BotPr failed, so requeue all PRs.
            # these PRs will eventually be rebatched with bisection.
            prs_to_requeue = bot_pr.batch_pr_list

    for failed_pr in failed_prs:
        pull = batch.get_pull(failed_pr.number)
        if not pull.merged:
            client.add_label(pull, repo.blocked_label)
            if update_pr_status:
                if not status_code:
                    raise Exception(
                        "status_code must be provided if update_pr_status is True"
                    )
                common.set_pr_blocked(failed_pr, status_code)

    # Automatically requeue all the non-problematic PRs in the batch
    bisection.requeue_prs(prs_to_requeue, repo, failed_batch_pr, bot_pr)
    bot_pr.set_status(
        "closed",
        failed_batch_pr.status_code if failed_batch_pr else StatusCode.FAILED_TESTS,
    )
    db.session.commit()

    _removed_from_batch(bot_pr.batch_pr_list)
    for p in prs_to_requeue:
        update_gh_pr_status.delay(p.id)

    # MER-2104: Close this PR after updating the DB to avoid any race conditions
    # with inconsistent state between Github and DB.
    if not batch.did_optimistically_reuse_original_pr(bot_pr):
        close_reason = None
        if failed_batch_pr:
            close_reason = (
                f"Batch failed due to PR #{failed_batch_pr.number}, "
                + f"reason: {failed_batch_pr.status_code.message()}"
            )
        close_pulls.delay(repo.id, [bot_pr.number], close_reason=close_reason)

    failed_pr_numbers = [pr.number for pr in failed_prs]
    requeued_pr_numbers = [pr.number for pr in prs_to_requeue]
    if failed_pr_numbers:
        logger.info(
            "The following PR failed in the batch",
            repo_id=repo.id,
            failed_prs=failed_pr_numbers,
            status_code=failed_prs[0].status_code,
        )
    if requeued_pr_numbers:
        logger.info("Repo %d PR %s have been requeued", repo.id, requeued_pr_numbers)
    for pr in failed_prs:
        if pr.status not in ("merged", "closed"):
            send_pr_update_to_channel.delay(pr.id)
            requeue_or_comment.delay(pr.id, ci_map)

    # Ignore the reset if requested explicitly or if we are using parallel bisection mode.
    if skip_reset_queue or bisection.is_parallel_bisection_batch(repo, bot_pr):
        logger.info(
            "Queue reset skipped",
            repo_id=repo.id,
            bot_pr_number=bot_pr.number,
            use_parallel_bisection=repo.parallel_mode_config.use_parallel_bisection,
        )
        return

    # now we need to resync all existing bot-PRs
    if repo.parallel_mode_config.use_affected_targets:
        batch_pr_id_list = [p.id for p in bot_pr.batch_pr_list]
        bot_prs = target_manager.get_dependent_bot_prs(repo, batch_pr_id_list)

        # Since both failed and requeued PRs are pushed to the end of the queue,
        # we will remove the associated dependencies.
        # The requeued PRs will then get the dependencies recalculated when those
        # are tagged in tag_new_batch.
        prs_to_remove_deps = failed_prs + bot_pr.batch_pr_list
        target_manager.refresh_dependency_graph_on_reset(repo, prs_to_remove_deps)
    else:
        # Find all BotPRs after the failed one and resync them.
        bot_prs = (
            BotPr.query.filter_by(status="queued", repo_id=repo.id)
            .join(PullRequest, BotPr.pull_request_id == PullRequest.id)
            .filter(PullRequest.target_branch_name == batch.target_branch_name)
            .filter(BotPr.id > bot_pr.id)
            .order_by(BotPr.id.asc())
            .all()
        )

    previous_bot_prs = (
        BotPr.query.filter_by(status="queued", repo_id=repo.id)
        .join(PullRequest, BotPr.pull_request_id == PullRequest.id)
        .filter(PullRequest.target_branch_name == batch.target_branch_name)
        .filter(BotPr.id < bot_pr.id)
        .order_by(BotPr.id.asc())
        .all()
    )

    # Exclude bisected
    if repo.parallel_mode_config.use_parallel_bisection:
        bot_prs = [bp for bp in bot_prs if not bp.is_bisected_batch]
        previous_bot_prs = [bp for bp in previous_bot_prs if not bp.is_bisected_batch]

    sync_branch = (
        previous_bot_prs[-1].branch_name
        if previous_bot_prs
        else batch.target_branch_name
    )
    previous_bot_pr = previous_bot_prs[-1] if previous_bot_prs else None
    previous_prs = []
    for pbr in previous_bot_prs:
        previous_prs.extend(pbr.batch_pr_list)

    # NOTE: requeued_pr_numbers are PRs that is in the failed batch. We need
    # to add the PRs that are in other batches.
    resync_pr_count = len(prs_to_requeue)
    closed_bot_pr_count = 0
    for bot_pr_for_resync in bot_prs:
        if (
            bot_pr_for_resync.pull_request.target_branch_name
            == batch.target_branch_name
        ):
            closed_bot_pr_count += 1
            resync_pr_count += len(bot_pr_for_resync.batch_pr_list)
            new_sync_branch = resync_bot_pr(
                client,
                repo,
                bot_pr_for_resync,
                previous_branch=sync_branch,
                previous_prs=previous_prs,
                previous_bot_pr=previous_bot_pr,
            )
            # Update the previous PRs list if resync was successful.
            if new_sync_branch:
                assert bot_pr_for_resync.head_commit_sha is not None, "Missing head SHA"
                activity.create_activity(
                    repo_id=repo.id,
                    pr_id=bot_pr_for_resync.pull_request_id,
                    activity_type=ActivityType.RESYNC,
                    status_code=StatusCode.RESET_BY_FAILURE,
                    payload=ActivityResyncPayload(
                        triggering_bot_pr_id=bot_pr.id,
                        new_bot_pr_head_commit_oid=bot_pr_for_resync.head_commit_sha,
                    ),
                )
                previous_prs.extend(bot_pr_for_resync.batch_pr_list)
                sync_branch = new_sync_branch
                previous_bot_pr = bot_pr_for_resync

    pr_for_reset_event: PullRequest | None = (
        batch.get_first_pr() if len(batch.prs) == 1 else None
    )
    reset_code = StatusCode.get_reset_reason(bot_pr.status_code)
    _record_reset_event(
        repo.id,
        reset_code,
        pr_for_reset_event,
        reset_count=resync_pr_count,
        closed_bot_pr_count=closed_bot_pr_count,
        ci_map=ci_map,
        bot_pr=bot_pr,
    )
    logger.info("Repo %d resync complete for %d PRs", repo.id, len(bot_prs))
    tag_queued_prs_async.delay(repo.id)


def _removed_from_batch(pr_list: list[PullRequest]) -> None:
    for pr in pr_list:
        pilot_data = common.get_pilot_data(pr, "removed_from_batch")
        hooks.call_master_webhook.delay(
            pr.id, "removed_from_batch", pilot_data=pilot_data, ci_map={}, note=""
        )


@celery.task
def requeue_or_comment(pr_id: int, ci_map: dict[str, str]) -> None:
    """
    If the PR can be re-queued, requeue it and skip comments. We only check
    for requeuing if it's enabled and one of the CI has failed.
    """
    pr = PullRequest.get_by_id_x(pr_id)
    repo = pr.repo
    # We always want to check for requeue attempts when a PR is blocked because of a new commit added.
    # The CI list is irrelevant since there is likely no failing CI (the new commit should have triggered a CI rerun).
    if (
        repo.parallel_mode
        and repo.parallel_mode_config.max_requeue_attempts
        and (ci_map or pr.status_code == StatusCode.COMMIT_ADDED)
    ):
        attempts = get_requeue_attempts(repo.id, pr.id)
        if attempts > repo.parallel_mode_config.max_requeue_attempts:
            comment_blocked_with_ci(pr, ci_map)
            return

        _, client = common.get_client(repo)
        pull = client.get_pull(pr.number)
        if not _requeue_pr_if_ci_updated(client, repo, pull, pr):
            comment_blocked_with_ci(pr, ci_map)
            return

        fetch_pr.delay(pr.number, repo.name)
        return

    comment_blocked_with_ci(pr, ci_map)


def comment_blocked_with_ci(pr: PullRequest, ci_map: dict[str, str]) -> None:
    comments.post_pull_comment.delay(pr.id, "blocked", ci_map=ci_map)
    pilot_data = common.get_pilot_data(pr, "blocked")
    hooks.call_master_webhook.delay(
        pr.id,
        "blocked",
        pilot_data=pilot_data,
        ci_map={},
        note="",
        status_code_int=pr.status_code.value,
    )


def handle_post_merge(
    client: GithubClient,
    repo: GithubRepo,
    pr: PullRequest,
    pull: pygithub.PullRequest | None,
    bot_pr: BotPr,
    bot_pull: pygithub.PullRequest,
    retry: bool = True,
) -> None:
    """
    TODO:
        Our post-merge actions are split across a few different places (namely
        update_prs_post_merge which is called by merge_pr as well as this
        function which is called by batch_merge, batch_fast_forward_merge,
        and retry_merge (all three of which call merge_pr internally).
    """
    logger.info("Repo %d PR %d merged", repo.id, pr.number)
    if bot_pr.number != pr.number:
        try:
            if _should_merge_draft_pr(pr):
                # In case of fast forwarding, the original PR is not closed,
                # so we add a label identifying it as merged, and close manually.
                # In this case, since we fast forward the bot_pull, that is reported
                # as merged and we only need to delete the branch reference there.
                if not pull:
                    raise Exception(
                        f"pull object cannot be empty when merging a draft PR {pr.number} repo {repo.id}"
                    )
                client.add_label(pull, MERGED_BY_MQ_LABEL)
                client.close_pull(pull)
                delete_branch_if_applicable(client, repo, pull)
                comments.post_pull_comment.delay(
                    pr.id, "merged", note=str(bot_pr.number)
                )
            else:
                if bot_pull.state != "closed":
                    client.close_pull(bot_pull)
            client.delete_ref(bot_pull.head.ref)
        except Exception as e:
            logger.warning(
                "Failed to handle post merge",
                repo_id=repo.id,
                pr_number=pr.number,
                retry=retry,
                exc_info=e,
            )
            if retry:
                handle_post_merge(client, repo, pr, pull, bot_pr, bot_pull, retry=False)
            else:
                logger.error(
                    "Failed to handle post merge",
                    repo_id=repo.id,
                    pr_number=pr.number,
                    exc_info=e,
                )

    if bot_pr.status != "closed":
        bot_pr.set_status("closed", StatusCode.MERGED_BY_MQ)
        db.session.commit()
    target_manager.remove_dependencies(repo, pr)


def _should_merge_draft_pr(pr: PullRequest) -> bool:
    return (
        bool(pr.repo.parallel_mode_config.use_fast_forwarding)
        and pr.repo.parallel_mode
        and not pr.emergency_merge
    )


def mark_ancestor_prs_merged_by_bot(client: GithubClient, pr: PullRequest) -> None:
    """
    Mark every ancestor PR as merged by the Aviator bot.
    """
    ancestor_prs = stack_manager.get_all_stack_ancestors(pr)
    repo = pr.repo
    logger.info(
        "Repo %d PR %d marking stack ancestors as merged: %s",
        repo.id,
        pr.number,
        ancestor_prs,
    )
    for ancestor_pr in ancestor_prs:
        util.posthog_util.capture_pull_request_event(
            util.posthog_util.PostHogEvent.MERGE_PULL_REQUEST,
            ancestor_pr,
        )
        ancestor_pr.set_status("merged", StatusCode.MERGED_BY_MQ)
        ancestor_pr.merged_at = time_util.now()
        try:
            pull = client.get_pull(ancestor_pr.number)
            if ancestor_pr.merge_commit_sha is None:
                ancestor_pr.merge_commit_sha = pr.merge_commit_sha
            if ancestor_pr.merge_commit_sha is None:
                # if commit sha is still None, we should log.
                logger.error(
                    "Missing merge commit SHA for a merged PR",
                    pr_number=ancestor_pr.number,
                )
            client.add_label(pull, MERGED_BY_MQ_LABEL)
            client.close_pull(pull)
            delete_branch_if_applicable(client, repo, pull)
            comments.post_pull_comment.delay(
                ancestor_pr.id, "merged", note=str(pr.number)
            )
        except Exception as e:
            logger.error(
                "Failed to mark as merged by bot",
                repo_id=repo.id,
                pr_number=pr.number,
                exc_info=e,
            )
    db.session.commit()
    for ancestor_pr in ancestor_prs:
        update_gh_pr_status.delay(ancestor_pr.id)
        flexreview.celery_tasks.process_merged_pull.delay(ancestor_pr.id)
    atc.non_released_prs.update_non_released_prs.delay(pr.repo_id)


def mark_ancestor_prs_blocked_by_top(client: GithubClient, pr: PullRequest) -> None:
    """
    Mark every ancestor PR as blocked by top
    """
    ancestor_prs = stack_manager.get_all_stack_ancestors(pr)
    repo = pr.repo
    logger.info(
        "Repo %d PR %d marking stack ancestors as blocked by top: %s",
        repo.id,
        pr.number,
        ancestor_prs,
    )
    for ancestor_pr in ancestor_prs:
        common.set_pr_blocked(
            ancestor_pr,
            StatusCode.BLOCKED_BY_STACK_TOP,
            f"Top queued PR #{pr.number}.",
        )
        try:
            pull = client.get_pull(ancestor_pr.number)
            client.add_label(pull, repo.blocked_label)
            comments.post_pull_comment.delay(ancestor_pr.id, "blocked")
        except Exception as e:
            logger.error(
                "Failed to mark as blocked by top",
                repo_id=repo.id,
                pr_number=pr.number,
                exc_info=e,
            )
    db.session.commit()
    for ancestor_pr in ancestor_prs:
        update_gh_pr_status.delay(ancestor_pr.id)


class _InvalidSquashCommitError(Exception):
    pass


def resync_bot_pr(
    client: GithubClient,
    repo: GithubRepo,
    bot_pr: BotPr,
    *,
    previous_branch: str,
    previous_prs: list[PullRequest],
    previous_bot_pr: BotPr | None,
    attempt: int = 1,
) -> str | None:
    """
    Resync a BotPR against the given previous branch.
    :param client: Github client
    :param repo: GithubRepo
    :param bot_pr: The BotPR that's being resynced.
    :param previous_branch: The base branch to resync against (i.e., the branch
        corresponding to the previous BotPR).
    :param previous_prs: The list of all PRs whose commit were included in the previous BotPR.
    :param previous_bot_pr: The previous BotPR, if any.
    :return: The name of the resynced branch, or None if the resync failed.
    """
    bot_pull = client.get_pull(bot_pr.number)
    slog = logger.bind(
        repo_id=repo.id,
        bot_pr_id=bot_pr.id,
        bot_pr_number=bot_pr.number,
        bot_pr_branch=bot_pull.head.ref,
        previous_branch=previous_branch,
        previous_pr_numbers=[pr.number for pr in previous_prs],
        attempt=attempt,
    )
    slog.info("Resyncing BotPR")
    batch = Batch(client, bot_pr.batch_pr_list)
    if batch.did_optimistically_reuse_original_pr(bot_pr):
        slog.error("Cannot resync BotPR because it's re-using the original PR")
        return bot_pull.head.ref

    # in order to resync the botPR, we need to
    # 1 - force-push the branch_to_sync to the bot_pull (this removes changes from that branch)
    # 2 - apply the changes from the associated original PRs to the bot_pr
    tmp_branch_name = None
    db.session.refresh(repo)
    try:
        if not repo.parallel_mode:
            # Not a parallel mode anymore, this likely was changed in-flight.
            # We can't do anything about it, so we just return the current branch.
            slog.info("Repository is not in parallel mode, aborting resync")
            return None

        if repo.parallel_mode_config.use_affected_targets:
            # In case of affected targets, we will override the previous_prs as we
            # only account for blocking PRs. Also handle the stacked PRs case, where the blocking
            # PRs will be a collection of all blocking PRs for all the PRs in the stack.
            previous_prs_set: set[PullRequest] = set()
            for pr in batch.prs:
                for stacked_pr in stack_manager.get_current_stack_ancestors(pr):
                    previous_prs_set.update(stacked_pr.blocking_prs)

            previous_prs = list(previous_prs_set - set(batch.prs))

        if (
            repo.parallel_mode_config.use_affected_targets
            and previous_bot_pr
            and not _are_previous_prs_included(
                previous_prs,
                latest_bot_pr=previous_bot_pr,
            )
        ):
            # In case of target mode, we take the latest target branch and
            # then apply all the relevant changes on top of it.
            # If the previous bot PR already includes all the changes, we don't need
            # to construct the botPR from scratch. In that case we will skip this loop.
            latest_sha = client.get_branch_head_sha(batch.target_branch_name)
            tmp_branch_name = client.copy_to_new_branch(
                latest_sha, str(batch.get_first_pr().number), prefix="mq-tmp-"
            )
            for pr in batch.prs:
                pull = batch.get_pull(pr.number)
                client.merge_branch_to_branch(pull.head.ref, tmp_branch_name)

            # Collect all the previous blocking and apply those to the batch.
            apply_prs_to_branch(
                client, repo, batch.prs[0], previous_prs, tmp_branch_name
            )
            # Update pr_list (these are previous PR commits that are included)
            bot_pr.pr_list = previous_prs.copy()
            bot_pr.pr_list.extend(batch.prs)
            # Reset the test counts.
            bot_pr.passing_test_count = 0
            bot_pr.failing_test_count = 0
            bot_pr.pending_test_count = 0

            bot_pr.head_commit_sha = client.force_push_branch(
                tmp_branch_name, bot_pull.head.ref
            )
            db.session.commit()

            client.delete_ref(tmp_branch_name)
            if latest_sha == bot_pr.head_commit_sha:
                # The new bot_pr should never have the same commit as the target branch.
                slog.info(
                    "Failed to generate commit for BotPR in parallel mode"
                    "(squashing pull request onto branch didn't change the HEAD commit SHA)"
                )
                # Let's raise exception, so we try one more time before giving up.
                raise _InvalidSquashCommitError()
        elif repo.parallel_mode_config.use_fast_forwarding:
            # Create a temporary branch and resync that to the latest commit.
            # Once done, we force push that sha to the bot_pull branch and delete temp branch.
            # This avoids running CI on the bot_pull branch for every commit.
            latest_sha = client.get_branch_head_sha(previous_branch)
            tmp_branch_name = client.copy_to_new_branch(
                latest_sha, str(batch.get_first_pr().number), prefix="mq-tmp-"
            )
            client.force_push_branch(previous_branch, tmp_branch_name)
            _squash_batch_prs_to_branch(client, batch, tmp_branch_name)
            bot_pr.head_commit_sha = client.force_push_branch(
                tmp_branch_name, bot_pull.head.ref
            )
            # Reset the test counts.
            bot_pr.passing_test_count = 0
            bot_pr.failing_test_count = 0
            bot_pr.pending_test_count = 0
            db.session.commit()
            client.delete_ref(tmp_branch_name)
            if latest_sha == bot_pr.head_commit_sha:
                # The new bot_pr should never have the same commit as the previous branch.
                slog.info(
                    "Failed to generate commit for BotPR for FF"
                    "(squashing pull request onto branch didn't change the HEAD commit SHA)"
                )
                # Let's raise exception, so we try one more time before giving up.
                raise _InvalidSquashCommitError()
        else:
            # instead of blindly pushing the branch_to_sync to bot_branch,
            # we will create a temporary branch, merge the branch_to_sync into it,
            # and then push that to the bot_branch.
            # this generates all new commit SHAs avoid any reruns of CI on same commits.
            # it also avoids bot_pr closing automatically.
            logger.info(
                "Using default parallel mode for resync bot PR",
                repo_id=repo.id,
                bot_pr=bot_pr.number,
                pr_numbers=[pr.number for pr in batch.prs],
            )
            latest_sha = client.get_branch_head_sha(previous_branch)
            tmp_branch_name = client.copy_to_new_branch(
                latest_sha, str(batch.get_first_pr().number), prefix="mq-tmp-"
            )
            for pr in batch.prs:
                client.merge_branch_to_branch(
                    batch.get_pull(pr.number).head.ref, tmp_branch_name
                )
            bot_pr.head_commit_sha = client.force_push_branch(
                tmp_branch_name, bot_pull.head.ref
            )
            # Reset the test counts.
            bot_pr.passing_test_count = 0
            bot_pr.failing_test_count = 0
            bot_pr.pending_test_count = 0
            db.session.commit()
            client.delete_ref(tmp_branch_name)
            if latest_sha == bot_pr.head_commit_sha:
                # The new bot_pr should never have the same commit as the previous branch.
                slog.info(
                    "Failed to generate commit for BotPR in parallel mode"
                    "(squashing pull request onto branch didn't change the HEAD commit SHA)"
                )
                # Let's raise exception, so we try one more time before giving up.
                raise _InvalidSquashCommitError()

        bot_pr_description = _bot_pr_body_text(repo, batch, previous_prs)
        bot_pull.edit(body=bot_pr_description)
        return bot_pull.head.ref
    except Exception as exc:
        # TODO: catching exceptions that are thrown locally is an anti-pattern
        #   and generally makes the logic harder to understand here
        is_weird_github_thing = (
            common.is_network_issue(exc)
            or isinstance(exc, pygithub.UnknownObjectException)
            or isinstance(exc, _InvalidSquashCommitError)
        )
        if attempt < 2 and is_weird_github_thing:
            # If Github threw up, give one more try.
            return resync_bot_pr(
                client,
                repo,
                bot_pr,
                previous_branch=previous_branch,
                previous_prs=previous_prs,
                previous_bot_pr=previous_bot_pr,
                attempt=attempt + 1,
            )
        slog.error(
            "Failed to resync BotPR",
            exc_info=exc,
        )
        status_reason = None
        if is_weird_github_thing:
            status_reason = (
                "Something went wrong when communicating with GitHub "
                "and Aviator could not process this pull request. "
                "Please try again later."
            )
        slog.info(
            "Setting status for BotPR and all target PRs",
            status_code=StatusCode.MERGE_CONFLICT,
        )
        if not repo.parallel_mode:
            # The parallel mode has been turned off. There is no good way out of resync.
            raise Exception("Parallel mode is not enabled, giving up on resync")
        for pr in bot_pr.batch_pr_list:
            mark_as_blocked(
                client,
                repo,
                pr,
                batch.get_pull(pr.number),
                StatusCode.MERGE_CONFLICT,
                status_reason=status_reason,
            )
        bot_pr.set_status("closed", StatusCode.MERGE_CONFLICT)
        db.session.commit()
        _removed_from_batch(bot_pr.batch_pr_list)
        if bot_pr.number != batch.get_first_pr().number:
            close_pulls.delay(
                repo_id=repo.id,
                pull_list=[bot_pr.number],
                # do not delete the branch, since we will use this for debug message
                delete_ref=(
                    not isinstance(exc, errors.PRStatusException)
                    or not exc.conflicting_pr
                ),
            )
    finally:
        if tmp_branch_name:
            client.delete_ref(tmp_branch_name)

    return None


def merge_pr(
    client: GithubClient,
    repo: GithubRepo,
    pr: PullRequest,
    pull: pygithub.PullRequest,
    parallel_mode: bool = False,
    bot_pull: pygithub.PullRequest | None = None,
    batch: Batch | None = None,
    *,
    optimistic_botpr: BotPr | None,
) -> bool:
    """
    Merge a PR using the appropriate strategy for the repo.

    Returns a boolean indicating whether or not the PR was merged by this
    function.

    :param optimistic_botpr: The BotPR that caused an optimistic merge (if any).
    """
    if _is_queue_paused(repo, pull.base.ref):
        _comment_for_paused(pr)
        logger.info(
            "Repo %d PR %d skipping merge_pr, queue is paused", repo.id, pr.number
        )
        return False
    else:
        delete_paused(repo.id, pr.number)
    # If we have reached this point, flag the status as complete.
    # Do it inline to avoid the status not updated before merging the PR.
    # Doing a bit of refetching to keep SQLAlchemy happy.
    av_checks.update_pr_check(pr, status="success")
    repo = pr.repo
    try:
        if pull.merged:
            return False
        if not valid_commit_sha(client, repo, pr, pull):
            return False
        if _should_merge_draft_pr(pr):
            if bot_pull:
                client.fast_forward_merge(bot_pull)
                prs_to_update = batch.prs if batch else [pr]
                for p in prs_to_update:
                    p.merge_commit_sha = bot_pull.head.sha
                db.session.commit()
            else:
                # This should not happen. This is a guardrail so that we don't merge a PR in FF mode.
                logger.error(
                    "Repo %d PR %d does not have a valid bot pull", repo.id, pr.number
                )
                return False
        elif pr.stack_parent and repo.merge_strategy.use_separate_commits_for_stack:
            merge_stacked_pr_via_ff(client, pr, pull)
            prs_to_update = [pr]
        else:
            stack_manager.pre_merge_actions(pr, pull)
            regex_configs = RegexConfig.query.filter_by(repo_id=repo.id).all()
            sha = client.merge(
                pull,
                repo.merge_strategy.name.value,
                repo.merge_labels,
                repo.merge_commit.use_title_and_body,
                get_commit_message(pr),
                get_commit_title(pr),
                regex_configs,
                stack_manager.get_stacked_pulls(pr, client),
            )
            pr.merge_commit_sha = sha
            db.session.commit()
            prs_to_update = [pr]
    except Exception as e:
        skip_pr, status_code, ci_map, note = handle_failed_merge(
            client, repo, pr, pull, e, parallel_mode
        )
        if skip_pr:
            logger.info(
                "Repo %d PR %d, skip_pr=%s, status_code=%s, ci_map=%s, note=%s",
                repo.id,
                pr.number,
                skip_pr,
                status_code,
                list(ci_map.keys()),
                note,
            )
            return False
        if status_code == StatusCode.MERGED_BY_MQ:
            # This is to track the scenario where the PR got merged via a
            # workaround, so we should not mark it as blocked here.
            prs_to_update = [pr]
        else:
            prs_to_block = (
                batch.prs
                if repo.parallel_mode_config.use_fast_forwarding and batch
                else [pr]
            )
            for pr in prs_to_block:
                batch_pull = batch.get_pull(pr.number) if batch else pull
                mark_as_blocked(
                    client,
                    repo,
                    pr,
                    batch_pull,
                    status_code,
                    ci_map=ci_map,
                    note=note,
                )
            return False

    update_prs_post_merge(
        client,
        repo,
        pr,
        pull,
        prs_to_update,
        optimistic_botpr=optimistic_botpr,
    )
    return True


def merge_stacked_pr_via_ff(
    client: GithubClient, pr: PullRequest, pull: pygithub.PullRequest
) -> None:
    """
    This method takes the latest master commit SHA in a tmp branch, and
    applies all stack PRs on top of it by squashing one at a time. Once
    all the squash commits are created, it will fast-forward to master / main
    to the new top commit, and delete the tmp branch.

    Effectively, this is the same workflow as the fast-forwarding mode,
    except this step happens during merge time, not botPR construction time.
    """
    target_git_ref = client.get_git_ref(pr.target_branch_name)
    tmp_branch = client.copy_to_new_branch(
        target_git_ref.object.sha, str(pr.number), prefix="mq-tmp-"
    )
    try:
        if pr.status == "merged":
            logger.info(
                "This PR is already merged, skipping stack merge",
                pr_number=pr.number,
                repo_id=pr.repo_id,
            )
            return
        pr_to_merge_sha = _squash_pull_to_branch(client, pr, pull, tmp_branch)
        # For Figma: move the target git ref one at a time.
        for pr_number, head_sha in pr_to_merge_sha:
            target_git_ref.edit(head_sha)  # don't force
            time.sleep(1)  # give a second for GitHub to catch up.
        _set_merge_commit_sha_in_stack(pr.repo_id, pr_to_merge_sha)

        # This should ideally be done in handle_post_merge, but since
        # this is a special case, it's cleaner to leave it here.
        client.add_label(pull, MERGED_BY_MQ_LABEL)
        client.close_pull(pull)
        delete_branch_if_applicable(client, pr.repo, pull)
    except Exception as exc:
        logger.info(
            "Deleted stale branch due to failed stacked PR merge",
            repo_id=pr.repo_id,
            pr=pr.number,
            exc_info=exc,
        )
        raise
    finally:
        client.delete_ref(tmp_branch)


def merge_attempt_via_ff_if_needed(
    client: GithubClient, pr: PullRequest, pull: pygithub.PullRequest, message: str
) -> bool:
    """
    This is a workaround only when we are stuck in a state
    where GH is not letting us merge the PR. We will check if
    we are in parallel mode (non-ff), and that enough time has
    passed waiting for GH to merge the PR.
    """
    if not pr.repo.parallel_mode:
        return False
    logger.info(
        "Checking if we should attempt FF merge",
        repo_id=pr.repo_id,
        pr_number=pr.number,
        message=message,
    )

    if _has_exceeded_pending_mergeability_wait(pr, message):
        merge_stacked_pr_via_ff(client, pr, pull)
        logger.info(
            "Successfully merged via FF mode",
            repo_id=pr.repo_id,
            pr_number=pr.number,
        )
        client.create_issue_comment(
            pr.number,
            "Merged via fast-forwarding because GitHub mergeability timed out."
            f"Additional debug info: {message}",
        )
        return True
    return False


def _set_merge_commit_sha_in_stack(
    repo_id: int, pr_to_merge_sha: list[tuple[int, str]]
) -> None:
    # convert back to dictionary
    pr_to_merge_sha_map = {pr_number: sha for pr_number, sha in pr_to_merge_sha}
    all_prs: list[PullRequest] = db.session.scalars(
        sa.select(PullRequest)
        .where(PullRequest.number.in_(pr_to_merge_sha_map.keys()))
        .where(PullRequest.repo_id == repo_id)
    ).all()
    for pr in all_prs:
        pr.merge_commit_sha = pr_to_merge_sha_map[pr.number]
    db.session.commit()


def update_prs_post_merge(
    client: GithubClient,
    repo: GithubRepo,
    pr: PullRequest,
    pull: pygithub.PullRequest,
    prs_to_update: list[PullRequest],
    *,
    optimistic_botpr: BotPr | None,
) -> PullRequest:
    """
    Update pull requests after merging them on GitHub.

    :param client: The GitHub client.
    :param repo: The GitHub repo.
    :param pr: The PR that was merged.
    :param pull: The GitHub pull request object.
    :param prs_to_update: The list of PRs to update.
    :param optimistic_botpr: The BotPR that caused an optimistic merge (if any).
        An optimistic merge is one where a subsequent BotPR succeeded and caused
        MergeQueue to consider the original PR as successful (even if its BotPR
        wasn't passing).
    :return:
    """
    for pr_to_update in prs_to_update:
        util.posthog_util.capture_pull_request_event(
            util.posthog_util.PostHogEvent.MERGE_PULL_REQUEST,
            pr_to_update,
        )
        pr_to_update.set_status("merged", StatusCode.MERGED_BY_MQ)
        pr_to_update.merged_at = time_util.now()
        if pr_to_update.merge_commit_sha is None:
            logger.error(
                "Missing merge commit SHA for a merged PR",
                pr_number=pr_to_update.number,
            )
        merged_activity = Activity(
            repo_id=repo.id,
            name=ActivityType.MERGED,
            pull_request_id=pr_to_update.id,
            status_code=pr_to_update.status_code,
            payload=ActivityMergedPayload(
                optimistic_merge_botpr_id=(
                    optimistic_botpr.id if optimistic_botpr else None
                ),
                emergency_merge=pr.emergency_merge,
            ),
        )
        db.session.add(merged_activity)
        db.session.commit()
        logger.info(
            "Updated pull request after merging",
            repo_id=repo.id,
            pr_id=pr_to_update.id,
            pr_number=pr_to_update.number,
            pr_status_code=pr_to_update.status_code,
            optimistic_merge=optimistic_botpr is not None,
        )
        send_pr_update_to_channel.delay(pr.id)
        pilot_data = common.get_pilot_data(pr, "merged")
        hooks.call_master_webhook.delay(
            pr_to_update.id, "merged", pilot_data=pilot_data, ci_map={}, note=""
        )

    # Note: We have to do this *before* deleting branches.
    handle_stacked_pr_post_merge_fixup(client, repo, pr, pull)
    delete_branch_if_applicable(client, repo, pull)

    for pr_to_update in prs_to_update:
        update_gh_pr_status.delay(pr_to_update.id)
        flexreview.celery_tasks.process_merged_pull.delay(pr_to_update.id)
    atc.non_released_prs.update_non_released_prs.delay(repo.id)
    return pr


def _has_equal_commit_hash(
    client: GithubClient, repo: GithubRepo, target_branch: str, new_branch: str
) -> bool:
    """
    Check if the new branch is the same as the last bot PR's branch.
    """
    last_bot_pr = _get_latest_bot_pr(repo, target_branch)
    if last_bot_pr:
        old_sha = last_bot_pr.head_commit_sha
    else:
        old_sha = client.get_branch_head_sha(target_branch)
    new_sha = client.get_branch_head_sha(new_branch)
    return new_sha == old_sha


def _is_queue_paused(repo: GithubRepo, base_branch: str) -> bool:
    return not repo.enabled or repo.is_base_branch_paused(base_branch)


@celery.task
def comment_for_paused_async(pr_id: int) -> None:
    pr = PullRequest.get_by_id_x(pr_id)
    _comment_for_paused(pr)


def _comment_for_paused(pr: PullRequest) -> None:
    repo = pr.repo
    if should_post_paused(repo.id, pr.number, "paused"):
        message = "The queue is currently paused."

        # Check for custom paused message
        base_branches: list[BaseBranch] = BaseBranch.query.filter_by(
            repo_id=repo.id, paused=True
        ).all()
        matched_base_branches = [
            base_branch
            for base_branch in base_branches
            if fnmatch.fnmatch(pr.base_branch_name, base_branch.name)
        ]
        base_branch = matched_base_branches[0] if matched_base_branches else None
        if base_branch and base_branch.paused_message:
            message += "\n\n" + base_branch.paused_message

        _, client = common.get_client(repo)
        client.create_issue_comment(pr.number, message)
        logger.info("Repo %d is paused, top PR is %d", repo.id, pr.number)


def delete_branch_if_applicable(
    client: GithubClient, repo: GithubRepo, pull: pygithub.PullRequest
) -> None:
    if repo.delete_branch:
        skip_delete_labels = [label.name for label in repo.skip_delete_labels]
        prs_count = (
            PullRequest.query.filter_by(
                repo_id=repo.id,
                base_branch_name=pull.head.ref,
            )
            .filter(PullRequest.status.in_(["open", "queued", "tagged", "blocked"]))
            .count()
        )
        if prs_count == 0:
            # We should only delete the branch (pull.head.ref) if there are
            #   no other open PRs that are based off pull.head.ref.
            client.delete_branch(pull, skip_delete_labels)


def handle_stacked_pr_post_merge_fixup(
    client: GithubClient,
    repo: GithubRepo,
    pr: PullRequest,
    pull: pygithub.PullRequest,
) -> None:
    """
    Fix-up stack dependents before merging.

    This changes the base branch of the dependents to be the base branch of the
    parent PR.
    """
    mark_ancestor_prs_merged_by_bot(client, pr)
    stack_manager.post_merge_actions(client, repo, pr)

    for dep in pr.stack_dependents:
        fetch_pr.delay(dep.number, repo.name)


def handle_failed_merge(
    client: GithubClient,
    repo: GithubRepo,
    pr: PullRequest,
    pull: pygithub.PullRequest,
    exc: Exception,
    parallel_mode: bool = False,
) -> tuple[bool, StatusCode, dict[str, str], str]:
    skip_pr = True
    status_code = StatusCode.FAILED_UNKNOWN
    ci_map: dict[str, str] = {}
    note = ""

    slog = logger.bind(
        repo_id=repo.id,
        pr_id=pr.id,
        pr_number=pr.number,
        pr_head_sha=pull.head.sha,
        pr_mergeable_state=pull.mergeable_state,
        exc_info=exc,
    )
    slog.info("Failed to merge, handling failure")
    if parallel_mode and pr.latest_bot_pr:
        slog = slog.bind(
            botpr_id=pr.latest_bot_pr.id,
            botpr_number=pr.latest_bot_pr.number,
        )

    if common.is_network_issue(exc):
        slog.info("Failed to read from GitHub")
        if repo.is_no_queue_mode:
            review_pr_async.delay(pr.id)
        return skip_pr, status_code, ci_map, note

    if (
        isinstance(exc, pygithub.GithubException)
        and exc.status == 405
        and isinstance(exc.data, dict)
        and exc.data["message"]
        == "Base branch was modified. Review and try the merge again."
    ):
        # This means that a different PR was just merged and we need to wait for some Github processes to finish.
        # https://github.community/t/merging-via-rest-api-returns-405-base-branch-was-modified-review-and-try-the-merge-again/13787
        slog.info("Failed to merge: base branch was modified")
        if merge_attempt_via_ff_if_needed(client, pr, pull, str(exc.data["message"])):
            return False, StatusCode.MERGED_BY_MQ, ci_map, note
        raise errors.GithubMergeabilityPendingException(
            "Base branch modified for repo %d PR %d" % (repo.id, pull.number)
        )
    if (
        isinstance(exc, pygithub.GithubException)
        and exc.status == 409
        and isinstance(exc.data, dict)
        and exc.data["message"]
        == "Head branch is out of date. Review and try the merge again."
    ):
        # This is likely a transient issue, we have only seen once.
        # We couldn't find much of a reference for this isue.
        slog.info("Failed to merge: head branch is out of date")
        if merge_attempt_via_ff_if_needed(client, pr, pull, str(exc.data["message"])):
            return False, StatusCode.MERGED_BY_MQ, ci_map, note
        return skip_pr, status_code, ci_map, note

    if (
        isinstance(exc, pygithub.GithubException)
        and exc.status == 405
        and isinstance(exc.data, dict)
        and exc.data["message"] == "Pull Request is not mergeable"
    ):
        # There's a chance that PR was already merged. Check if that's true.
        pull = client.get_pull(pull.number)
        if pull.merged and common.is_bot_user(repo.account_id, pull.merged_by.login):
            slog.info("Failed to merge: pull request is already merged")
            update_prs_post_merge(
                client,
                repo,
                pr,
                pull,
                prs_to_update=[pr],
                optimistic_botpr=None,
            )

        # If a PR is not mergeable, we should just retry it normally and not flag
        # as merge conflict.
        if merge_attempt_via_ff_if_needed(client, pr, pull, str(exc.data["message"])):
            return False, StatusCode.MERGED_BY_MQ, ci_map, note
        raise errors.GithubMergeabilityPendingException(
            "Unknown Github mergeability for repo %d PR %d" % (repo.id, pull.number)
        )
    if (
        isinstance(exc, pygithub.GithubException)
        and exc.status == 405
        and isinstance(exc.data, dict)
        and exc.data["message"] == "Merge already in progress"
    ):
        if merge_attempt_via_ff_if_needed(client, pr, pull, str(exc.data["message"])):
            return False, StatusCode.MERGED_BY_MQ, ci_map, note
        # Looks like there's another merge process happening (e.g. emergency merge).
        # Wait and retry this.
        raise errors.GithubMergeabilityPendingException(
            f"GitHub says the merge is already in progress for repo {repo.id} PR {pull.number}"
        )

    if (
        isinstance(exc, pygithub.GithubException)
        and exc.status == 422
        and isinstance(exc.data, dict)
        and exc.data["message"] == "Update is not a fast forward"
    ):
        # Reset the queue - a new commit has been added to master so the PR could not be fast forwarded.
        client.create_issue_comment(
            pr.number,
            "The base branch has new commits so this PR could not be fast forwarded. Triggering a full queue reset.",
        )
        slog.warning("Failed to merge: invalid fast-forwardable state")
        stack_manager.failed_merge_actions(pr, pull)
        full_queue_reset(
            repo,
            StatusCode.RESET_BY_FF_FAILURE,
            pr.target_branch_name,
        )
        return skip_pr, status_code, ci_map, note

    if (
        isinstance(exc, pygithub.GithubException)
        and repo.parallel_mode_config.use_fast_forwarding
        and exc.status == 422
        and isinstance(exc.data, dict)
        and exc.data["message"]
        == "At least 1 approving review is required by reviewers with write access."
    ):
        # user does not have admin access on fast-forward causing error when we try to ff the branch
        client.create_issue_comment(
            pr.number,
            "The Aviator app does not have enough permissions to fast-forward this branch. Review the setup "
            "instructions: https://docs.aviator.co/how-to-guides/fast-forwarding#setup.",
        )
        slog.warning("Failed to merge: insufficient permissions to fast-forward")
        skip_pr = False
        status_code = StatusCode.RESET_BY_FF_FAILURE
        return skip_pr, status_code, ci_map, note

    slog.info("Failed to merge")

    # always check if the commit sha is valid
    # note: Github's mergeable and mergeable_state may not be accurate
    if not valid_commit_sha(client, repo, pr, pull):
        return skip_pr, status_code, ci_map, note

    if pull.mergeable is False and not parallel_mode:
        # this will be for non-queue modes. For parallel mode, we validates statuses first.
        status_code = (
            StatusCode.REBASE_FAILED if repo.use_rebase else StatusCode.MERGE_CONFLICT
        )
        slog.info("Failed to merge: merge conflict detected")
    elif _is_merge_blocked_by_strict_status_checks(exc, pull, parallel_mode):
        status_code = StatusCode.BLOCKED_BY_GITHUB
        note = (
            "Is the `Required branches to be up to date before merging` "
            "setting enabled in your GitHub branch protection rules? This "
            "setting is incompatible with MergeQueue's parallel mode and must be"
            "disabled."
        )
    elif _is_merge_blocked_by_not_authorized(exc, pull):
        status_code = StatusCode.BLOCKED_BY_GITHUB
        note = (
            "Is the `Restrict who can push to matching branches` "
            "setting enabled in your GitHub branch protection rules? You may have to add "
            "`mergequeue` app in the list of allowed users."
        )
    elif pull.mergeable_state != "clean":
        # only ignore when something is in progress.
        if isinstance(exc, pygithub.GithubException):
            if isinstance(exc.data, dict):
                note = str(exc.data.get("message"))
            error_message = note or str(exc)
            if parallel_mode:
                details = checks.fetch_latest_test_statuses(
                    client,
                    repo,
                    pull,
                    botpr=False,
                )
                status = details.result
                ci_map = details.check_urls
                update_gh_pr_status.delay(pr.id)
                if status == "failure":
                    status_code = StatusCode.FAILED_TESTS
                elif status == "pending" or pending_error_message(error_message):
                    # if bot-pr is recently created, the CI may have been re-triggered,
                    # let's try again in a few seconds before declaring stuck.
                    if pr.latest_bot_pr and _bot_pr_recently_created(pr.latest_bot_pr):
                        slog.info(
                            "Failed to merge: BotPR is recently created (we'll retry in a few seconds)",
                        )
                        return skip_pr, status_code, ci_map, note
                    if not ci_map:
                        slog.info(
                            "Failed to merge: PR has no pending CI, so this is pending due to "
                            "mergeability, we will retry again later."
                        )
                        return skip_pr, status_code, ci_map, note
                    if parallel_mode_check_pr_stuck(client, repo, pr, pull, ci_map):
                        status_code = StatusCode.STUCK_TIMEDOUT
                    else:
                        return skip_pr, status_code, ci_map, note
                elif pull.mergeable is False:
                    status_code = (
                        StatusCode.REBASE_FAILED
                        if repo.use_rebase
                        else StatusCode.MERGE_CONFLICT
                    )
                    slog.info("Failed to merge: merge conflict detected")
            if "Waiting on code owner review" in error_message:
                status_code = StatusCode.MISSING_OWNER_REVIEW
    if status_code == StatusCode.FAILED_UNKNOWN and isinstance(
        exc, pygithub.GithubException
    ):
        if isinstance(exc.data, dict):
            note = str(exc.data.get("message"))
        error_message = note or str(exc)
        if pending_error_message(error_message):
            # Let's circle back again.
            slog.error("Failed to merge: inconsistent status check configuration setup")
            # MER-654: If we found inconsistent status checks, it's possible that a new GitHub check
            # has been added. Analyze the repo to fetch the latest required status checks.
            extract.analyze_repo_async.delay(repo.id)
            return skip_pr, status_code, ci_map, note
        else:
            slog.warning("Failed to merge: unknown error received from Github.")
            status_code = StatusCode.BLOCKED_BY_GITHUB
    skip_pr = False
    return skip_pr, status_code, ci_map, note


def _is_merge_blocked_by_strict_status_checks(
    error: Exception,
    pull: pygithub.PullRequest,
    parallel_mode: bool,
) -> bool:
    """
    Determine whether this PR is blocked by strict status checks in GH.

    In parallel mode, we create draft PRs that include all the
    previous commits that are queued for merge (so that we ensure that the
    current PR works with all the previous PRs). However, we merge the
    original PRs (and close the draft BotPr).
    This breaks if the "require branches to be up to date" setting is
    enabled as a branch protection rule (`requiresStrictStatusChecks` in
    the GitHub API).
    From our perspective (given just the GH API PR object), this PR shows
    as behind (i.e., lacking most recent commits from base branch) and
    mergeable, but the merge fails anyway.

    :param error: The exception raised by the GitHub API.
    :param pull: The GitHub API PullRequest object.
    :param parallel_mode: True if parallel mode is enabled.
    :return: True if the PR is blocked by strict status checks.
    """
    # Heuristic based on the actual error payload that GitHub returns.
    return bool(
        isinstance(error, pygithub.GithubException)
        and error.status == 405
        and parallel_mode
        and pull.mergeable
        and pull.mergeable_state == "behind"
    )


def _is_merge_blocked_by_not_authorized(
    error: Exception,
    pull: pygithub.PullRequest,
) -> bool:
    """
    Determine whether this PR is blocked because we are not authorized
    to merge it.

    :param error: The exception raised by the GitHub API.
    :param pull: The GitHub API PullRequest object.
    :return: True if the PR is blocked by not authorized.
    """
    # Heuristic based on the actual error payload that GitHub returns.
    if error:
        message = getattr(error, "data", {}).get("message", "")
    else:
        message = ""
    return bool(
        isinstance(error, pygithub.GithubException)
        and error.status == 405
        and message.startswith("You're not authorized to push to this branch")
    )


def valid_commit_sha(
    client: GithubClient,
    repo: GithubRepo,
    pr: PullRequest,
    pull: pygithub.PullRequest,
) -> bool:
    new_pull = client.get_pull(pull.number)
    if pull.head.sha != new_pull.head.sha:
        logger.info(
            "Repo %d PR %d confirmed stale status check old sha %s new sha %s"
            + ", mergeable state %s",
            repo.id,
            pr.number,
            pull.head.sha,
            new_pull.head.sha,
            pull.mergeable_state,
        )
        return False
    return True


def _bot_pr_recently_created(bot_pr: BotPr) -> bool:
    """
    Determine if the bot PR was recently created.

    :param bot_pr: The BotPr object.
    :return: True if the bot PR was recently created.
    """
    return (
        bot_pr.created > time_util.now() - datetime.timedelta(seconds=10)
        and bot_pr.number != bot_pr.pull_request.number
    )


def parallel_mode_check_pr_stuck(
    client: GithubClient,
    repo: GithubRepo,
    pr: PullRequest,
    pull: pygithub.PullRequest,
    ci_map: dict[str, str],
) -> bool:
    """
    Check if a pull request is "stuck."

    A pull request is considered "stuck" if:
      - The BotPR is passing CI
      - The original PR is not passing all required CI (probably has a pending test)
      - And the stuck timeout has not been exceeded
    """
    stuck_activity: Activity | None = (
        Activity.query.filter(
            Activity.repo_id == pr.repo_id,
            Activity.name == ActivityType.STUCK,
            Activity.pull_request_id == pr.id,
        )
        .filter(Activity.created > pr.queued_at)
        .first()
    )

    if not stuck_activity:
        logger.info(
            "Repo %d PR %d CI taking longer than expected, adding stuck label/comment",
            repo.id,
            pr.number,
        )
        if repo.parallel_mode_stuck_label and not common.has_label(
            pull, repo.parallel_mode_stuck_label.name
        ):
            try:
                client.add_label(pull, repo.parallel_mode_stuck_label.name)
            except Exception as e:
                logger.error(
                    "Failed to add label / comment",
                    repo_id=repo.id,
                    pr_number=pr.number,
                    exc_info=e,
                )

        pr.status_code = StatusCode.STUCK_ON_STATUS
        db.session.commit()
        comments.post_pull_comment.delay(pr.id, "stuck", ci_map=ci_map)
        pilot_data = common.get_pilot_data(pr, "stuck")
        hooks.call_master_webhook.delay(
            pr.id,
            "stuck",
            pilot_data=pilot_data,
            ci_map=ci_map,
            note="",
            status_code_int=pr.status_code.value,
        )
    elif check_stuck_timeout(repo, pr):
        # This means that the stuck minutes >= stuck_pr_timeout_mins, and therefore the CI is stuck beyond timeout.
        return True
    return False


def pending_error_message(error_message: str) -> bool:
    return (
        "required status check" in error_message.lower()
        and "failing" not in error_message
    )


# This should really not fail at all. Adding retry with exponential backoff of up to 20 mins.
@celery.task(
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=1200,
    retry_kwargs={"max_retries": 20},
    retry_jitter=True,
)
def close_pulls(
    repo_id: int,
    pull_list: list[int],
    *,
    delete_ref: bool = True,
    close_reason: str | None = None,
) -> None:
    """
    Close the given pull requests.

    This is especially useful to close draft PRs that are created by MergeQueue
    (e.g., when using parallel mode).
    """
    logger.info("Closing PRs %s", pull_list)
    repo = GithubRepo.get_by_id_x(repo_id)
    access_token, client = common.get_client(repo)
    for pull_number in pull_list:
        bot_pull = client.get_pull(pull_number)
        if bot_pull.state == "closed":
            # skip already closed pulls
            continue

        if close_reason is not None:
            comments.add_bot_pr_close_comment(
                repo_id=repo_id,
                bot_pr_number=pull_number,
                comment=close_reason,
            )

        client.close_pull(bot_pull)
        # we only sometimes want to delete the ref
        if delete_ref:
            client.delete_ref(bot_pull.head.ref)
    logger.info("PRs closed %s", pull_list)


def full_queue_reset(
    repo: GithubRepo,
    status_code: StatusCode,
    target_branch: str = "",
    bot_pr: BotPr | None = None,
) -> None:
    """
    For parallel mode, resets the given repo by closing all BotPRs.

    :param repo:
    :param status_code: The status code to associate with the reset Activity.
    :param target_branch: The target branch to reset. If not specified, all
        branches will be reset.
    :param bot_pr: bot PR that caused the reset to happen, can be null.
    """
    logger.info(
        "Resetting Parallel mode, full reset for repo %d %s due to %s",
        repo.id,
        repo.name,
        status_code,
    )
    bot_pr_query: BaseModelQuery = BotPr.query.filter_by(
        repo_id=repo.id, status="queued"
    )
    pr_query: BaseModelQuery = PullRequest.query.filter_by(repo_id=repo.id).filter(
        PullRequest.status.in_(["tagged", "queued"])
    )
    if target_branch:
        bot_pr_query = (
            bot_pr_query.join(PullRequest, BotPr.pull_request_id == PullRequest.id)
            .filter(PullRequest.target_branch_name == target_branch)
            .filter_by(target_branch_name=target_branch)
        )
        pr_query = pr_query.filter_by(target_branch_name=target_branch)
    bot_prs: list[BotPr] = bot_pr_query.all()
    all_prs: list[PullRequest] = pr_query.all()
    tagged_prs = [pr for pr in all_prs if pr.status == "tagged"]
    queued_prs = [pr for pr in all_prs if pr.status == "queued"]
    target_manager.refresh_dependency_graph_on_reset(repo, all_prs)
    for pr in tagged_prs:
        pr.set_status("queued", StatusCode.QUEUED)
        if pr.merge_operations and pr.merge_operations.is_bisected:
            pr.merge_operations.is_bisected = False
            pr.merge_operations.times_bisected = 0
            pr.merge_operations.bisected_batch_id = None
        activity.create_activity(
            repo_id=repo.id,
            pr_id=pr.id,
            activity_type=ActivityType.RESYNC,
            status_code=status_code,
            payload=ActivityResyncPayload(),
        )
    for pr in queued_prs:
        if pr.merge_operations and pr.merge_operations.is_bisected:
            pr.merge_operations.is_bisected = False
            pr.merge_operations.times_bisected = 0
            pr.merge_operations.bisected_batch_id = None
    pull_list_to_close = [
        bp.number for bp in bot_prs if bp.number != bp.pull_request.number
    ]
    for bp in bot_prs:
        bp.set_status("closed", status_code)
    _record_reset_event(
        repo.id,
        status_code,
        None,
        reset_count=len(tagged_prs),
        closed_bot_pr_count=len(bot_prs),
        bot_pr=bot_pr,
    )
    _removed_from_batch(tagged_prs)
    logger.info(
        "Resetting parallel mode, full reset for repo",
        repo_id=repo.id,
        bp_to_close=pull_list_to_close,
        queued_prs=[pr.number for pr in queued_prs],
        tagged_prs=[pr.number for pr in tagged_prs],
    )
    if pull_list_to_close:
        close_reason = StatusCode.get_reset_reason(status_code).message()
        close_pulls.delay(repo.id, pull_list_to_close, close_reason=close_reason)


def partial_queue_reset_by_skip_line_pr(
    repo: GithubRepo,
    skip_line_pr: PullRequest,
) -> None:
    """
    For parallel mode, resets the given PR by closing all BotPRs in the same queue,
    except for PRs with skip_line = True.

    :param repo:
    :param skip_line_pr:
    """
    logger.info(
        "Partial reset caused by a skip-line PR",
        skip_line_pr_number=skip_line_pr.number,
    )
    bot_prs: list[BotPr]
    if repo.parallel_mode_config.use_affected_targets:
        bot_prs = target_manager.get_all_related_bot_prs(repo, skip_line_pr)
        target_manager.refresh_dependency_graph_on_reset(repo, [skip_line_pr])
    else:
        bot_prs = _get_queued_bot_prs(
            repo,
            skip_line_pr.target_branch_name,
        )

    requeued_pr_numbers: list[int] = []
    bot_prs_to_close: list[int] = []
    non_skipped = False
    for bp in bot_prs:
        # ignore the skip_line top of queue PRs, include skip_line PRs that
        # occur after the first seen non skip_line.
        if not bp.pull_request.skip_line:
            non_skipped = True
        if non_skipped:
            for pr in bp.batch_pr_list:
                pr.set_status("queued", StatusCode.QUEUED)
                requeued_pr_numbers.append(pr.number)
            if bp.pull_request.number != bp.number:
                bot_prs_to_close.append(bp.number)
            bp.set_status("closed", StatusCode.RESET_BY_SKIP)
            _removed_from_batch(bp.batch_pr_list)

    _record_reset_event(
        repo.id,
        StatusCode.RESET_BY_SKIP,
        skip_line_pr,
        reset_count=len(requeued_pr_numbers),
        closed_bot_pr_count=len(bot_prs_to_close),
    )
    logger.info(
        "Requeue caused by skip-line PR complete",
        skip_line_pr_number=skip_line_pr.number,
        requeued_pr_numbers=requeued_pr_numbers,
        closed_bot_pr_numbers=bot_prs_to_close,
    )
    if bot_prs_to_close:
        close_pulls.delay(
            repo.id, bot_prs_to_close, close_reason=StatusCode.RESET_BY_SKIP.message()
        )


def _record_reset_event(
    repo_id: int,
    status_code: StatusCode,
    pr: PullRequest | None,
    *,
    reset_count: int,
    closed_bot_pr_count: int,
    ci_map: dict[str, str] = {},
    bot_pr: BotPr | None = None,
) -> None:
    """
    Record the queue reset activity and send a master webhook for it.

    :param pr: The pull request that caused the reset if one can be identified.
        Can be None if there are many (e.g. a batch include multiple PRs).
    :param reset_count: Number of pull-requests affected by this reset.
    :param closed_bot_pr_count: Number of closed BotPRs.
    :param ci_map: CI status map if this is triggered by a BotPR test failure.
    :param bot_pr: Failed BotPR if this is triggered by a BotPR test failure.
    """
    reset_activity = Activity(
        repo_id=repo_id,
        name=ActivityType.RESET,
        status_code=status_code,
        reset_pr_count=reset_count,
        pull_request_id=pr.id if pr else None,
        bot_pr_id=bot_pr.id if bot_pr else None,
    )
    db.session.add(reset_activity)
    db.session.commit()
    if pr:
        pilot_data = common.get_pilot_data(pr, "reset")
        hooks.call_master_webhook.delay(
            pr.id,
            "reset",
            pilot_data=pilot_data,
            ci_map=ci_map,
            note="",
            reset_count=reset_count,
            closed_bot_pr_count=closed_bot_pr_count,
            bot_pr_id=bot_pr.id if bot_pr else None,
            status_code_int=status_code.value,
        )
    else:
        hooks.call_webhook_reset_without_pr.delay(
            repo_id,
            status_code.value,
            reset_count,
            closed_bot_pr_count=closed_bot_pr_count,
            ci_map=ci_map,
            bot_pr_id=bot_pr.id if bot_pr else None,
        )


class _BotPRMetadata(schema.BaseModel):
    """
    Metadata stored as JSON in the body text of a BotPR.

    Some customers rely on this so updated should be backwards compatible.
    """

    class PullRequest(schema.BaseModel):
        number: int
        head_commit: str

    pull_requests: list[PullRequest]
    target_branch: str


def _bot_pr_body_text(
    repo: GithubRepo,
    batch: Batch,
    previous_prs: list[PullRequest],
) -> str:
    tagged_prs = []
    for pr in batch.prs:
        if stack_manager.is_in_stack(pr):
            for ancestor in stack_manager.get_current_stack_ancestors(pr):
                tagged_prs.append(f"#{ancestor.number}")
        else:
            tagged_prs.append(f"#{pr.number}")
    base_text = f"This PR contains changes from PR(s): {' '.join(tagged_prs)}"
    if repo.parallel_mode_config.use_fast_forwarding:
        base_text += (
            f"\n`{batch.target_branch_name}` will be fast-forwarded to the HEAD of this PR when CI passes, "
            f" and the original PR(s) will be closed.\n\n"
        )
    elif repo.parallel_mode_config.use_parallel_bisection and batch.is_bisected_batch():
        base_text += (
            "\nThis is a bisected batch. These PR(s) will be queued back "
            "into the regular queue when CI passes.\n\n"
        )
    else:
        base_text += f"\nThese PR(s) will be merged into `{batch.target_branch_name}` when CI passes.\n\n"

    # We don't use regular auto-links here ("#123") since that creates backlinks on GitHub which we don't want
    prev_pr_info = [f"- [**{p.title}** #{p.number}]({p.number})" for p in previous_prs]
    if prev_pr_info:
        base_text += (
            "\n\nThis PR also includes changes from the following PRs:\n"
            + "\n".join(prev_pr_info)
        )

    metadata = _BotPRMetadata(
        pull_requests=[
            _BotPRMetadata.PullRequest(
                number=pr.number,
                head_commit=pr.head_commit_sha,
            )
            for pr in batch.prs
        ],
        target_branch=batch.target_branch_name,
    )
    base_text += "\n\n# Aviator metadata\n```json\n"
    base_text += metadata.model_dump_json(indent=2)
    base_text += "\n```\n"
    return base_text


def _merge_once(
    repo: GithubRepo,
    client: GithubClient,
    pr: PullRequest,
    pull: pygithub.PullRequest,
) -> None:
    try:
        sync.merge_or_rebase_head(repo, client, pull, pull.base.ref)
    except errors.PRStatusException as e:
        logger.info(
            "Repository has a merge conflict",
            repo_id=repo.id,
            pr_number=pr.number,
            exc_info=e,
        )
        mark_as_blocked(client, repo, pr, pull, e.code, note=e.message)


def _squash_pull_to_branch(
    client: GithubClient,
    pr: PullRequest,
    pull: pygithub.PullRequest,
    new_branch: str,
) -> list[tuple[int, str]]:
    try:
        pulls_to_squash: list[pygithub.PullRequest]
        if pr.stack_parent:
            # For stacked PRs, we want a single commit per PR in the stack
            ancestors = [
                client.get_pull(ancestor_pr.number)
                for ancestor_pr in stack_manager.get_current_stack_ancestors(
                    pr, skip_merged=True
                )
            ]
            ancestors.reverse()
            logger.info(
                "PR %d is a stack PR, squashing %s on branch %s",
                pr.number,
                ancestors,
                new_branch,
            )
            # ancestors already includes the target PR itself
            pulls_to_squash = ancestors
        else:
            pulls_to_squash = [pull]

        logger.info(
            "Creating a squash commit for repo",
            repo_id=pr.repo_id,
            pr_numbers=[p.number for p in pulls_to_squash],
            branch=new_branch,
        )
        pr_to_sha_map = client.create_squash_commit_per_pull(
            [
                SquashPullInput(
                    pull,
                    # Add "Closes ..." to the commit message. This means
                    # GitHub will show an entry in the PR timeline that says
                    # "aviator[bot] closed this in [abcd1234](link-to-commit)"
                    # which is useful to see which commit the PR was merged
                    # into mainline with, and also for the av CLI to track
                    # how PRs are merged in FF mode.
                    commit_message_trailer=f"Closes #{pull.number}",
                )
                for pull in pulls_to_squash
            ],
            new_branch,
        )
        # return in order of squash
        result = []
        for pull in pulls_to_squash:
            result.append((pull.number, pr_to_sha_map[pull.number]))
        return result
    except Exception as exc:
        logger.info(
            "Failed to cleanly create a squash commit",
            repo_id=pr.repo_id,
            pr_number=pull.number,
            exc_info=exc,
        )
        if common.is_network_issue(exc) or common.is_github_gone_crazy(exc):
            raise exc
        elif common.is_merge_conflict(exc):
            raise errors.PRStatusException(
                StatusCode.MERGE_CONFLICT,
                "Failed to cleanly add this PR on top of existing queue due to a merge conflict. "
                "Please wait for the queue to clear out and then rebase this PR "
                "manually and resolve conflicts).",
                # In case of stacked PR, this would always return the top PR in the stack,
                # which may not be the PR that caused the merge conflict.
                conflicting_pr=pr.number,
            )
        raise exc


def _get_ready_merge_operation(pr: PullRequest) -> MergeOperation | None:
    """
    Get the active MergeOperation associated with the pull request (if any).
    """
    merge_operation: MergeOperation | None = (
        MergeOperation.query.filter(
            MergeOperation.pull_request_id == pr.id,
            MergeOperation.ready,
        )
        .order_by(MergeOperation.id.desc())
        .first()
    )
    return merge_operation


def _pull_is_labeled_ready_to_merge(
    repo: GithubRepo,
    pr: PullRequest,
    pull: pygithub.PullRequest,
) -> bool:
    return (
        # We don't allow the ready label to be applied to stacked PRs yet
        pr.stack_parent_id is None
        and any(label.name == repo.queue_label for label in pull.labels)
    )


def _ensure_pr_merge_operation_if_ready(
    repo: GithubRepo,
    pr: PullRequest,
    pull: pygithub.PullRequest,
) -> MergeOperation | None:
    """
    Ensure that a MergeOperation exists if the pull request is marked as
    ready-to-merge.

    If the pull request is labeled with the repository's ready-to-merge label,
    a MergeOperation will be created if one does not already exist. Otherwise,
    we return any existing MergeOperation (e.g., if the pull request was queued
    via the MergeQueue API).

    This is an awkward function violates the "do one thing" principle, but it
    exists for now to avoid making larger code changes before we're ready to
    fully commit to restructuring things around MergeOperations.
    """
    if _pull_is_labeled_ready_to_merge(repo, pr, pull):
        # Here we cannot tell who was the GH user that labeled it, so leave it blank.
        return common.ensure_merge_operation(
            pr, ready=True, ready_source=ReadySource.LABEL
        )
    merge_operation = _get_ready_merge_operation(pr)
    if (
        merge_operation
        and merge_operation.ready_source == ReadySource.LABEL
        and (time_util.now() - merge_operation.modified).total_seconds() > 5
    ):
        # PR was marked as ready via Label but does not have the label anymore.
        # Give a 5 seconds gap to avoid any race conditions when processing webhooks.
        # This condition is only meant as a fallback to handle dequeue events
        # when we missed webhooks
        logger.warning(
            "PR marked as ready with no label", pr_number=pr.number, repo_id=pr.repo_id
        )
        common.ensure_merge_operation(pr, ready=False, ready_source=ReadySource.UNKNOWN)
        return None

    return merge_operation


def check_pr_ci_within_timeout(
    client: GithubClient,
    repo: GithubRepo,
    pull: pygithub.PullRequest,
    associated_pr: PullRequest,
) -> bool:
    """
    Determine whether or not the pull request has exceeded the CI timeout
    configured for the repository.

    Returns True if the pull request is still within the timeout, False otherwise.

    To calculate when the CI was started we use the time of the last commit. In case the commit
    was fast forwarded to a new PR (bot PR) we use the time of PR creation instead.
    This time is used to calculate whether we had hit a timeout.
    :return: bool - True if the CI has not hit the timeout yet
    """
    now = time_util.now()
    last_commit_dt = client.get_last_commit_date(pull)
    pull_created_at = pull.created_at
    if not pull_created_at.tzinfo:
        pull_created_at = pull_created_at.replace(tzinfo=datetime.UTC)
    labeled_at = _get_queued_or_labeled_at(associated_pr)
    if not labeled_at.tzinfo:
        labeled_at = labeled_at.replace(tzinfo=datetime.UTC)
    max_time = max(pull_created_at, labeled_at, last_commit_dt)
    ci_wait_time_mins = int((now - max_time).total_seconds() / 60)
    if repo.ci_timeout_mins and ci_wait_time_mins > repo.ci_timeout_mins:
        logger.info(
            "Repo %d PR %d test still running for %d mins. CI timeout exceeded",
            repo.id,
            pull.number,
            ci_wait_time_mins,
        )
        return False

    logger.info(
        "Repo %d PR %d test still running for %d mins",
        repo.id,
        pull.number,
        ci_wait_time_mins,
    )
    return True


def _get_queued_or_labeled_at(pr: PullRequest) -> datetime.datetime:
    labeled_activity: Activity | None = (
        Activity.query.filter(
            Activity.repo_id == pr.repo_id,
            Activity.name == ActivityType.LABELED,
            Activity.pull_request_id == pr.id,
        )
        .order_by(Activity.id.desc())
        .first()
    )
    queued_at_activity: Activity | None = (
        Activity.query.filter(
            Activity.repo_id == pr.repo_id,
            Activity.name == ActivityType.QUEUED,
            Activity.pull_request_id == pr.id,
        )
        .order_by(Activity.id.desc())
        .first()
    )
    if labeled_activity and queued_at_activity:
        return max(labeled_activity.created, queued_at_activity.created)
    if labeled_activity:
        return labeled_activity.created
    if queued_at_activity:
        return queued_at_activity.created
    logger.info(
        "No queued or labeled event detected", pr_number=pr.number, repo_id=pr.repo_id
    )
    return datetime.datetime.now(datetime.UTC)


def check_stuck_timeout(repo: GithubRepo, pr: PullRequest) -> bool:
    """
    Determine whether or not the pull request has been stuck for longer than the
    configured timeout.
    """
    # Find most recent stuck event.
    event: Activity | None = (
        Activity.query.filter(
            Activity.repo_id == pr.repo_id,
            Activity.name == ActivityType.STUCK,
            Activity.pull_request_id == pr.id,
        )
        .order_by(Activity.id.desc())
        .first()
    )
    if event:
        stuck_mins = int((time_util.now() - event.created).total_seconds() / 60)
        # if there is no stuck_pr_timeout_mins set, or we have been stuck longer, report timeout.
        return (
            not repo.parallel_mode_config.stuck_pr_timeout_mins
            or stuck_mins >= repo.parallel_mode_config.stuck_pr_timeout_mins
        )
    return False


def did_mark_ready(pr: PullRequest) -> bool:
    """
    Returns true if the PR was just flagged as ready (aka labeled). There are
    several ways the PR could be flagged as ready (slash command, graphql, GH label).
    In this method, we are checking a Redis cache to see if the PR was marked ready for
    first time. We also use this method to reset the Redis cache if the PR is no longer
    in ready state. This way a subsequent request to mark the PR as ready will be
    considered as a first time ready.
    """
    value = "1" if common.has_merge_operation(pr) else "0"
    key = "ready-%d" % pr.id
    changed = set_if_changed(key, value)
    if changed and value == "1":
        return True
    return False


def should_post(repo_id: int, pr_number: int, value: str) -> bool:
    key = "comment-%d-%d" % (repo_id, pr_number)
    return set_if_changed(key, value)


def top_changed(repo_id: int, pr_number: int) -> bool:
    key = "repo-top-%d" % (repo_id)
    return set_if_changed(key, str(pr_number))


def should_post_paused(repo_id: int, pr_number: int, value: str) -> bool:
    key = "paused-%d-%d" % (repo_id, pr_number)
    return set_if_changed(key, value)


def delete_paused(repo_id: int, pr_number: int) -> None:
    key = "paused-%d-%d" % (repo_id, pr_number)
    redis_client.delete(key)


def _set_merge_conflict_with_no_conflicting_pr(pr_id: int) -> None:
    key = "merge-conflict-%d" % (pr_id)
    redis_client.set(key, "true", ex=time_util.THREE_HOURS_SECONDS)


def _has_merge_conflict_with_no_conflicting_pr(pr_id: int) -> bool:
    key = "merge-conflict-%d" % (pr_id)
    return bool(redis_client.get(key))


def is_conv_resolved(gql: graphql.GithubGql, repo: GithubRepo, pr_number: int) -> bool:
    if repo.preconditions.conversation_resolution_required:
        return gql.are_comments_resolved(repo.name, pr_number)
    return True


def set_if_changed(key: str, value: str) -> bool:
    existing_value = redis_client.get(key)
    if existing_value and existing_value.decode("utf-8") == str(value):
        return False
    expiration = 604800
    redis_client.set(key, value, ex=expiration)
    return True


def _has_exceeded_pending_mergeability_wait(pr: PullRequest, message: str) -> bool:
    """
    Determine whether the pull request has been waiting for too long in the pending
    mergeability state with the provided error message.
    """
    key = f"pm-{pr.id}-{message}"
    existing_value = redis_client.get(key)
    if existing_value:
        duration = time_util.now() - datetime.datetime.fromisoformat(
            existing_value.decode("utf-8")
        )
        return duration.total_seconds() > 300  # 5 minutes

    value = time_util.now().isoformat()
    redis_client.set(key, value, ex=3600)  # 1 hour
    return False


def can_skip_process_top(unique_id: int) -> bool:
    """
    If we just processed the same PR, skip it. We will eventually come back to it.
    This will help reduce load on both GH rate limits and our queues.
    """
    key = f"process-top-{unique_id}"
    existing_value = redis_client.get(key)
    if existing_value:
        # if old cache key exists, that means we just processed this, so let's skip.
        return True

    # set a 10 seconds cache
    redis_client.set(key, 1, ex=10)
    return False


def should_process_pr(
    pr: PullRequest,
    test_name: str,
    *,
    is_bot_pr: bool = False,
    is_pr_pending: bool = False,
) -> bool:
    """
    Check if the PR should be processed at this point. The goal is to avoid processing
    the PR too often, at the same time, identify clear signs that we should process it now.
    Since process_pr is typically called every minute, we do not need to be too exhaustive.

    We will return True if the given test_name is a required check and:
    - either the PR has a definite failure (status = "failure", "error")
    - or all the required checks are completed.
    """
    repo = pr.repo
    required_patterns: set[str] = {
        test.name for test in repo.get_required_tests(botpr=is_bot_pr)
    }
    is_required_test_name = (
        len(checks.get_matching_patterns(required_patterns, test_name)) > 0
    )
    if not is_required_test_name and not repo.require_all_checks_pass:
        return False
    return _has_finished_pending_tests(
        repo,
        pr,
        is_bot_pr=is_bot_pr,
        is_pr_pending=is_pr_pending,
        associated_test_name=test_name,
    )


def is_required_check(
    pr: PullRequest,
    test_name: str,
) -> bool:
    """
    Check if the test is a required test or not. This can be used to check individual tests.
    """
    repo = pr.repo
    required_patterns: set[str] = {
        test.name for test in repo.get_required_tests(botpr=False)
    }
    if repo.require_all_checks_pass:
        return True

    if len(checks.get_matching_patterns(required_patterns, test_name)):
        return True

    return False


def _has_finished_pending_tests(
    repo: GithubRepo,
    pr: PullRequest,
    *,
    is_bot_pr: bool = False,
    is_pr_pending: bool = False,
    associated_test_name: str = "",
) -> bool:
    """
    Returns whether the PR has finished all pending test status. This method only checks
    the database and does not make any external API calls.
    This is not a fool-proof way of validating CI check and should only be used as a
    quick way to determine if we should make further API calls.
    """
    required_tests = repo.get_required_tests(botpr=is_bot_pr)
    test_statuses = db.session.scalars(
        sa.select(GithubTestStatus)
        .where(
            GithubTestStatus.head_commit_sha == pr.head_commit_sha,
            GithubTestStatus.repo_id == pr.repo_id,
            GithubTestStatus.deleted.is_(False),
        )
        # Use the name of the test for matching the wildcard. Eagerly load this
        # to avoid N+1.
        .options(joinedload(GithubTestStatus.github_test)),
    ).all()

    # This is not a foolproof logic but it helps with reducing the number of API calls
    # we make to GitHub. We will only make further API calls if we are "somewhat" sure that
    # the PR is not pending.
    test_ids = [test.id for test in required_tests]
    acceptable_statuses_map = custom_checks.get_acceptable_statuses_map(
        test_ids, for_override=is_bot_pr
    )
    summary = checks.required_test_summary(
        repo, test_statuses, required_tests, acceptable_statuses_map, botpr=is_bot_pr
    )
    logger.info(
        "Repo %d PR %d test_name %s summary: %s, pending tests: %s failing tests: %s",
        repo.id,
        pr.number,
        associated_test_name,
        summary.result,
        summary.pending_urls.keys(),
        summary.failure_urls.keys(),
    )
    if summary.result == "failure":
        # If the PR is pending, we should not process it since a failed required
        # test would not move a PR from pending to ready.
        #
        # If PR is not pending, then a failure can help dequeue a PR.
        # This is not a foolproof way to determine failure,
        # but we are just doing high level checks before we process the PR.
        return not is_pr_pending

    return summary.result != "pending"


def is_bot_pr_top(bot_pr: BotPr, repo: GithubRepo) -> bool:
    if repo.parallel_mode_config.use_affected_targets:
        top_prs = target_manager.get_all_top_prs(
            repo, bot_pr.pull_request.target_branch_name
        )
        for pr in top_prs:
            if pr.id == bot_pr.pull_request_id:
                return True
        return False
    else:
        bot_pr_list = common.get_top_bot_prs(
            repo,
            bot_pr.pull_request.target_branch_name,
        )
        return bool(bot_pr_list and bot_pr_list[0].id == bot_pr.id)


def get_commit_message(pr: PullRequest) -> str:
    return (pr.merge_operations.commit_message if pr.merge_operations else "") or ""


def get_commit_title(pr: PullRequest) -> str:
    return (pr.merge_operations.commit_title if pr.merge_operations else "") or ""


def _get_latest_bot_pr(repo: GithubRepo, base_branch: str) -> BotPr | None:
    """
    Find the latest bot PR that covers the previous PRs.
    """
    bot_pr: BotPr | None = db.session.scalar(
        sa.select(BotPr)
        .where(BotPr.repo_id == repo.id, BotPr.status == "queued")
        .where(BotPr.target_branch_name == base_branch)
        .order_by(BotPr.created.desc())
    )
    return bot_pr


def get_oldest_bot_pr(
    repo: GithubRepo, base_branch: str, previous_prs: list[PullRequest]
) -> BotPr | None:
    """
    Difference between this method and _get_latest_bot_pr is that in this method we try to
    find the oldest bot PR that covers all the previous PRs.
    """
    bot_pr_id_rows = []
    if previous_prs:
        bot_pr_id_rows = db.session.execute(
            sa.select(
                bot_pr_mapping.c.bot_pr_id,
                sa.func.count(bot_pr_mapping.c.pull_request_id),
            )
            .where(bot_pr_mapping.c.pull_request_id.in_([pr.id for pr in previous_prs]))
            .group_by(bot_pr_mapping.c.bot_pr_id)
            .having(
                sa.func.count(bot_pr_mapping.c.pull_request_id)
                == len(set(previous_prs))
            )
        ).all()

    q = (
        sa.select(BotPr)
        .where(BotPr.repo_id == repo.id, BotPr.status == "queued")
        .where(BotPr.target_branch_name == base_branch)
    )
    if not bot_pr_id_rows:
        return None

    q = q.where(BotPr.id.in_([bp_id for bp_id, _ in bot_pr_id_rows]))
    bot_pr: BotPr | None = db.session.scalar(q.order_by(BotPr.created.asc()))
    return bot_pr


@celery.task(
    autoretry_for=(Exception,),
    retry_backoff=10,
    retry_kwargs={"max_retries": 5},
    retry_jitter=True,
)
def update_gh_pr_status(pr_id: int, *, status: str = "") -> None:
    comments.update_pr_sticky_comment.delay(pr_id)
    # get the PR so we can update the associated checks
    pr = PullRequest.get_by_id_x(pr_id)
    av_checks.update_pr_check(pr, status)


@dataclasses.dataclass
class QueueDepthInfo:
    #: The total number of PRs that have requested to be merged.
    #: This is generally equal to ``processing`` + ``waiting``.
    queued: int

    #: The number of PRs being actively processed by the queue. In parallel mode
    #: this is in the range [0, max_parallel_builds]. In serial mode this is
    #: always equal to ``queued``.
    processing: int

    #: The number of PRs that are queued but not yet tagged. Only applicable for
    #: parallel mode (always 0 in serial mode).
    waiting: int


def queue_depth(repo: GithubRepo, branch: str | None) -> QueueDepthInfo:
    q = sa.select(PullRequest).where(
        PullRequest.repo_id == repo.id, PullRequest.status.in_(["queued", "tagged"])
    )
    if branch:
        q = q.where(PullRequest.target_branch_name == branch)
    pr_count: list[PullRequest] = db.session.execute(q).scalars().all()

    n_queued = len([pr for pr in pr_count if pr.status == "queued"])
    n_tagged = len([pr for pr in pr_count if pr.status == "tagged"])
    return QueueDepthInfo(
        queued=n_queued + n_tagged,
        processing=n_tagged if repo.parallel_mode else n_queued,
        waiting=n_queued if repo.parallel_mode else 0,
    )


def _get_graphql(repo: GithubRepo) -> graphql.GithubGql:
    access_token, client = common.get_client(repo)
    if not access_token:
        raise Exception("Could not fetch access token for repo %s" % repo.name)

    return graphql.GithubGql(access_token.token, access_token.account_id)


def _fetch_gh_mergeable_prs(
    repo: GithubRepo,
    candidate_prs: list[PullRequest],
) -> list[PullRequest]:
    """
    Query the GitHub API to determine which of the given pull requests are
    mergeable (from GitHub's perspective -- i.e., approved and don't have merge
    conflicts).
    """
    if not candidate_prs:
        return []

    gql = _get_graphql(repo)
    pr_gh_node_ids = [pr.gh_node_id for pr in candidate_prs if pr.gh_node_id]
    try:
        result = gql.get_pull_requests_by_node_id(pr_gh_node_ids)
    except gh_graphql.GraphQLClientHttpError as exc:
        if exc.status_code == 401:
            logger.info(
                "Failed to fetch pull requests (authorization failed)", exc_info=exc
            )
            return []
        raise

    mergeable_numbers = {
        pr.number for pr in result if pr and _pull_info_is_gh_mergeable(pr)
    }
    return [pr for pr in candidate_prs if pr.number in mergeable_numbers]


def _pull_info_is_gh_mergeable(pull_info: gh_graphql.PullRequestInfo) -> bool:
    return (
        # Note: a review_decision of `None` indicates that the repository does
        # not require reviews for merging, so we have to check for that here.
        pull_info.review_decision in ("APPROVED", None)
        and pull_info.mergeable is gh_graphql.MergeableState.MERGEABLE
    )


def notify_if_mergeable(pr: PullRequest) -> None:
    """
    This method uses internal DB state to identify if the PR is mergeable.
    It basically makes the same checks as process_fetched_pull but without
    using the pull object from GH.
    """
    if pr.status != "open" or pr.is_draft:
        # No need to check if the PR is not open. If it's in pending or queued state,
        # then we anyways don't need to notify. We only want to notify if the
        # PR is not queued
        logger.info(
            "Ignoring notify as not open",
            repo_id=pr.repo_id,
            pr_number=pr.number,
            status=pr.status,
        )
        return

    if not pr.is_codeowner_approved:
        return
    if pr.status_code == StatusCode.MERGE_CONFLICT:
        return
    if _has_finished_pending_tests(pr.repo, pr, is_pr_pending=True):
        logger.info(
            "Notifying user about PR ready to queue",
            repo_id=pr.repo_id,
            pr_number=pr.number,
            status=pr.status,
        )
        notify_pr_ready_to_queue.delay(pr.id)


def load_queued_pull_requests(
    repo_id: int,
    *,
    count: int = 100,
    offset: int = 0,
    target_branch: str,
    for_queue_position: bool = False,
) -> list[PullRequest]:
    q = (
        sa.select(PullRequest)
        .where(
            PullRequest.repo_id == repo_id,
            PullRequest.status.in_(["queued", "tagged"]),
            PullRequest.deleted.is_(False),
        )
        .order_by(
            PullRequest.skip_line.desc(),
            PullRequest.queued_at.asc(),
            PullRequest.created.asc(),
        )
    )
    if target_branch:
        q = q.where(PullRequest.target_branch_name == target_branch)

    if for_queue_position:
        q = q.where(PullRequest.stack_parent_id.is_(None))

    return db.session.scalars(q.offset(offset).limit(count)).all()


def format_github_exception(e: pygithub.GithubException) -> str:
    texts = [str(e.data["message"])] if isinstance(e.data, dict) else []
    if e.headers and e.headers.get("x-github-request-id", None):
        texts.append("x-github-request-id: " + str(e.headers["x-github-request-id"]))
    return "\n".join(texts)


def simple_or_stack_queue(
    repo: GithubRepo,
    target_pr: PullRequest,
    *,
    ready_source: ReadySource,
    ready_user_id: int | None = None,
    ready_gh_user_id: int | None = None,
    client: GithubClient | None = None,
) -> bool:
    """
    Single point of entry to ensure that both regular merge requests and stack
    merge requests can be handled coherently.

    Returns true if the PR was queued successfully, false otherwise.
    """
    pr_stack = [target_pr]
    is_stack = stack_manager.is_in_stack(target_pr)
    if is_stack:
        logger.info(
            "Request queue action for stacked PR", repo_id=repo.id, pr=target_pr.number
        )
        pr_stack = stack_manager.get_current_stack_ancestors(target_pr)
        if not _stack_merge(repo, target_pr, pr_stack, client=client):
            return False

    for pr in pr_stack:
        common.ensure_merge_operation(
            pr,
            ready=True,
            ready_source=ready_source,
            ready_user_id=ready_user_id,
            ready_gh_user_id=ready_gh_user_id,
        )
    if not is_stack:
        # When it's a regular PR, we still imitate applying a label.
        fetch_pr.delay(
            target_pr.number, repo.name, action="labeled", label=repo.queue_label
        )
    return True


def _stack_merge(
    repo: GithubRepo,
    pr: PullRequest,
    pr_stack: list[PullRequest],
    client: GithubClient | None = None,
) -> bool:
    """
    Returns true if the PR was queued successfully, false otherwise.
    """
    if not client:
        _, client = common.get_client(repo)

    result, message = stack_manager.validate_and_set_stack_as_ready(
        client,
        pr,
        pr_stack,
    )
    if not result:
        client.create_issue_comment(pr.number, message)
        return False

    # In case of a successful stack-queue, the sticky-comment will be updated through the fetch async task.
    ancestor_prs = stack_manager.get_current_stack_ancestors(pr)
    ancestor_prs.reverse()
    for pr in ancestor_prs:
        # pretend as if the PR was labeled ready. This should also update the sticky comment.
        fetch_pr.delay(pr.number, repo.name, action="labeled", label=repo.queue_label)
    return True


def simple_or_stack_dequeue(
    repo: GithubRepo,
    pr: PullRequest,
    *,
    ready_source: ReadySource,
    ready_user_id: int | None = None,
    ready_gh_user_id: int | None = None,
    client: GithubClient | None = None,
    gh_pull: pygithub.PullRequest | None = None,
    label: str | None = None,
) -> None:
    if stack_manager.is_in_stack(pr):
        logger.info(
            "requested dequeue action for stacked PR", repo_id=repo.id, pr=pr.number
        )
        _cancel_stack(repo, pr)
        return

    logger.info(
        "requested dequeue action for regular PR", repo_id=repo.id, pr=pr.number
    )
    common.ensure_merge_operation(
        pr,
        ready=False,
        ready_source=ready_source,
        ready_user_id=ready_user_id,
        ready_gh_user_id=ready_gh_user_id,
    )
    if not client:
        _, client = common.get_client(repo)
    if not gh_pull:
        gh_pull = client.get_pull(pr.number)
    if not label:
        # Remove the label when this was not called with an unlabel action.
        remove_ready_labels(client, repo, gh_pull)

    # Pretend as if the PR was unlabelled. This should also update the sticky
    # comment.
    fetch_pr.delay(
        pr.number, repo.name, action="unlabeled", label=label or repo.queue_label
    )


def _cancel_stack(
    repo: GithubRepo, pr: PullRequest, *, label: str | None = None
) -> None:
    result, message = stack_manager.cancel_stack_merge(pr)
    _, client = common.get_client(repo)
    if not result:
        client.create_issue_comment(pr.number, message)
        return

    gh_pull = client.get_pull(pr.number)
    if not label:
        # Remove the label from the specified PR, typically label should only
        # be associated with one PR, but this can also be a hole where the
        # parent PRs also have queue label.
        remove_ready_labels(client, repo, gh_pull)

    # In case of a successful stack-queue, the sticky-comment will be updated through the fetch async task.
    prs = stack_manager.get_current_stack_ancestors(pr, skip_merged=True)
    prs.reverse()
    for pr in prs:
        # Pretend as if the PR was unlabelled. This should also update the
        # sticky comment.
        fetch_pr.delay(pr.number, repo.name, action="unlabeled", label=repo.queue_label)


def update_approval(pr: PullRequest) -> tuple[bool, bool]:
    gql = _get_graphql(pr.repo)
    return _update_approval(gql, pr)


def _update_approval(gql: graphql.GithubGql, pr: PullRequest) -> tuple[bool, bool]:
    repo = pr.repo
    review_decision = gql.get_review_decision(repo.name, pr.number)
    # Special case repositories that don't require reviews. These are usually
    # only for demo/testing purposes. We do however enforce that if a changes
    # requested review was submitted, we will refuse to merge.
    if (
        repo.preconditions.number_of_approvals == 0
        and review_decision != "CHANGES_REQUESTED"
    ):
        review_decision = "APPROVED"

    pr_changes_requested = review_decision == "CHANGES_REQUESTED"
    # TODO: When we ultimately deprecate required number of approvals in MQ
    #       settings, this becomes redundant with pr_is_approved.

    pr_codeowner_approved = review_decision == "APPROVED"

    # DEPRECATION COMPAT
    # The "required approvals" setting (within Aviator) has been deprecated.
    # Instead, we suggest using GitHub's code owners features. For repositories
    # where this is enabled (i.e., where it's not the default value of 1), we
    # still need to do all the requisite checks until we officially retire this
    # feature. But if it's not enabled (vast majority of repos), we can skip
    # this step and save an API call (preserving that precious rate limit).
    # pr_is_approved = (
    #     client.is_approved(
    #         pull,
    #         repo.approvals,
    #         [u.username for u in repo.required_users],
    #     )
    #     if repo.approvals > 1
    #     else review_decision == "APPROVED"
    # )
    pr.gh_review_decision = review_decision
    if pr.is_codeowner_approved != pr_codeowner_approved:
        pr.is_codeowner_approved = pr_codeowner_approved
    db.session.commit()
    return pr_codeowner_approved, pr_changes_requested
