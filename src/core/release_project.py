import re

import structlog

import atc.non_released_prs
import errors
import flexreview.celery_tasks
from core.models import (
    GithubRepo,
    PreviousReleaseAssociation,
    Release,
    ReleaseCut,
    ReleaseIncludedPullRequest,
    ReleaseProject,
    ReleaseProjectRepoConfig,
)
from core.release_project_config import ReleaseProjectConfig, ReleaseVersionSchema
from main import db
from util import time_util

logger = structlog.stdlib.get_logger()


def setup_repo(
    rp: ReleaseProject, repo: GithubRepo, delete_releases: bool = False
) -> ReleaseProjectRepoConfig:
    rp_repo_config: ReleaseProjectRepoConfig | None = (
        ReleaseProjectRepoConfig.query.filter(
            ReleaseProjectRepoConfig.release_project_id == rp.id,
            ReleaseProjectRepoConfig.repo_id == repo.id,
        ).first()
    )
    if not rp_repo_config:
        rp_repo_config = ReleaseProjectRepoConfig(
            release_project_id=rp.id,
            repo_id=repo.id,
        )
        db.session.add(rp_repo_config)
        db.session.commit()

    cuts: list[ReleaseCut] = (
        ReleaseCut.query.join(Release, Release.id == ReleaseCut.release_id)
        .filter(Release.release_project_id == rp.id)
        .all()
    )

    if len(cuts) > 0:
        if not delete_releases:
            logger.info(
                "Found existing releases. Repo setup was unsuccessful.",
                release_project_id=rp.id,
                repo_id=repo.id,
            )
            raise errors.ReleaseAlreadyExistsException("Found existing releases.")
        else:
            logger.info(
                "Deleting all existing releases.",
                release_project_id=rp.id,
                repo_id=repo.id,
            )
            ReleaseIncludedPullRequest.query.filter(
                ReleaseIncludedPullRequest.release_cut_id.in_([cut.id for cut in cuts])
            ).delete()
            for cut in cuts:
                db.session.delete(cut)
            db.session.commit()

            releases: list[Release] = Release.query.filter(
                Release.release_project_id == rp.id
            ).all()
            PreviousReleaseAssociation.query.filter(
                PreviousReleaseAssociation.release_id.in_(
                    [release.id for release in releases]
                )
            ).delete()
            PreviousReleaseAssociation.query.filter(
                PreviousReleaseAssociation.previous_release_id.in_(
                    [release.id for release in releases]
                )
            ).delete()
            for release in releases:
                db.session.delete(release)
            db.session.commit()

    return rp_repo_config


def create_project_data(
    project_name: str,
    repo: GithubRepo,
    git_tag_patterns: list[str],
    release_version_schema: ReleaseVersionSchema | None,
    require_verification: bool,
    require_verification_for_labeled_pr: bool,
    read_only_dashboard: bool = False,
    overwrite: bool = False,
    delete_releases: bool = False,
    enable_scheduled_rlease_cut: bool = False,
    scheduled_release_cut_cron: str = "",
) -> tuple[ReleaseProject, ReleaseProjectRepoConfig]:
    # TODO: Support display name for project
    project_name = re.sub(r"\s+", "-", project_name.strip())
    if not repo.flexreview_active:
        repo.flexreview_active = True
        repo.flexreview_merged_pull_backfill_next_node = ""
        repo.flexreview_backfill_started_at = time_util.now()
        db.session.commit()
        flexreview.celery_tasks.backfill_merged_pulls.delay(repo.id)

    rp: ReleaseProject | None = ReleaseProject.query.filter(
        ReleaseProject.account_id == repo.account_id,
        ReleaseProject.name == project_name,
        ReleaseProject.deleted == False,
    ).first()

    if not rp:
        rp = ReleaseProject(
            account_id=repo.account_id,
            name=project_name,
            enable_scheduled_release_cut=enable_scheduled_rlease_cut,
            scheduled_release_cut_cron=scheduled_release_cut_cron,
            config=ReleaseProjectConfig(
                release_version_schema=release_version_schema,
                require_verification=require_verification,
                require_verification_for_labeled_pr=require_verification_for_labeled_pr,
                read_only_dashboard=read_only_dashboard,
            ),
        )
        db.session.add(rp)
    elif not overwrite:
        logger.info(
            "Found an existing release project. No project is created",
            release_project_id=rp.id,
        )
        raise errors.ReleaseProjectAlreadyExistsException(
            "Found an existing release project"
        )
    rp.release_git_tag_patterns = git_tag_patterns
    db.session.commit()

    rp_repo_config = setup_repo(rp, repo, delete_releases)
    logger.info(
        "Created release project. Release backfill will be run asynchronously.",
        release_project_id=rp.id,
        repo_id=repo.id,
    )
    atc.non_released_prs.update_non_released_prs.delay(repo.id)

    return rp, rp_repo_config
