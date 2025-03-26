from __future__ import annotations

import datetime
import statistics
from decimal import Decimal

import sqlalchemy as sa
import structlog

from auth.models import Account
from core.models import GithubRepo, QueueTimeStat
from main import celery, db
from util import time_util

logger = structlog.get_logger()


@celery.task
def calculate_queue_eta() -> None:
    iso_weekdate = time_util.iso_weekdate()
    current_week = time_util.iso_current_week()
    current_year = time_util.iso_current_year()
    begin_inclusive = datetime.date.fromisocalendar(current_year, current_week, 1)
    end_inclusive = datetime.date.fromisocalendar(current_year, current_week, 7)

    repos: list[GithubRepo] = (
        GithubRepo.query.filter_by(active=True)
        .join(Account)
        .filter(Account.billing_active)
        .all()
    )

    for repo in repos:
        median_seconds = calculate_queue_eta_for_repo(
            repo.id, str(begin_inclusive), str(end_inclusive)
        )
        if median_seconds:
            stat = QueueTimeStat(
                repo_id=repo.id,
                weekdate=iso_weekdate,
                queue_time_median_seconds=Decimal(median_seconds),
            )
            db.session.add(stat)
    db.session.commit()
    return


def calculate_queue_eta_for_repo(
    repo_id: int,
    begin_inclusive: str,
    end_inclusive: str,
) -> float | None:
    query = """
    select merged_at, queued_at
    from pull_request
    where repo_id = :repo_id
    and merged_at >= (:merged_at_begin)::timestamp
    and merged_at <= (:merged_at_end)::timestamp
    and queued_at >= (:queued_at_begin)::timestamp
    and queued_at <= (:queued_at_end)::timestamp
    """

    result = db.session.execute(
        sa.text(query),
        {
            "repo_id": repo_id,
            "merged_at_begin": begin_inclusive,
            "merged_at_end": end_inclusive,
            "queued_at_begin": begin_inclusive,
            "queued_at_end": end_inclusive,
        },
    )

    if not result:
        return None

    result_list = [(row[0] - row[1]).total_seconds() for row in result]
    if not result_list:
        return None
    median_seconds = statistics.median(result_list)
    return median_seconds
