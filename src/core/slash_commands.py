from __future__ import annotations

import argparse
import dataclasses
import shlex
from collections.abc import Callable
from typing import Any, Never, override

import sqlalchemy as sa
import structlog

import errors
import flexreview.celery_tasks
import flexreview.fr_approvals
import flexreview.fr_teams
from core import (
    backport,
    changesets,
    checks,
    comments,
    common,
    messages,
    pygithub,
    queue,
    sync,
    target_manager,
)
from core.client import GithubClient
from core.models import (
    AuditLog,
    AuditLogAction,
    AuditLogEntity,
    BreakglassApproval,
    GithubRepo,
    GithubUser,
    PullRequest,
    ReadySource,
)
from flexreview.fr_teams import GHAssignmentResult
from main import db
from slackhook.events import notify_breakglass

logger = structlog.stdlib.get_logger()


@dataclasses.dataclass(frozen=True)
class SlashCommandArgs:
    full_command: str
    client: GithubClient
    repo: GithubRepo
    pr: PullRequest
    gh_pull: pygithub.PullRequest
    gh_user: GithubUser
    args: list[str]


def _handle_status(args: SlashCommandArgs) -> None:
    client = args.client
    repo = args.repo
    pr = args.pr
    gh_pull = args.gh_pull
    try:
        checks.fetch_latest_test_statuses(client, repo, gh_pull, botpr=False)
        queue.fetch_pr_with_repo(gh_pull.number, repo)
    except Exception:
        logger.exception("Failed to refresh pull request")
        comment = (
            "Failed to refresh this pull request due to an Aviator internal error. "
            "Please try again later or contact Aviator support."
        )
        client.create_issue_comment(pr.number, comment)
        raise

    # If not repo.active (MQ disabled), the state transition will not be triggered.
    # Handle this case here.
    if not repo.active:
        if gh_pull.state == "closed" and pr.status != "merged":
            queue.set_pr_as_closed_or_merged(
                client,
                pr,
                merged=gh_pull.merged,
                merged_at=gh_pull.merged_at,
                merged_by_login=gh_pull.merged_by.login if gh_pull.merged_by else None,
                merge_commit_sha=gh_pull.merge_commit_sha,
            )

    flexreview.fr_teams.process_fr_teams.delay(pr.id, assign_reviewer=False)
    flexreview.fr_approvals.process_fr_approvals.delay(pr.id)

    db.session.refresh(pr)

    if pr.status == "merged":
        flexreview.celery_tasks.process_merged_pull.delay(pr.id)

    # We describe this as a "snapshot" to make it more obvious that this comment
    # won't be updated when things change (unlike the sticky comment).
    issue_comment = "# Aviator status snapshot\n\n"
    issue_comment += messages.get_message_body(repo, pr)
    issue_comment += (
        "\n\n---\n"
        "You can also comment `/aviator sync` to update this pull request with "
        "the latest commits from the base branch."
    )
    client.create_issue_comment(pr.number, issue_comment)
    changesets.refresh_status_checks(pr)
    queue.update_gh_pr_status.delay(pr.id)


def _handle_merge(args: SlashCommandArgs) -> None:
    client = args.client
    repo = args.repo
    pr = args.pr
    gh_pull = args.gh_pull
    gh_user = args.gh_user
    full_command = args.full_command
    if pr.status == "merged":
        client.create_issue_comment(
            pr.number,
            "Error: Cannot add this pull request to the queue "
            "(the pull request is already merged).",
        )
        return

    if full_command:
        # Handle skip line
        if "--skip-line" in full_command:
            skip_line_reason = _get_skip_line_reason(full_command)
            if (
                repo.current_config.merge_rules.require_skip_line_reason
                and not skip_line_reason
            ):
                client.create_issue_comment(
                    pr.number,
                    "Error: Cannot add this pull request to the queue "
                    "(the repository requires a reason for skipping the line). "
                    'To skip the line in the queue, comment `/aviator merge --skip-line="<reason>"`.',
                )
                return
            if skip_line_reason:
                common.set_skip_line_reason(pr, skip_line_reason)
            if "--skip-validation" in full_command:
                common.set_skip_validation(pr)
            pr.skip_line = True
            db.session.commit()

        # Handle targets
        targets = _get_targets_from_command(full_command)
        if targets:
            try:
                target_manager.queue_pr_with_targets(repo, pr, targets, gh_user)
                queue.tag_queued_prs_async.delay(pr.repo_id)
            except errors.InvalidActionException as e:
                gh_pull.create_issue_comment(str(e))
                return

    success = queue.simple_or_stack_queue(
        repo,
        pr,
        client=client,
        ready_source=ReadySource.SLASH_COMMAND,
        ready_gh_user_id=gh_user.id,
    )
    if not success:
        return

    success_comment = (
        "Aviator has accepted the merge request. "
        "It will enter the queue when all of the required status checks have passed."
    )
    if repo.current_config.merge_rules.status_comment.publish != "never":
        sticky_url = comments.get_sticky_comment_url(client, pr)
        sticky_status_comment = "sticky status comment"
        if sticky_url:
            sticky_status_comment = f"[{sticky_status_comment}]({sticky_url})"
        success_comment += f" Aviator will update the {sticky_status_comment} as the pull request moves through the queue."
    client.create_issue_comment(pr.number, success_comment)


