from __future__ import annotations

import datetime
import enum

import structlog

import core.locks
from core.models import (
    AttentionReason,
    GithubUser,
    PullRequest,
    PullRequestUserSubscription,
)
from main import db

logger = structlog.stdlib.get_logger()


class AttentionTrigger(enum.Enum):
    """Triggers of the attention changes.

    There are two users involved in the trigger:

    * The actor: the user who made the action.
    * The target: the user who is the target of the action. For example, if User A
      removes the approval from User B, User A is the actor and User B is the target.
      The target present only in certain triggers.
    """

    # * The actor loses the attention.
    # * If the actor is a reviewer, the PR author gets the attention.
    # * If the actor is the PR author, the non-approved reviewers get the attention.
    ADDED_COMMENT = enum.auto()

    # * The actor loses the attention.
    # * The PR author gets the attention.
    APPROVED = enum.auto()

    # * The target gets the attention.
    # * The actor loses the attention.
    REVIEW_REQUESTED = enum.auto()

    # * The target loses the attention.
    REVIEW_REQUEST_REMOVED = enum.auto()

    # * The target gets the attention.
    # * If the actor is the PR author, the PR author loses the attention.
    REVIEW_DISMISSED = enum.auto()

    # * The PR author gets the attention.
    TEST_FAILURE = enum.auto()

    # * The PR author gets the attention.
    PR_BLOCKED = enum.auto()

    # * The target gets the attention.
    MANUAL_SET = enum.auto()

    # * The target loses the attention.
    MANUAL_UNSET = enum.auto()


def transition_attention(
    pr: PullRequest,
    subs: list[PullRequestUserSubscription],
    trigger: AttentionTrigger,
    actor: GithubUser | None,
    target: GithubUser | None,
) -> None:
    """Transition the attention based on the trigger.

    This doesn't send a notification. The caller should send a notification to users who
    were flipped. The caller should decide on what notification to send in some cases.
    For example, if a PR gets approved, the PR author will get an attention, but instead
    of sending a notification for the flipped attention, it should send a notification
    for PR approval.

    Args:
      actor: The user who made the action.
      target: The user who is the target of the action.
    """
    with core.locks.lock("pr-subs", f"pr-subs-{pr.id}"):
        author_sub = None
        reviewer_subs: dict[int, PullRequestUserSubscription] = {}
        for sub in subs:
            if sub.github_user_id == pr.creator_id:
                author_sub = sub
            else:
                reviewer_subs[sub.github_user_id] = sub
        if author_sub is None:
            author_sub = PullRequestUserSubscription(
                pull_request_id=pr.id,
                github_user_id=pr.creator_id,
                has_attention=False,
                is_reviewer=False,
                attention_reason=AttentionReason.FALLBACK,
            )
            db.session.add(author_sub)

        actor_sub = None
        if actor is not None:
            if actor.id in reviewer_subs:
                actor_sub = reviewer_subs[actor.id]
            elif actor.id == pr.creator_id:
                actor_sub = author_sub
        target_sub = None
        if target is not None:
            if target.id in reviewer_subs:
                target_sub = reviewer_subs[target.id]
            elif target.id == pr.creator_id:
                target_sub = author_sub

        match trigger:
            case AttentionTrigger.ADDED_COMMENT:
                if actor_sub is not None:
                    _set_attention(actor_sub, False, AttentionReason.REVIEW_COMMENT)
                    if actor_sub.is_reviewer:
                        _set_attention(author_sub, True, AttentionReason.REVIEW_COMMENT)
                    else:
                        for sub in reviewer_subs.values():
                            if sub.approved_at is None:
                                _set_attention(
                                    sub, True, AttentionReason.REVIEW_COMMENT
                                )
            case AttentionTrigger.APPROVED:
                if actor_sub is not None:
                    _set_attention(actor_sub, False, AttentionReason.APPROVED)
                    _set_attention(author_sub, True, AttentionReason.APPROVED)
            case AttentionTrigger.REVIEW_REQUESTED:
                if actor_sub is not None:
                    _set_attention(actor_sub, False, AttentionReason.REVIEW_REQUESTED)
                if target_sub is not None:
                    _set_attention(target_sub, True, AttentionReason.REVIEW_REQUESTED)
            case AttentionTrigger.REVIEW_REQUEST_REMOVED:
                if target_sub is not None:
                    _set_attention(target_sub, False, AttentionReason.REVIEW_REQUESTED)
            case AttentionTrigger.REVIEW_DISMISSED:
                if target_sub is not None:
                    _set_attention(target_sub, True, AttentionReason.REVIEW_DISMISSED)
                    if actor_sub and not actor_sub.is_reviewer:
                        _set_attention(
                            author_sub,
                            False,
                            AttentionReason.REVIEW_DISMISSED,
                        )
            case AttentionTrigger.TEST_FAILURE:
                _set_attention(author_sub, True, AttentionReason.TEST_FAILURE)
            case AttentionTrigger.PR_BLOCKED:
                _set_attention(author_sub, True, AttentionReason.PULL_REQUEST_BLOCKED)
            case AttentionTrigger.MANUAL_SET:
                if target_sub is not None:
                    _set_attention(target_sub, True, AttentionReason.MANUAL)
            case AttentionTrigger.MANUAL_UNSET:
                if target_sub is not None:
                    _set_attention(target_sub, False, AttentionReason.MANUAL)

        # Check if one of the reviewers have an attention.
        if not any(sub.has_attention for sub in reviewer_subs.values()):
            # Nobody has an attention. If the actor is the author, set the attention to
            # non-approved reviewers. Otherwise, give the attention to the author.
            if actor_sub and actor_sub.github_user_id == pr.creator_id:
                for sub in reviewer_subs.values():
                    if sub.approved_at is None:
                        _set_attention(sub, True, AttentionReason.FALLBACK)
                # If everybody is approved, give the attention to the author.
                if not any(sub.has_attention for sub in reviewer_subs.values()):
                    _set_attention(author_sub, True, AttentionReason.FALLBACK)
            else:
                _set_attention(author_sub, True, AttentionReason.FALLBACK)

        if actor_sub:
            actor_sub.last_interaction_at = datetime.datetime.now(datetime.UTC)

        db.session.commit()


def _set_attention(
    sub: PullRequestUserSubscription,
    should_have_attention: bool,
    reason: AttentionReason,
) -> None:
    if sub.has_attention == should_have_attention:
        return
    sub.has_attention = should_have_attention
    sub.attention_reason = reason
    sub.attention_bit_flipped_at = datetime.datetime.now(datetime.UTC)
