from __future__ import annotations

from enum import IntEnum

import typing_extensions as TE


class StatusCode(IntEnum):
    UNKNOWN = 0

    # open
    NOT_QUEUED = 1

    # queued
    QUEUED = 2
    PENDING_TEST = 3
    STUCK_ON_STATUS = 4
    PENDING_MERGEABILITY = 5
    IN_DRAFT = 6
    REQUEUED_BY_BISECTION = 7
    MISSING_AFFECTED_TARGETS = 8
    PENDING_STACK_READY = 9
    THROTTLED_BY_MAX_CAP = 10

    # blocked
    BLOCKED_UNKNOWN = 100
    NOT_APPROVED = 101
    LOW_APPROVAL_COUNT = 102
    REQUIRED_APPROVER_MISSING = 103
    QUEUE_LABEL_REMOVED = 104
    BLOCKED_BY_LABEL = 105
    MERGE_CONFLICT = 106
    FAILED_TESTS = 107
    FAILED_UNKNOWN = 108
    MISSING_OWNER_REVIEW = 109
    BLOCKED_BY_GITHUB = 110
    CI_TIMEDOUT = 111
    STUCK_TIMEDOUT = 112
    UNRESOLVED_CONVERSATIONS = 113
    DRAFT_PRS_UNSUPPORTED = 114
    # The PR is blocked because the repository configuration requires all
    # commits to be signed/verified, but at least one commit in the PR doesn't
    # have a signature.
    UNVERIFIED_COMMITS = 115
    # The PR is blocked because a new commit is added to a tagged PR.
    COMMIT_ADDED = 116
    CHANGES_REQUESTED = 117
    # The PR is blocked because task list is not completed.
    UNSUPPORTED_BASE_BRANCH = 118
    CUSTOM_VALIDATIONS_FAILED = 119
    REBASE_FAILED = 120
    GITHUB_API_ERROR = 121
    # The PR is blocked because the stack top failed
    BLOCKED_BY_STACK_TOP = 122
    # hook supports
    BLOCKED_BY_READY_HOOK = 123
    # This PR cannot be merged as its changes are already covered by the base
    # branch or another queued PR.
    BLOCKED_BY_EMPTY_DRAFT_PR = 124
    PENDING_PRECONDITION_PRS = 125
    BATCH_BISECTION_ATTEMPT_EXCEEDED = 126
    BLOCKED_BY_GITLAB = 127

    # closed
    MERGED_BY_MQ = 200
    MERGED_MANUALLY = 201
    PR_CLOSED_MANUALLY = 203

    # reset
    MANUAL_RESET = 300
    RESET_BY_FAILURE = 301
    RESET_BY_SKIP = 302
    RESET_BY_DEQUEUE = 303
    RESET_BY_PR_MANUALLY_CLOSED = 304
    RESET_BY_FF_FAILURE = 305
    RESET_BY_EMERGENCY_MERGE = 306
    RESET_BY_CONFIG_CHANGE = 307
    RESET_BY_TIMEOUT = 308
    RESET_BY_INVALID_PR_STATE = 309
    RESET_BY_COMMIT_ADDED = 310
    ###
    # READ THIS: If you add a new reset status code, you must also add it to the
    # analytics page. See PR #1745 for example. https://github.com/aviator-co/mergeit/pull/1745.
    ###

    @classmethod
    def reset_status_codes(cls) -> list[StatusCode]:
        return [
            StatusCode.MANUAL_RESET,
            StatusCode.RESET_BY_FAILURE,
            StatusCode.RESET_BY_SKIP,
            StatusCode.RESET_BY_DEQUEUE,
            StatusCode.RESET_BY_PR_MANUALLY_CLOSED,
            StatusCode.RESET_BY_FF_FAILURE,
            StatusCode.RESET_BY_EMERGENCY_MERGE,
            StatusCode.RESET_BY_CONFIG_CHANGE,
            StatusCode.RESET_BY_TIMEOUT,
            StatusCode.RESET_BY_INVALID_PR_STATE,
            StatusCode.RESET_BY_COMMIT_ADDED,
        ]

    def message(self) -> str:
        """
        A user facing description of the status code.
        """
        if self is StatusCode.UNKNOWN:
            return "unknown"
        elif self is StatusCode.NOT_QUEUED:
            return "not queued"
        elif self is StatusCode.QUEUED:
            return "queued"
        elif self is StatusCode.STUCK_ON_STATUS:
            return "stuck waiting for CI status"
        elif self is StatusCode.PENDING_TEST:
            return "waiting for CI to complete"
        elif self is StatusCode.PENDING_MERGEABILITY:
            return "waiting for PR to be mergeable before queuing"
        elif self is StatusCode.IN_DRAFT:
            return "this PR is in draft state"
        elif self is StatusCode.REQUEUED_BY_BISECTION:
            return "this PR was re-queued after the associated batch was bisected"
        elif self is StatusCode.MISSING_AFFECTED_TARGETS:
            return "PR is missing affected targets"
        elif self is StatusCode.PENDING_STACK_READY:
            return "this PR is waiting on the rest of the PRs in the stack to be ready before queuing"
        elif self is StatusCode.THROTTLED_BY_MAX_CAP:
            return "PR is throttled by maximum number of parallel builds"

        elif self is StatusCode.BLOCKED_UNKNOWN:
            return "failed due to unknown reason"
        elif self is StatusCode.NOT_APPROVED:
            return "this PR has not been approved"
        elif self is StatusCode.LOW_APPROVAL_COUNT:
            return "approval count requirement not met"
        elif self is StatusCode.REQUIRED_APPROVER_MISSING:
            return "missing required approver(s)"
        elif self is StatusCode.QUEUE_LABEL_REMOVED:
            return "queue label was manually removed"
        elif self is StatusCode.BLOCKED_BY_LABEL:
            return "PR has a blocked label, remove to re-queue"
        elif self is StatusCode.BLOCKED_BY_READY_HOOK:
            return "PR is blocked by the ready hook"
        elif self is StatusCode.BLOCKED_BY_EMPTY_DRAFT_PR:
            return (
                "PR cannot be merged as its changes are already covered by "
                "the base branch or another queued PR."
            )
        elif self is StatusCode.PENDING_PRECONDITION_PRS:
            return "PR is waiting on other PRs to be merged before queuing"
        elif self is StatusCode.BATCH_BISECTION_ATTEMPT_EXCEEDED:
            return "bisection attempt exceeded, PR is blocked"
        elif self is StatusCode.MERGE_CONFLICT:
            return "merge conflict detected, please resolve manually and requeue"
        elif self is StatusCode.FAILED_TESTS:
            return "some required checks failed"
        elif self is StatusCode.FAILED_UNKNOWN:
            return "failed due to unknown reason"
        elif self is StatusCode.MISSING_OWNER_REVIEW:
            return "missing codeowner approval"
        elif self is StatusCode.BLOCKED_BY_GITHUB:
            return "blocked by Github, possibly missing approvals or merge cannot be cleanly created"
        elif self is StatusCode.CI_TIMEDOUT:
            return "CI timed out"
        elif self is StatusCode.STUCK_TIMEDOUT:
            return "PR is stuck, the CI timed out"
        elif self is StatusCode.UNRESOLVED_CONVERSATIONS:
            return "this PR has conversations which must be resolved before merging"
        elif self is StatusCode.DRAFT_PRS_UNSUPPORTED:
            return "draft PRs are not supported in this repository"
        elif self is StatusCode.UNVERIFIED_COMMITS:
            return (
                "this PR contains unverified commits which is not allowed by "
                "the repository configuration "
                "(see https://docs.aviator.co/faqs/mergequeue)"
            )
        elif self is StatusCode.COMMIT_ADDED:
            return "new commit introduced for a queued PR, invalidating the status"
        elif self is StatusCode.CUSTOM_VALIDATIONS_FAILED:
            return "some custom validations failed for this PR"
        elif self is StatusCode.CHANGES_REQUESTED:
            return (
                "this PR has a review with changes requested "
                "(the review must be approved or dismissed before merging)"
            )
        elif self is StatusCode.UNSUPPORTED_BASE_BRANCH:
            return "this PR is against an unsupported base branch"
        elif self is StatusCode.REBASE_FAILED:
            return (
                "PR cannot be automatically rebased, please rebase manually to continue"
            )
        elif self is StatusCode.GITHUB_API_ERROR:
            return "Something went wrong when communicating with GitHub"
        elif self is StatusCode.BLOCKED_BY_STACK_TOP:
            return "top queued PR in the stack failed to merge"
        elif self is StatusCode.BLOCKED_BY_GITLAB:
            return "blocked by GitLab, possibly missing approvals or merge cannot be cleanly created"

        elif self is StatusCode.MERGED_BY_MQ:
            return "merged by Aviator bot"
        elif self is StatusCode.MERGED_MANUALLY:
            return "merged manually"
        elif self is StatusCode.PR_CLOSED_MANUALLY:
            return "PR closed manually"

        elif self is StatusCode.MANUAL_RESET:
            return "reset triggered manually"
        elif self is StatusCode.RESET_BY_FAILURE:
            return "reset triggered due to failure"
        elif self is StatusCode.RESET_BY_SKIP:
            return "reset triggered by use of skip label"
        elif self is StatusCode.RESET_BY_DEQUEUE:
            return "reset triggered due to dequeue"
        elif self is StatusCode.RESET_BY_PR_MANUALLY_CLOSED:
            return "reset triggered by manually closed draft PR"
        elif self is StatusCode.RESET_BY_FF_FAILURE:
            return "reset triggered by fast forwarding failure"
        elif self is StatusCode.RESET_BY_EMERGENCY_MERGE:
            return "reset triggered by emergency merge request"
        elif self is StatusCode.RESET_BY_CONFIG_CHANGE:
            return "reset triggered by change in configuration"
        elif self is StatusCode.RESET_BY_TIMEOUT:
            return "reset triggered by timed out PR"
        elif self is StatusCode.RESET_BY_INVALID_PR_STATE:
            return "reset triggered since the queued PR was identified as not ready to merge"
        elif self is StatusCode.RESET_BY_COMMIT_ADDED:
            return "reset triggered by addition of a new commit in a queued PR"
        else:
            TE.assert_never(self)

    def __eq__(self, other: object) -> bool:
        """
        Custom == function that allows enum objects to be compared with values.

        This means that ``StatusCode.NOT_APPROVED == 101`` will return True.
        """
        try:
            return self.value == StatusCode(other).value  # type: ignore[arg-type]
        except ValueError:
            return False

    @staticmethod
    def get_reset_reason(status_code: StatusCode) -> StatusCode:
        """
        Given a status code of PR / BotPR failure, generate a status code for reset reason.
        """
        if status_code.value >= 300:
            return status_code
        if status_code == StatusCode.CI_TIMEDOUT:
            return StatusCode.RESET_BY_TIMEOUT
        if status_code == StatusCode.BLOCKED_BY_LABEL:
            return StatusCode.RESET_BY_DEQUEUE
        if status_code == StatusCode.COMMIT_ADDED:
            return StatusCode.RESET_BY_COMMIT_ADDED
        if status_code in (
            StatusCode.UNRESOLVED_CONVERSATIONS,
            StatusCode.MISSING_OWNER_REVIEW,
            StatusCode.CHANGES_REQUESTED,
            StatusCode.NOT_APPROVED,
            StatusCode.MISSING_AFFECTED_TARGETS,
        ):
            return StatusCode.RESET_BY_INVALID_PR_STATE
        return StatusCode.RESET_BY_FAILURE