def _handle_stack_merge(args: SlashCommandArgs) -> None:
    _handle_merge(args)


def _handle_block_until(args: SlashCommandArgs) -> None:
    client = args.client
    pr = args.pr
    full_command = args.full_command
    parts = full_command.split(" ", maxsplit=5)
    if len(parts) < 3:
        client.create_issue_comment(
            pr.number,
            "Error: Invalid `/aviator block until` command. "
            "Usage: `/aviator block until <pr_number>`",
        )
        return
    if len(parts) > 3 and not full_command.endswith("is merged"):
        client.create_issue_comment(
            pr.number,
            "Error: Invalid `/aviator block until` command. "
            "Usage: `/aviator block until <pr_number> is merged`",
        )
        return
    target_pr = _get_pr_from_markdown(pr, parts[2])
    if not target_pr:
        logger.info(
            "Failed to find target PR",
            current_pr=pr.number,
            repo=pr.repo.name,
            pr_number=parts[2],
        )
        client.create_issue_comment(
            pr.number,
            f"Error: Pull request {parts[2]} not found.",
        )
        return
    if target_pr.status == "merged":
        client.create_issue_comment(
            pr.number,
            f"Error: Pull request {parts[2]} is already merged / closed.",
        )
        return
    common.block_pr_until_merged(pr, target_pr)
    client.create_issue_comment(
        pr.number,
        f"This pull request is now blocked until {target_pr.repo.name}#{target_pr.number} is merged.",
    )


def _handle_block_cancel(args: SlashCommandArgs) -> None:
    client = args.client
    pr = args.pr
    if pr.status == "merged":
        client.create_issue_comment(
            pr.number,
            "Error: This pull request is already merged.",
        )
        return
    common.cancel_blocking_prs(pr)
    client.create_issue_comment(
        pr.number,
        "This pull request is no longer blocked on any PR.",
    )


def _handle_stack_cancel(args: SlashCommandArgs) -> None:
    _handle_cancel(args)


def _handle_cancel(args: SlashCommandArgs) -> None:
    pr = args.pr
    client = args.client
    repo = args.repo
    gh_pull = args.gh_pull
    gh_user = args.gh_user
    if pr.status == "merged":
        client.create_issue_comment(
            pr.number,
            "Error: This pull request is already merged.",
        )
        return

    queue.simple_or_stack_dequeue(
        repo,
        pr,
        client=client,
        gh_pull=gh_pull,
        ready_source=ReadySource.SLASH_COMMAND,
        ready_gh_user_id=gh_user.id,
    )
    client.create_issue_comment(
        pr.number,
        "This pull request (or associated stack) has been cancelled and is no longer ready-to-merge.",
    )


def _handle_sync(args: SlashCommandArgs) -> None:
    pr = args.pr
    client = args.client
    repo = args.repo
    gh_pull = args.gh_pull
    # Skip sync if PR is already queued
    if pr.status in ("queued", "tagged"):
        client.create_issue_comment(
            pr.number,
            "Error: Pull requests cannot be synchronized when queued. Please dequeue first.",
        )
        return

    try:
        _, already_up_to_date = sync.merge_or_rebase_head(
            repo,
            client,
            gh_pull,
            gh_pull.base.ref,
        )
    except errors.PRStatusException as exc:
        client.create_issue_comment(pr.number, exc.message)
        return
    except Exception:
        logger.exception("Failed to synchronize pull request")
        client.create_issue_comment(
            pr.number,
            "Error: Failed to synchronize pull request (Aviator internal error). "
            "Please try again later or contact Aviator support.",
        )
        raise

    if already_up_to_date:
        client.create_issue_comment(
            pr.number,
            "This pull request is already up to date.",
        )
        return
    client.create_issue_comment(
        pr.number,
        f"This pull request was synchronized with `{gh_pull.base.ref}` successfully.",
    )


