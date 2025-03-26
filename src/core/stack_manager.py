from __future__ import annotations

import datetime

import structlog

from core import common, locks, pygithub
from core.client import GithubClient
from core.models import GithubRepo, PullRequest, ReadySource, StatusCode
from main import redis_client

logger = structlog.stdlib.get_logger()


def validate_and_set_stack_as_ready(
    client: GithubClient,
    target_pr: PullRequest,
    pr_stack: list[PullRequest],
) -> tuple[bool, str]:
    """
    Calculates the stack from that point back, validates the PRs are up-to-date,
    and sets the stack as ready. Note that we do not validate CI here when flagging as ready.
    Those validations are done by <code>process_fetched_pull</code>.
    """
    # using the first PR as the baseline since that should be consistent for the entire stack.
    root_pr = pr_stack[-1]
    lock_name = "stack-%d-pr-%d" % (target_pr.repo_id, root_pr.number)
    with locks.lock("stack", lock_name, expire=120):
        for pr in pr_stack:
            if pr.status == "merged":
                logger.info(
                    "Cannot queue PR as part of substack is already merged",
                    repo_id=target_pr.repo_id,
                    target_pr=target_pr.number,
                    starting_pr=pr.number,
                )
                msg = f"Cannot queue PR: substack #{pr.number} is already merged. Please run `av sync` and try again."
                return False, msg
            if pr.status == "closed":
                logger.info(
                    "Cannot queue PR as part of substack is closed",
                    repo_id=target_pr.repo_id,
                    target_pr=target_pr.number,
                    starting_pr=pr.number,
                )
                msg = f"Cannot queue PR: substack #{pr.number} is closed. Please update the stack and try again"
                return False, msg
            if common.has_merge_operation(pr):
                logger.info(
                    "Repo %d PR %d cannot queue PR as part of substack is still queued (starting at #%d)",
                    target_pr.repo_id,
                    target_pr.number,
                    pr.number,
                )
                msg = f"Cannot queue PR: substack (starting at #{pr.number}) is {pr.status}."
                return False, msg
            # important edge case: make sure the PR has a stack parent before
            # checking that it's up to date (otherwise we'll be checking that
            # the root PR is up-to-date with the trunk branch, which is not what
            # we want).
            if pr.stack_parent_id and not _is_pr_up_to_date(client, pr):
                msg = f"Cannot queue this stack as PR #{pr.number} is not up-to-date."
                logger.info("Repo %d PR %d %s", pr.repo_id, pr.number, msg)
                return False, msg
        for pr in pr_stack:
            # Before we can queue the PRs, we have to remove all the blocked labels,
            # otherwise the state can become inconsistent.
            client.remove_label_by_pr_number(pr.number, pr.repo.blocked_label)
        logger.info(
            "Repo %d successfully queued stacked PRs %s",
            pr.repo_id,
            [pr.number for pr in pr_stack],
        )
    return True, ""


def cancel_stack_merge(pr: PullRequest) -> tuple[bool, str]:
    """
    Cancel the merge of the stack PR.
    """
    pr_stack = get_current_stack_ancestors(pr)
    if not is_in_stack(pr):
        return False, "PR is not in a stack"

    root_pr = pr_stack[-1]
    lock_name = "stack-%d-pr-%d" % (pr.repo_id, root_pr.number)
    with locks.lock("stack", lock_name, expire=120):
        for pr in pr_stack:
            if not common.has_merge_operation(pr):
                logger.warning(
                    "Cannot cancel stack merge as stack is invalid.",
                    repo_id=pr.repo_id,
                    pr_number=pr.number,
                )
                return False, "This PR is not queued in the stack."
        for pr in pr_stack:
            common.ensure_merge_operation(
                pr, ready=False, ready_source=ReadySource.UNKNOWN
            )
    return True, ""


def is_stack_queued(some_pr: PullRequest) -> bool:
    """
    Given one PR in the stack, check if the entire stack (that was marked ready) is queued.
    If the PR is not in the stack, return True.
    """
    # only look at PRs before current one.
    lower_stack = get_current_stack_ancestors(some_pr)[1:]
    for pr in lower_stack:
        if not common.has_merge_operation(pr):
            logger.error(
                "Found a child PR in the stack that is not ready",
                repo_id=pr.repo_id,
                pr_number=pr.number,
            )
            return False
        if pr.status not in ("queued", "tagged"):
            logger.error(
                "Found a child PR in the stack that is not queued / tagged",
                repo_id=pr.repo_id,
                pr_number=pr.number,
                status=pr.status,
            )
            return False
    # only look at PRs after current one.
    upper_stack = get_ready_stack_after_pr(some_pr)[1:]
    for pr in upper_stack:
        if pr.status not in ("queued", "tagged"):
            return False
    return True


