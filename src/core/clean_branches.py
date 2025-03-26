from __future__ import annotations

import sqlalchemy as sa
import structlog

from core.common import get_client
from core.locks import for_repo
from core.models import BotPr, GithubRepo
from core.rate_limit import get_parsed_gh_graphql_api_limit
from lib.niche_git import NicheGitClient
from main import celery, db

logger = structlog.stdlib.get_logger()


@celery.task
def clean_branches_for_account(account_id: int, max_branches_to_delete: int) -> None:
    logger.info("Starting clean_branches for account", account_id=account_id)
    rate_limit = get_parsed_gh_graphql_api_limit(account_id)
    if rate_limit and rate_limit.remaining < rate_limit.total / 2:
        # If remaining quota is less than a half, do not try to prune the branches. We
        # can do this later.
        logger.info(
            "Not enough quota. Do not clean the branches.",
            account_id=account_id,
            rate_limit_remaining=rate_limit.remaining,
        )
        return

    repos: list[GithubRepo] = db.session.scalars(
        sa.select(GithubRepo).where(
            GithubRepo.account_id == account_id,
            GithubRepo.deleted == False,
            GithubRepo.active == True,
        )
    ).all()
    for repo in repos:
        clean_branches_for_repo.delay(repo.id, max_branches_to_delete)


@celery.task
def clean_branches_for_repo(repo_id: int, max_branches_to_delete: int) -> None:
    logger.info("Starting clean_branches for repo", repo_id=repo_id)
    repo = GithubRepo.get_by_id_x(repo_id)
    repo.bind_contextvars()
    access_token, client = get_client(repo)
    niche_git_client = NicheGitClient.from_repo_and_access_token(repo, access_token)

    with for_repo(repo):
        refs = niche_git_client.ls_refs(["refs/heads/mq-tmp-", "refs/heads/mq-bot-"])
        tmp_branches = [
            ref.removeprefix("refs/heads/")
            for ref in refs
            if ref.startswith("refs/heads/mq-tmp-")
        ]
        bot_branches = [
            ref.removeprefix("refs/heads/")
            for ref in refs
            if ref.startswith("refs/heads/mq-bot-")
        ]
        logger.info(
            "Found branches mq-tmp and mp-bot branches",
            num_tmp_branches=len(tmp_branches),
            num_bot_branches=len(bot_branches),
        )

        bot_prs: list[BotPr] = db.session.scalars(
            sa.select(BotPr).where(
                BotPr.repo_id == repo.id, BotPr.branch_name == sa.func.any(bot_branches)
            )
        ).all()

    # At this point, we get the snapshot of the database and repositories. No need to
    # hold a lock anymore.

    queued_bot_branches = {pr.branch_name for pr in bot_prs if pr.status == "queued"}
    closed_bot_branches = {pr.branch_name for pr in bot_prs if pr.status == "closed"}
    unknown_bot_branches = set(bot_branches) - queued_bot_branches - closed_bot_branches
    if len(unknown_bot_branches) > 0:
        logger.warning(
            "Found mq-bot branches that are not in the database",
            unknown_bot_branches=list(unknown_bot_branches),
        )
    branches_to_delete = []
    branches_to_delete.extend(tmp_branches)
    branches_to_delete.extend(closed_bot_branches)
    branches_to_delete.extend(unknown_bot_branches)

    logger.info("Found branches to delete", num_branches=len(branches_to_delete))
    # NOTE: Technically, we can delete branches with niche-git (implementing that
    # command is easy). This will save the API quota a lot. But for now, let's use the
    # REST API.
    if max_branches_to_delete > 0 and len(branches_to_delete) > max_branches_to_delete:
        logger.info(
            "Too many branches to delete. Deleting 100 and delete the rest in next CRON runs to save API quota."
        )
        branches_to_delete = branches_to_delete[:max_branches_to_delete]

    for branch in branches_to_delete:
        logger.info("Deleting branch", branch=branch)
        client.delete_ref(branch)