def _handle_backport(args: SlashCommandArgs) -> None:
    full_command = args.full_command
    gh_pull = args.gh_pull
    pr = args.pr
    parts = full_command.split(" ", maxsplit=2)
    if len(parts) != 2:
        gh_pull.create_issue_comment(
            "Invalid backport command.\nUsage: `/aviator backport <target branch>`",
        )
        return
    target = parts[1]
    backport.backport_pull_request.delay(pr.repo_id, pr.number, target)
    # We don't post a comment here since that happens in the backport task.


def _handle_flexreview_assign(args: SlashCommandArgs) -> None:
    repo = args.repo
    gh_pull = args.gh_pull
    pr = args.pr
    client = args.client
    if not repo.flexreview_active:
        gh_pull.create_issue_comment("FlexReview is not enabled for this repository.")
        return
    repo.bind_contextvars()
    pr.bind_contextvars()

    result = flexreview.fr_teams.stateless_process_pr(
        client,
        pr,
        repo,
        ignore_existing_for_assignment=True,
        trigger_for_auto_assignment=False,
        exclude_reviewers=[],
    )
    logger.info("Processed PR for FR teams.", result=result.status)
    gh_assignment_result = result.run_actions(client, pr, assign_reviewers=True)

    if gh_assignment_result == GHAssignmentResult.SHOULD_RETRY:
        logger.info("Failed to assign reviewer, retrying")
        result = flexreview.fr_teams.stateless_process_pr(
            client,
            pr,
            repo,
            ignore_existing_for_assignment=True,
            trigger_for_auto_assignment=False,
            exclude_reviewers=[],
        )
        result.run_actions(client, pr, assign_reviewers=True)
    gh_pull.create_issue_comment(result.to_slash_command_result_comment())


def _handle_flexreview_refresh(args: SlashCommandArgs) -> None:
    flexreview.fr_teams.process_fr_teams.delay(args.pr.id, assign_reviewer=False)
    flexreview.fr_approvals.process_fr_approvals.delay(args.pr.id)


def _handle_flexreview_suggest(args: SlashCommandArgs) -> None:
    repo = args.repo
    gh_pull = args.gh_pull
    pr = args.pr
    client = args.client
    if not repo.flexreview_active:
        gh_pull.create_issue_comment("FlexReview is not enabled for this repository.")
        return
    repo.bind_contextvars()
    pr.bind_contextvars()

    result = flexreview.fr_teams.stateless_process_pr(
        client,
        pr,
        repo,
        ignore_existing_for_assignment=True,
        trigger_for_auto_assignment=False,
        exclude_reviewers=[],
    )
    logger.info("Processed PR for FR teams.", result=result.status)
    result.run_actions(client, pr, assign_reviewers=False)
    gh_pull.create_issue_comment(result.to_slash_command_result_comment())


def _handle_flexreview_breakglass(args: SlashCommandArgs) -> None:
    pr = args.pr
    gh_pull = args.gh_pull
    repo = args.repo
    gh_user = args.gh_user
    if not repo.flexreview_active:
        gh_pull.create_issue_comment("FlexReview is not enabled for this repository.")
        return
    if not repo.enable_flexreview_approvals:
        gh_pull.create_issue_comment(
            "FlexReview validation is not enabled for this repository.",
        )
        return
    repo.bind_contextvars()
    pr.bind_contextvars()

    existing_record: BreakglassApproval | None = db.session.scalar(
        sa.select(BreakglassApproval).where(
            BreakglassApproval.pull_request_id == pr.id,
            BreakglassApproval.github_user_id == gh_user.id,
        ),
    )
    if existing_record:
        gh_pull.create_issue_comment(
            f"Error: Breakglass override already recorded by user @{existing_record.github_user.username}.",
        )
        return

    breakglass_reason = _get_breakglass_reason(args.args)
    if not breakglass_reason:
        gh_pull.create_issue_comment("Error: Breakglass reason is required.")
        return

    db.session.add(
        BreakglassApproval(
            pull_request_id=pr.id,
            github_user_id=gh_user.id,
            repo_id=repo.id,
            account_id=repo.account_id,
            reason=breakglass_reason,
        ),
    )
    db.session.commit()

    AuditLog.capture_github_user_action(
        github_user=gh_user,
        action=AuditLogAction.FLEXREVIEW_BREAKGLASS_SLASH_COMMAND,
        entity=AuditLogEntity.FLEXREVIEW,
        target=str(pr.id),
    )

    notify_breakglass.delay(pr.id, gh_user.id, breakglass_reason)

    gh_pull.create_issue_comment(
        "Breakglass override recorded. One additional approval is required.",
    )


