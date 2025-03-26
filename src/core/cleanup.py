from __future__ import annotations

import datetime

import structlog

from core.models import (
    GithubTestStatus,
)
from main import app, celery, db
from util import time_util

logger = structlog.stdlib.get_logger()


@celery.task
def clean_up_old_data(*, lookback_days: int, delete: bool) -> None:
    clean_up_gh_test_status(lookback_days=lookback_days, delete=delete)


def clean_up_gh_test_status(*, lookback_days: int, delete: bool) -> None:
    lookback_time = time_util.now() - datetime.timedelta(days=lookback_days)

    start = time_util.now()
    if delete:
        deleted_count = GithubTestStatus.query.filter(
            GithubTestStatus.created < lookback_time
        ).delete()
        db.session.commit()
    else:
        deleted_count = GithubTestStatus.query.filter(
            GithubTestStatus.created < lookback_time
        ).count()
    duration = time_util.now() - start

    logger.info(
        "Cleanup job: deleted %d GithubTestStatuses in %d seconds (delete=%s)",
        deleted_count,
        duration.total_seconds(),
        delete,
    )
