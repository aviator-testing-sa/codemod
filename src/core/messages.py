from __future__ import annotations

import datetime
import itertools

import sqlalchemy as sa
import structlog

import core.custom_validations
from core import checks, custom_checks, stack_manager
from core.models import Activity, BotPr, ChangeSet, GithubRepo, PullRequest
from core.status_codes import StatusCode
from main import app, db

logger = structlog.stdlib.get_logger()


def get_message_body(
    repo: GithubRepo,
    pr: PullRequest,
) -> str:
    chunks: list[str] = []
    if pr.status == "open":
        chunks.extend(_get_open_message_parts(repo, pr))
    elif pr.status == "merged":
        chunks.extend(_get_merged_message_parts(repo, pr))
    elif pr.status in ("queued", "tagged"):
        chunks.extend(_get_queued_message_parts(repo, pr))
    elif pr.status in ("blocked", "pending"):
        chunks.extend(_get_blocked_message_parts(repo, pr))
    else:
        logger.warning(
            "unknown PR state for Repo %d PR %d: %s", pr.repo_id, pr.number, pr.status
        )
        chunks.append(
            f"This PR is currently in state `{pr.status}` ({pr.status_description})."
        )

    if pr.stack_parent or pr.stack_dependents:
        chunks.extend(_get_stack_message_parts(repo, pr))

    if pr.change_sets:
        chunks.extend(_get_changesets_message_parts(pr))

    chunks.extend(_get_affected_target_parts(repo, pr))
    chunks.extend(_get_custom_validation_parts(repo, pr))

    return "\n\n".join(chunks)


def _get_open_message_parts(repo: GithubRepo, pr: PullRequest) -> list[str]:
    parts: list[str] = ["This pull request is currently **open** (not queued)."]

    # Show the stack merge instructions regardless of whether or not there is a
    # custom message set.
    is_stacked = stack_manager.is_in_stack(pr)
    if is_stacked:
        parts.append(
            f" Comment `/aviator stack merge` to merge this stack into "
            f"{pr.target_branch_name} when it's ready. **Do not** use the "
            f"GitHub merge button to merge this PR."
        )
        if pr.stack_dependents:
            parts.append(
                "\n> **This PR is not at the end of the stack.** Using the "
                "merge command from this PR will only merge the PRs that "
                "come before this one in the stack (you'll have to rebase "
                "the later PRs in the stack using `av sync` from the "
                "command line)."
            )

    if repo.current_config.merge_rules.status_comment.open_message:
        return [
            repo.current_config.merge_rules.status_comment.open_message,
            "---",
            *parts,
        ]

    if not is_stacked:
        parts.append("### How to merge")
        parts.append(
            f" To merge this PR, comment `/aviator merge` or add the "
            f"`{pr.repo.queue_label}` label."
        )
    return parts


def _get_merged_message_parts(repo: GithubRepo, pr: PullRequest) -> list[str]:
    if pr.status_code == StatusCode.MERGED_MANUALLY:
        return [
            "This PR was merged manually (without Aviator). "
            "Merging manually can negatively impact the performance of the queue. "
            "Consider using Aviator next time."
        ]
    elif pr.merged_at:
        return ["This PR was merged using Aviator."]
    else:
        return [
            "This PR was closed without merging. "
            "If you still want to merge this PR, re-open it."
        ]


def _get_queued_message_parts(repo: GithubRepo, pr: PullRequest) -> list[str]:
    parts: list[str] = []

    # If there's a custom queued message, we still display the default message
    # since it details the current status of the pull request within the queue.
    if repo.current_config.merge_rules.status_comment.queued_message:
        parts.append(repo.current_config.merge_rules.status_comment.queued_message)
        parts.append("---")

    if not stack_manager.is_stack_queued(pr):
        parts.append(
            "This PR is waiting on one or more PRs in the rest of the stack to be ready before queuing. "
            "Please make sure all PRs in the stack are ready to be merged."
        )
    else:
        if pr.skip_line:
            position_text = "priority: skip line"
        else:
            position_text = "review queue position"

        position_md = _position_markdown(
            position_text, repo.name, pr.target_branch_name
        )
        parts.append(f"This PR is queued for merge ({position_md}).")
        if _is_delayed_by_parallel_mode_cap(repo, pr):
            parts.append(
                "⚠️⚠️ "
                "**MergeQueue has reached the configured maximum number of "
                "parallel test pull requests. This pull request will "
                "automatically be tested and merged when it closer to the "
                "front of the queue. You do not need to take any action.** "
                "⚠️⚠️"
            )
        else:
            parts.append(
                "It will be merged when it reaches the top of the queue and all "
                "required status checks are passing."
            )

        parts.extend(add_all_test_status_summaries(pr))

    return parts