def is_stack_queue_eligible(
    some_pr: PullRequest,
) -> tuple[bool, list[PullRequest]]:
    """
    Given one PR in the stack, check if the entire stack (that was marked ready) is ready
    to be queued, ignoring the passed PR.

    We calculate eligibility based on the StatusCode.PENDING_STACK_READY which represents
    that this particular PR is ready, just waiting on other PRs in the queue. So if all PRs
    in the stack are PENDING_STACK_READY, the stack is eligible to be queued.

    Returns whether the stack is queue eligible along with the PRs that are still pending.
    It also returns true if the stack was already queued, or if the PR was not in a stack.
    """
    # only look at PRs before current one.
    lower_stack = get_current_stack_ancestors(some_pr, skip_merged=True)[1:]
    pending_prs = []
    for pr in lower_stack:
        if not common.has_merge_operation(pr):
            logger.error(
                "Found a child PR in the stack that is not ready",
                pr_number=pr.number,
            )
            return False, []
        if pr.status == "pending":
            if pr.status_code is not StatusCode.PENDING_STACK_READY:
                return False, []
            # else capture PR for state change
            pending_prs.append(pr)
            continue
        if pr.status not in ("queued", "tagged"):
            return False, []

    # only look at PRs after current one.
    upper_stack = get_ready_stack_after_pr(some_pr)[1:]
    for pr in upper_stack:
        if pr.status == "pending":
            if pr.status_code is not StatusCode.PENDING_STACK_READY:
                return False, []
            pending_prs.append(pr)
            continue
        if pr.status not in ("queued", "tagged"):
            return False, []

    return True, pending_prs


def update_stack_queued_at(some_pr: PullRequest, queued_at: datetime.datetime) -> bool:
    """
    Given one PR in the stack, update queued_at for the entire stack.
    """
    # only look at PRs before current one.
    lower_stack = get_current_stack_ancestors(some_pr)[1:]
    for pr in lower_stack:
        if pr.status != "queued":
            logger.error(
                "Repo %d PR %d found an ancestor in the stack that is not queued",
                pr.repo_id,
                pr.number,
            )
            return False
        pr.queued_at = queued_at
    # only look at PRs after current one.
    upper_stack = get_ready_stack_after_pr(some_pr)[1:]
    for pr in upper_stack:
        if pr.status != "queued":
            logger.error(
                "Repo %d PR %d found a child PR in the stack that is not queued",
                pr.repo_id,
                pr.number,
            )
            return False
        pr.queued_at = queued_at
    return True


def is_top_queued_pr(pr: PullRequest) -> bool:
    """
    Given a PR in the stack, returns whether this is the top PR in the stack.
    """
    upper_stack = get_ready_stack_after_pr(pr)
    if len(upper_stack) != 1:
        logger.info(
            "PR has dependent, is not top of stack.",
            pr_number=pr.number,
            repo=pr.repo_id,
            upper_stack=[p.number for p in upper_stack],
        )
    return len(upper_stack) == 1


def is_in_stack(pr: PullRequest) -> bool:
    return bool(pr.stack_parent_id or pr.stack_dependents)


def _is_pr_up_to_date(client: GithubClient, pr: PullRequest) -> bool:
    if not pr.base_branch_name or not pr.branch_name:
        logger.warning(
            "Repo %d PR %d has base_branch_name=%s, branch_name=%s",
            pr.repo_id,
            pr.number,
            pr.base_branch_name or "<missing>",
            pr.branch_name or "<missing>",
        )
        return False
    compare = client.repo.compare(pr.base_branch_name, pr.branch_name)
    return compare.behind_by == 0


def get_stack_root(pr: PullRequest) -> PullRequest:
    while pr.stack_parent and pr.stack_parent.status != "merged":
        pr = pr.stack_parent
    return pr


def get_current_stack_ancestors(
    pr: PullRequest | None, skip_merged: bool = False
) -> list[PullRequest]:
    """
    Given a PR, return the stack of PRs that are ancestors of the PR. The returned list
    is in order from the current PR to the root.

    skip_merged: If true, the ancestors list will stop at the first merged PR.
    """
    pr_stack: list[PullRequest] = []
    while pr and pr not in pr_stack:
        if not skip_merged and pr.status == "merged":
            break
        pr_stack.append(pr)
        pr = pr.stack_parent
    return pr_stack


def get_all_stack_ancestors(pr: PullRequest | None) -> list[PullRequest]:
    """
    :param pr: the pull request whose ancestors we find
    :return: the list of ancestors of the current pr not including the current PR
    """
    pr_stack: list[PullRequest] = []
    while pr and pr not in pr_stack:
        pr_stack.append(pr)
        pr = pr.stack_parent
    return pr_stack[1:]


def get_stack_linearized(pr: PullRequest) -> list[PullRequest]:
    """
    Get a linear (topographical) ordering of pull requests in the stack.

    The first PullRequest in the list is the root PR of the stack.
    """
    root = get_stack_root(pr)
    return [root, *get_stack_descendants(root, [])]


