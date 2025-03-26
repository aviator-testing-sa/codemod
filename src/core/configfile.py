from __future__ import annotations

import itertools

import sqlalchemy as sa
import structlog
from sqlalchemy.orm import Mapped

import basemodel
from core import common, locks, pygithub
from core.models import GithubRepo
from main import celery, db

CONFIG_FILE_PREFIX = ".aviator"

HOOK_READY = ".aviator/mergequeue/ready.js"

_all_config_files = {HOOK_READY}


logger = structlog.stdlib.get_logger()


def _is_config_file(path: str) -> bool:
    return path in _all_config_files


@celery.task
def update_config_files(
    repo_id: int,
    *,
    commit_info: dict,
) -> None:
    """
    Update the config files stored in the database from a commit.

    :param repo_id: The ID of the repository.
    :param commit_info: The commit info from the GitHub API (this match the
        shape of `pygithub.CommitInfo` -- we just can't pass rich types directly
        through Celery directly).
    """
    repo: GithubRepo | None = GithubRepo.get_by_id(repo_id)
    if not repo:
        return
    repo.bind_contextvars()

    commit = pygithub.CommitInfo(**commit_info)
    configs_added = {
        f for f in itertools.chain(commit.added, commit.modified) if _is_config_file(f)
    }
    configs_removed = {f for f in commit.removed if _is_config_file(f)}

    if not configs_added and not configs_removed:
        return

    _, client = common.get_client(repo)
    config_contents = {}
    for config_path in configs_added:
        try:
            contents = client.repo.get_contents(config_path, commit.id)
            assert isinstance(contents, pygithub.ContentFile), (
                f"Expected {config_path} to be a file"
            )
            config_contents[config_path] = contents.decoded_content.decode("utf-8")
        except Exception as exc:
            logger.error(
                "Failed to get config file from commit",
                path=config_path,
                commit_id=commit.id,
                exc_info=exc,
            )
            configs_added.remove(config_path)

    configs_updated = configs_added | configs_removed
    with locks.lock("RepoConfigFile", f"RepoConfigFile/{repo.id}"):
        logger.info(
            "Updating config files",
            configs_added=configs_added,
            configs_removed=configs_removed,
        )
        all_config_models: list[RepoConfigFile] = RepoConfigFile.query.filter(
            RepoConfigFile.repo_id == repo.id,
            RepoConfigFile.path.in_(configs_updated),
        ).all()
        config_models = {m.path: m for m in all_config_models}

        for path in configs_added:
            model: RepoConfigFile | None = config_models.get(path)
            if model is None:
                model = RepoConfigFile(repo_id=repo.id, path=path)
                db.session.add(model)
            model.text = config_contents[path]
            model.commit_id = commit.id
        for path in configs_removed:
            model = config_models.get(path)
            if not model:
                continue
            db.session.delete(model)
        db.session.commit()


class RepoConfigFile(basemodel.BaseModel):
    @staticmethod
    def load(repo: GithubRepo, path: str) -> RepoConfigFile | None:
        rcf: RepoConfigFile | None = RepoConfigFile.query.filter(
            RepoConfigFile.repo_id == repo.id,
            RepoConfigFile.path == path,
        ).one_or_none()
        return rcf

    __tablename__ = "repo_config_file"
    __table_args__ = (sa.UniqueConstraint("repo_id", "path"),)

    repo_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_repo.id"),
        nullable=False,
        doc="The ID of the github_repo that this config file belongs to.",
    )
    path: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
        doc="The path of the config file (as it exists in the actual Git repository).",
    )
    text: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
        doc="The textual content of the config file.",
    )
    commit_id: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
        doc="The commit ID of the commit that last modified this config file.",
    )

    # We don't do soft deletes for this table. We can always add this back in
    # later if we change that though.
    deleted = None  # type: ignore[assignment]

    repo: Mapped[GithubRepo] = sa.orm.relationship("GithubRepo", uselist=False)