def _is_delayed_by_parallel_mode_cap(repo: GithubRepo, pr: PullRequest) -> bool:
    if not repo.parallel_mode or not pr.status == "queued":
        return False
    activities_list: list[tuple[str, datetime.datetime]] = (
        db.session.query(
            Activity.name,
            sa.func.max(Activity.created),
        )
        .filter(Activity.pull_request_id == pr.id)
        .group_by(Activity.name)
        .all()
    )
    activities = {name: time for name, time in activities_list}
    delayed = activities.get("delayed")
    queued = activities.get("queued")
    if delayed and queued and delayed > queued:
        return True
    return False


def add_all_test_status_summaries(pr: PullRequest) -> list[str]:
    chunks: list[str] = []
    draft_is_passing = False
    if pr.repo.parallel_mode and pr.latest_bot_pr:
        draft_is_passing = add_test_status_summary(pr, chunks, pr.latest_bot_pr)

    # We want to add original PR CI checks if
    #   - PR is in pending state
    #   - draft PR passed but original PR has not
    if not pr.repo.parallel_mode or pr.status == "pending" or draft_is_passing:
        add_test_status_summary(pr, chunks)

    return chunks


def _get_blocked_message_parts(repo: GithubRepo, pr: PullRequest) -> list[str]:
    parts: list[str] = []

    # If there's a custom blocked message, we still display the default message.
    if repo.current_config.merge_rules.status_comment.blocked_message:
        parts.append(repo.current_config.merge_rules.status_comment.blocked_message)
        parts.append("---")

    parts.append(
        f"This PR is not ready to merge (currently in state {pr.status}): "
        f"{pr.status_description}."
    )
    if pr.status_reason:
        parts.append(pr.status_reason)

    if pr.status == "blocked" and pr.repo.blocked_label:
        parts.append(
            f"Once the issues are resolved, remove the `{pr.repo.blocked_label}` label "
            f"and re-queue the pull request. Note that the pull request will be automatically "
            f"re-queued if it has the `{pr.repo.queue_label}` label."
        )
    parts.extend(add_all_test_status_summaries(pr))
    return parts


def _get_stack_message_parts(repo: GithubRepo, pr: PullRequest) -> list[str]:
    history: list[str] = []
    # get_stack_ancestors includes the current PR, so exclude it here
    ancestors = stack_manager.get_current_stack_ancestors(pr)[1:]
    ancestors.reverse()
    for ancestor in ancestors:
        history.append(
            f"1. [**{ancestor.title.strip()}** #{ancestor.number}]({ancestor.number}){_get_pr_stack_status(ancestor)}"
        )
    history.append(
        f"1. 👉 [**{pr.title.strip()}** #{pr.number}]({pr.number}) 👈 (this pr)"
    )
    # NOTE: This won't reflect the actual hierarchy if multiple PRs have the
    # same PR as their parent, but it will still display the PRs in
    # topological order (i.e., if A is a parent of B, A is displayed before
    # B) which is fine for now.
    for descendant in stack_manager.get_stack_descendants(pr, []):
        history.append(
            f"1. [**{descendant.title.strip()}** #{descendant.number}]({descendant.number})"
        )

    return [
        "### Stack",
        "\n".join(history),
    ]


def _get_pr_stack_status(pr: PullRequest) -> str:
    if pr.status == "merged":
        return " (merged)"
    if pr.status == "closed":
        return " (closed)"
    return ""