def get_stack_descendants(
    pr: PullRequest, parent_stack_list: list[int]
) -> list[PullRequest]:
    pr_stack: list[PullRequest] = []
    for descendant in pr.stack_dependents:
        if descendant.status == "merged":
            # if the descendant has been merged then we don't care about it
            continue
        if descendant.number in parent_stack_list:
            # if the descendant is already in the stack, then we have a cycle
            # and we should not go down this path.
            logger.error(
                "Cycle detected in stack",
                repo_id=pr.repo_id,
                parent_stack_list=parent_stack_list,
                descendant_number=descendant.number,
            )
            continue
        pr_stack.append(descendant)
        pr_stack.extend(get_stack_descendants(descendant, [p.number for p in pr_stack]))
    return pr_stack


def get_ready_stack_after_pr(pr: PullRequest | None) -> list[PullRequest]:
    # At any given time, we should assume that the ready stack is linear.
    # Because even if the entire stack is non-linear, all PRs leading up to the tagged PR
    # should form a linear path.
    pr_stack: list[PullRequest] = []
    while pr and pr not in pr_stack:
        pr_stack.append(pr)
        pr = _get_ready_dependent(pr)
        if not pr:
            break
    return pr_stack


def _get_ready_dependent(pr: PullRequest) -> PullRequest | None:
    for sd_pr in pr.stack_dependents:
        if common.has_merge_operation(sd_pr):
            return sd_pr
    return None


def pre_merge_actions(pr: PullRequest, pull: pygithub.PullRequest) -> None:
    """
    If the given pr is in a stack, update the PR's base branch to the target branch of the PR.
    """
    if not is_in_stack(pr):
        return
    if pr.base_branch_name == pr.target_branch_name or not pr.target_branch_name:
        return
    if pr.repo.merge_strategy.use_separate_commits_for_stack:
        # If we are going to merge separate stacked PRs, don't change base branch.
        return
    logger.info("Updating base branch of PR %d to %s", pr.number, pr.target_branch_name)
    # Set the base branch in cache to ensure we can set back same one.
    # We don't use the DB version here to avoid scenarios where the webhook updates the base branch.
    set_cache_base_branch(pr)
    pull.edit(base=pr.target_branch_name)


def failed_merge_actions(pr: PullRequest, pull: pygithub.PullRequest) -> None:
    """
    If the given pr is in a stack, update the PR's base branch back to the original cached base branch.
    """
    if not is_in_stack(pr):
        return
    base_branch = get_cache_base_branch(pr)
    if base_branch:
        logger.info(
            "Updating base branch of PR %d to %s", pr.number, pr.target_branch_name
        )
        try:
            pull.edit(base=base_branch)
        except:
            # Best effort, don't fail the whole process if this fails.
            logger.warning("Failed to update base branch of PR %d", pr.number)
    else:
        logger.warning(
            "No base branch found in cache for repo %d PR %d", pr.repo_id, pr.number
        )


def post_merge_actions(
    client: GithubClient,
    repo: GithubRepo,
    pr: PullRequest,
) -> None:
    # At some point, depending on the queue mode (FIFO vs parallel, etc.), we
    # might want to do some things here (eventually we want to automatically
    # fix-up dependent PRs after their parent merges by rebasing them on top of
    # the squash commit that landed in the mainline branch).
    for dep in pr.stack_dependents:
        try:
            dep_pull = client.repo.get_pull(dep.number)
            dep_pull.create_issue_comment(
                f"This PR is stacked on top of #{pr.number} which was merged. "
                f"This PR (and its dependents) need to be synchronized on the "
                f"commit that was merged into `{pr.target_branch_name}`. "
                f"From your repository, run the following commands:\n"
                f"```sh\n"
                f"# Switch to this branch\n"
                f"git checkout {dep.branch_name}\n"
                f"# Sync this branch (and its children) on top of the merge commit\n"
                f"av sync\n"
                f"```\n"
            )
        except Exception as e:
            logger.warning(
                "Failed to post message to stack dependent",
                repo_id=repo.id,
                pr_number=pr.number,
                dep_pr_number=dep.number,
                exc_info=e,
            )


def set_cache_base_branch(pr: PullRequest) -> None:
    expiration_secs = 120
    key = "stack-pr-%d" % pr.id
    if not pr.base_branch_name:
        raise Exception("No base branch found for PR %d" % pr.number)
    redis_client.set(key, pr.base_branch_name, ex=expiration_secs)


def get_cache_base_branch(pr: PullRequest) -> str | None:
    key = "stack-pr-%d" % pr.id
    value = redis_client.get(key)
    return value.decode("utf-8") if value else None


def get_stacked_pulls(
    pr: PullRequest, client: GithubClient
) -> list[pygithub.PullRequest]:
    pulls = []
    if is_in_stack(pr):
        # Note: we use pr.stack_parent here since we do not want to include the current PR.
        for p in get_current_stack_ancestors(pr.stack_parent):
            pulls.append(client.get_pull(p.number))

    return pulls
