from __future__ import annotations

from core.models import GithubLabel, GithubLabelPurpose, GithubRepo
from main import app, celery, db


@celery.task
def create_default_labels(repo_id: int) -> None:
    # Due to the cyclic import, we need to place this inside a function.
    from core.common import get_client

    repo = GithubRepo.get_by_id_x(repo_id)
    _, client = get_client(repo)

    queue_label: GithubLabel | None = (
        GithubLabel.query.filter_by(repo_id=repo.id)
        .filter(
            GithubLabel.purpose == GithubLabelPurpose.Queue,
        )
        .first()
    )
    if not queue_label:
        db.session.add(
            GithubLabel(
                name=app.config["DEFAULT_READY_LABEL"],
                repo_id=repo.id,
                purpose=GithubLabelPurpose.Queue,
            )
        )
        client.create_label(app.config["DEFAULT_READY_LABEL"], "0E8A16")

    skip_line_label: GithubLabel | None = (
        GithubLabel.query.filter_by(repo_id=repo.id)
        .filter(
            GithubLabel.purpose == GithubLabelPurpose.SkipLine,
        )
        .first()
    )
    if not skip_line_label:
        db.session.add(
            GithubLabel(
                name=app.config["DEFAULT_SKIP_LABEL"],
                repo_id=repo.id,
                purpose=GithubLabelPurpose.SkipLine,
            )
        )
        client.create_label(app.config["DEFAULT_SKIP_LABEL"], "D93F0B")

    blocked_label: GithubLabel | None = (
        GithubLabel.query.filter_by(repo_id=repo.id)
        .filter(
            GithubLabel.purpose == GithubLabelPurpose.Blocked,
        )
        .first()
    )
    if not blocked_label:
        db.session.add(
            GithubLabel(
                name=app.config["DEFAULT_FAILED_LABEL"],
                repo_id=repo.id,
                purpose=GithubLabelPurpose.Blocked,
            )
        )
        client.create_label(app.config["DEFAULT_FAILED_LABEL"], "B60205")

    db.session.commit()