def _get_changesets_message_parts(pr: PullRequest) -> list[str]:
    changesets = []
    for cs in pr.change_sets:
        changesets.append(f"* {_changeset_link_markdown(cs)} (Status: {cs.status})")
        for pr in cs.pr_list:
            changesets.append(f"  * {_markdown_link(pr.title, pr.url)}")
    return [
        "### Change Sets",
        "\n".join(changesets),
    ]


def _get_affected_target_parts(repo: GithubRepo, pr: PullRequest) -> list[str]:
    if not repo.parallel_mode_config.use_affected_targets or not pr.affected_targets:
        return []

    targets = list(pr.affected_targets)

    parts: list[str] = []
    names: list[str] = sorted([target.name for target in targets])[:3]
    parts.append("### Affected targets")
    parts.append("\n".join(f"* `{name}`" for name in names))

    if len(targets) > 3:
        parts.append("\n" + _markdown_link(f"+ {len(targets) - 3} more", pr.app_url))
    return parts


def _get_custom_validation_parts(repo: GithubRepo, pr: PullRequest) -> list[str]:
    parts: list[str] = []
    validation_issues = core.custom_validations.check_custom_validations(repo, pr)
    if validation_issues:
        parts.append("### Validation issues")
        for issue in validation_issues:
            parts.append(f"* {issue}")
    return parts


def add_test_status_summary(
    pr: PullRequest,
    chunks: list[str],
    latest_bot_pr: BotPr | None = None,
) -> bool:
    """
    Returns the test status summary for the given PR or latest bot pr.

    :param pr: The pull request
    :param chunks: List containing the test statuses.
    :param latest_bot_pr: If provided, get the test status summary for the latest_bot_pr instead of the given pr.

    :return bool: True if the PR is passing, else false if the PR still has pending or failing tests.
    """
    summary_map = custom_checks.get_many_pr_test_previews(
        pr.repo, [pr], for_bot_prs=bool(latest_bot_pr)
    )
    summary = summary_map.get(pr.id)
    if not summary:
        # This can happen due to a race condition where the provided latest_bot_pr is already
        # stale and we do not get any botPR summary. It's okay to return an empty summary here.
        logger.info(
            "No test summary found for repository",
            repo_id=pr.repo_id,
            pr_number=pr.number,
        )
        return False

    if latest_bot_pr:
        title = f"### Pending Status Checks (for Associated PR #{latest_bot_pr.number})"
    else:
        title = "### Pending Status Checks"
    chunks.append(title)

    bullets: list[str] = []
    for pending_test in itertools.chain(
        summary.pending_test_previews,
        summary.failure_test_previews,
    ):
        bullets.append(
            f"* {pending_test.status_emoji} "
            + _markdown_link(f"``` {pending_test.name} ```", pending_test.url)
            + f" ({pending_test.gh_status.description})"
        )
    if summary.remaining_pending_tests > 0:
        bullets.append(
            f"* {checks.EMOJI_PENDING} {summary.remaining_pending_tests} other pending tests"
        )
    if summary.total_success_tests > 0:
        bullets.append(
            f"* {checks.EMOJI_SUCCESS} {summary.total_success_tests} tests passing!"
        )

    if not bullets:
        chunks.append("This pull request has no reported status checks.")
        return True

    chunks.append("\n".join(bullets))
    return summary.total_pending_tests == 0 and summary.total_failure_tests == 0


def _markdown_link(text: str, url: str | None) -> str:
    if url:
        return f"[{text}]({url})"
    return text


def _position_markdown(
    position_text: str, repo_name: str, target_branch_name: str
) -> str:
    queue_url = (
        app.config["BASE_URL"]
        + f"/repo/{repo_name}/queue?targetBranch={target_branch_name}"
    )
    return _markdown_link(position_text, queue_url)


def _changeset_link_markdown(cs: ChangeSet) -> str:
    cs_url = app.config["BASE_URL"] + f"/changeset/{cs.number}"
    return _markdown_link(f"Change Set {cs.number}", cs_url)
