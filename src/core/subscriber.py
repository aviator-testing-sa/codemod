from __future__ import annotations

import sqlalchemy as sa
import sqlalchemy.dialects.postgresql as sa_pg
import structlog

from core import models
from main import db

logger = structlog.stdlib.get_logger()


def ensure_pull_request_subscriber(
    pull_request_id: int,
    github_user_id: int,
) -> models.PullRequestUserSubscription:
    """
    For a given pull_request and github_user, ensure a subscription exists.
    On creation, we assume the user is a reviewer we have not seen before.
    """
    sub: models.PullRequestUserSubscription = db.session.scalar(
        sa.select(models.PullRequestUserSubscription)
        .from_statement(
            sa_pg.insert(models.PullRequestUserSubscription)
            .values(
                pull_request_id=pull_request_id,
                github_user_id=github_user_id,
                has_attention=True,
                is_reviewer=True,
                attention_reason=models.AttentionReason.REVIEW_REQUESTED,
            )
            .on_conflict_do_update(
                index_elements=["github_user_id", "pull_request_id"],
                # Do a no-op update here since `on_conflict_do_nothing` won't
                # return the row if it already exists.
                set_={
                    "github_user_id": github_user_id,
                    "pull_request_id": pull_request_id,
                },
            )
            .returning(models.PullRequestUserSubscription)
        )
        .execution_options(populate_existing=True)
    )
    assert isinstance(sub, models.PullRequestUserSubscription), (
        f"Expected PullRequestUserSubscription, got {sub}"
    )
    db.session.commit()
    return sub


def ensure_pull_request_owner_subscription(
    pull_request_id: int,
    author_user_id: int,
) -> models.PullRequestUserSubscription:
    sub: models.PullRequestUserSubscription = db.session.scalar(
        sa.select(models.PullRequestUserSubscription)
        .from_statement(
            sa_pg.insert(models.PullRequestUserSubscription)
            .values(
                pull_request_id=pull_request_id,
                github_user_id=author_user_id,
                has_attention=False,
                is_reviewer=False,
                attention_reason=models.AttentionReason.REVIEW_REQUESTED,
            )
            # No-op update here to prevent clobbering existing values.
            # We can't use `on_conflict_do_nothing` here because it won't
            # return the row if it already exists.
            .on_conflict_do_update(
                index_elements=["github_user_id", "pull_request_id"],
                set_={
                    "github_user_id": author_user_id,
                    "pull_request_id": pull_request_id,
                },
            )
            .returning(models.PullRequestUserSubscription)
        )
        .execution_options(populate_existing=True)
    )
    assert isinstance(sub, models.PullRequestUserSubscription), (
        f"Expected PullRequestUserSubscription, got {sub}"
    )
    db.session.commit()
    return sub