def _get_targets_from_command(full_command: str) -> list[str]:
    words = full_command.split()
    for word in words:
        if word.lower().startswith("--targets="):
            targets = word.split("=")[1]
            return targets.split(",")
    return []


def _get_skip_line_reason(full_command: str) -> str | None:
    flag_index = full_command.find('--skip-line="')
    if flag_index < 0:
        return None
    start = flag_index + len('--skip-line="')
    end = full_command.find('"', start)
    return full_command[start:end]


def _get_breakglass_reason(args: list[str]) -> str | None:
    reason_flag = "--reason="

    for arg in args:
        if arg.startswith(reason_flag):
            reason_value = arg[len(reason_flag) :]

            if reason_value.startswith('"') and reason_value.endswith('"'):
                reason_value = reason_value[1:-1]
            return reason_value

    return None


def _get_pr_from_markdown(current_pr: PullRequest, markdown: str) -> PullRequest | None:
    pr_number = None
    repo_name = current_pr.repo.name
    if "#" in markdown:
        splits = markdown.split("#")
        pr_number = splits[-1]
        repo_name = splits[0].strip()
        if not repo_name:
            repo_name = current_pr.repo.name
    elif markdown.startswith("https"):
        parts = markdown.split("/")
        if len(parts) < 5:
            return None
        pr_number = parts[-1]
        repo_name = parts[-4] + "/" + parts[-3]
    repo = common.get_repo_by_name(repo_name)
    if not repo or not pr_number:
        return None
    pr: PullRequest | None = db.session.scalar(
        sa.select(PullRequest).where(
            PullRequest.repo_id == repo.id,
            PullRequest.number == int(pr_number),
        ),
    )
    return pr


def handle_pr_slash_command(
    repo: GithubRepo,
    pull_number: int,
    command: str,
    gh_user: GithubUser,
) -> None:
    """
    Handle a `/aviator XYZ` command on a PR.

    Users can post `/aviator XYZ` comments on a PR asking the bot to take some
    action (e.g., manual rebase, report the current status, etc.). This function
    triggers the appropriate action for the command.
    """
    structlog.contextvars.bind_contextvars(slash_command=command)
    repo.bind_contextvars()

    _, client = common.get_client(repo)
    gh_pull = client.get_pull(pull_number)
    _, pr = common.ensure_pull_request(repo, gh_pull)
    pr.bind_contextvars()

    commands: dict[str, Callable[[SlashCommandArgs], None]] = {
        "refresh": _handle_status,
        "merge": _handle_merge,
        "stack merge": _handle_stack_merge,
        "block until": _handle_block_until,
        "block cancel": _handle_block_cancel,
        "stack cancel": _handle_stack_cancel,
        "cancel": _handle_cancel,
        "sync": _handle_sync,
        "backport": _handle_backport,
        "flexreview assign": _handle_flexreview_assign,
        "flexreview refresh": _handle_flexreview_refresh,
        "flexreview suggest": _handle_flexreview_suggest,
        "flexreview breakglass": _handle_flexreview_breakglass,
    }

    logger.info("Handling slash command")

    words = shlex.split(command)
    args = SlashCommandArgs(
        full_command=command,
        client=client,
        repo=repo,
        pr=pr,
        gh_pull=gh_pull,
        gh_user=gh_user,
        args=words,
    )
    prefix = words.pop(0)
    try:
        while True:
            if prefix in commands:
                commands[prefix](args)
                return

            if len(words) == 0:
                break
            prefix += " " + words.pop(0)

        client.create_issue_comment(
            pr.number,
            f"Unknown `/aviator` command: ``` {command} ```",
        )
    except SlashCommandHelpExitError as exc:
        gh_pull.create_issue_comment(f"```\n{exc.help_text}```\n")
    except SlashCommandParseError as exc:
        gh_pull.create_issue_comment(
            f"Error while parsing /aviator command: {exc.message}\n\n"
            f"```\n{exc.help_text}```\n",
        )


class NonExitingArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that does not sys.exit on error."""

    @override
    def print_help(self, file: Any = None) -> Never:
        # This happens when '-h' is used.
        raise SlashCommandHelpExitError(self.format_help())

    def error(self, message: str) -> Never:
        raise SlashCommandParseError(message, self.format_help())


class SlashCommandHelpExitError(Exception):
    def __init__(self, help_text: str) -> None:
        self.help_text = help_text


class SlashCommandParseError(Exception):
    def __init__(self, message: str, help_text: str) -> None:
        self.message = message
        self.help_text = help_text
