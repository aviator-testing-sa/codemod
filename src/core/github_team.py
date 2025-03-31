from __future__ import annotations

from datetime import timedelta

import sqlalchemy as sa
import structlog

from auth.models import AccessToken, Account
from core import common, github_user
from core.graphql import GithubGql
from core.models import (
    GithubRepo,
    GithubTeam,
    GithubTeamMembers,
    GithubTeamSyncStatus,
    GithubUser,
    GithubUserType,
    SyncStatus,
)
from errors import GitHubOrganizationInvalidException
from lib import gh
from lib.gh_graphql import GithubTeamInfo, GithubUserInfo
from main import celery, db
from util import time_util

from . import team_automation_rules

logger = structlog.stdlib.get_logger()


# NOTE: In webhook context, team.id is the github_database_id and
# team.node_id is the github_node_id
def create_or_update_github_team_from_webhook(
    access_token: AccessToken,
    organization: str,
    gh_team: gh.Team,
    fetch_members: bool = False,
) -> GithubTeam:
    if gh_team.parent:
        parent_team: GithubTeam | None = db.session.scalar(
            sa.select(GithubTeam).where(
                GithubTeam.github_database_id == gh_team.parent.id
            )
        )
        if not parent_team:
            logger.warning(
                "Parent team is missing while processing webhook. Try to create and proceed",
                organization=organization,
                team_db_id=gh_team.id,
                parent_db_id=gh_team.parent.id,
            )
            parent_team = create_or_update_github_team_from_webhook(
                access_token=access_token,
                organization=organization,
                gh_team=gh_team.parent,
                fetch_members=True,
            )
    else:
        parent_team = None

    team: GithubTeam | None = db.session.scalar(
        sa.select(GithubTeam).where(
            GithubTeam.github_database_id == gh_team.id,
        )
    )
    if team:
        team.name = gh_team.name
        team.slug = gh_team.slug
        team.github_node_id = gh_team.node_id
        team.parent_id = parent_team.id if parent_team else None
    else:
        team = GithubTeam(
            name=gh_team.name,
            account_id=access_token.account_id,
            organization=organization,
            slug=gh_team.slug,
            github_node_id=gh_team.node_id,
            github_database_id=gh_team.id,
            parent_id=parent_team.id if parent_team else None,
        )
        db.session.add(team)
    db.session.commit()

    logger.info("Updating the team automation rule inheritance", team_id=team.id)
    team_automation_rules.update_automation_rule_mappings_for_team(team)

    if fetch_members:
        update_team_members(
            team, GithubGql(access_token.token, access_token.account_id), None
        )

    return team


def update_team_members(
    team: GithubTeam, gql: GithubGql, gh_team: GithubTeamInfo | None
) -> None:
    logger.info("Updating team members", team_id=team.id, name=team.full_name)

    all_members: set[GithubUser] = set()
    if gh_team and gh_team.members and gh_team.members.nodes:
        for node in gh_team.members.nodes:
            if isinstance(node, GithubUserInfo):
                all_members.add(
                    github_user.ensure(
                        account_id=team.account_id,
                        login=node.login,
                        gh_type=GithubUserType.USER,
                        gh_node_id=node.id,
                        gh_database_id=node.database_id,
                    )
                )
            else:
                raise Exception("GraphQL response not recognized")

    # Load immediate members of the team
    has_next_page: bool = True
    end_cursor: str | None = None
    if gh_team is not None:
        has_next_page = gh_team.members.page_info.has_next_page
        end_cursor = gh_team.members.page_info.end_cursor
        if has_next_page and not end_cursor:
            raise Exception("Bad GraphQL response: Missing endCursor")

    if has_next_page:
        remaining_member_list = gql.fetch_team_members(
            org_name=team.organization,
            team_slug=team.slug,
            after=end_cursor,
        )
        for member in remaining_member_list:
            all_members.add(
                github_user.ensure(
                    account_id=team.account_id,
                    login=member.login,
                    gh_type=GithubUserType.USER,
                    gh_node_id=member.id,
                    gh_database_id=member.database_id,
                )
            )
    logger.info(
        "Fetched immediate members",
        team_id=team.id,
        name=team.full_name,
        count=len(all_members),
    )

    existing_members = set(team.members)
    new_members = all_members - existing_members
    removed_members = existing_members - all_members
    logger.info(
        "Members to add/remove",
        team_id=team.id,
        name=team.full_name,
        add=len(new_members),
        remove=len(removed_members),
    )

    if new_members:
        db.session.execute(
            sa.insert(GithubTeamMembers),
            [
                {
                    "github_team_id": team.id,
                    "github_user_id": user.id,
                }
                for user in new_members
            ],
        )
    if removed_members:
        db.session.execute(
            sa.delete(GithubTeamMembers).where(
                sa.and_(
                    GithubTeamMembers.github_team_id == team.id,
                    GithubTeamMembers.github_user_id.in_([m.id for m in removed_members]),
                )
            ),
            execution_options={"synchronize_session": False},
        )
    db.session.commit()


def _fetch_teams_and_members_from_github(
    account: Account,
    organization: str,
    gql: GithubGql,
    force_refetch: bool = False,
    freshness: timedelta = timedelta(weeks=1),
) -> None:
    # Try to get from cached results
    cache_record: GithubTeamSyncStatus | None = db.session.scalar(
        sa.select(GithubTeamSyncStatus).where(
            GithubTeamSyncStatus.account_id == account.id,
            GithubTeamSyncStatus.organization == organization,
        )
    )

    if cache_record:
        # Skip if this is not a valid organization
        if cache_record.status == SyncStatus.INVALID:
            logger.info("Skipped due to invalid organization")
            return

        # Skip if an organization is being synced
        if cache_record.status in {SyncStatus.LOADING, SyncStatus.NEW}:
            if cache_record.synced_at + timedelta(minutes=5) > time_util.now():
                logger.info("Skipped due to sync in process")
                return
            else:
                logger.info(
                    "Previous status is still NEW/LOADING, it's been a while. Refetching"
                )

        if (
            not force_refetch
            and cache_record.status == SyncStatus.SUCCESS
            and cache_record.synced_at + freshness > time_util.now()
        ):
            logger.info("Skipped due to cached snapshot")
            return

        # Set status to loading or new so other workers won't pick it up
        cache_record.status = SyncStatus.LOADING
    else:
        cache_record = GithubTeamSyncStatus(
            account_id=account.id,
            organization=organization,
            status=SyncStatus.NEW,  # NEW means LOADING + cache does not exist
            synced_at=time_util.now(),
        )
        db.session.add(cache_record)
    db.session.commit()



    try:
        # GitHub database ID to GithubTeamInfo
        github_teams_hash: dict[int, GithubTeamInfo] = gql.fetch_teams_in_org(
            org_name=organization, root_only=False
        )
        logger.info(
            "Fetched teams", organization=organization, count=len(github_teams_hash)
        )
        existing_teams: list[GithubTeam] = db.session.scalars(
            sa.select(GithubTeam).where(
                GithubTeam.account_id == account.id,
                GithubTeam.organization == organization,
            )
        ).all()
        existing_teams_dict: dict[int, GithubTeam] = {
            team.github_database_id: team for team in existing_teams
        }

        logger.info("Syncing teams to DB", organization=organization)
        for database_id, gh_team in github_teams_hash.items():
            if database_id not in existing_teams_dict:
                _, slug = gh_team.combined_slug.split("/")
                logger.info("Adding team", organization=organization, slug=slug)
                team = GithubTeam(
                    name=gh_team.name,
                    account_id=account.id,
                    organization=organization,
                    slug=slug,
                    github_node_id=gh_team.id,
                    github_database_id=database_id,
                )
                db.session.add(team)
                existing_teams_dict[database_id] = team
            else:
                team = existing_teams_dict[database_id]
                if team.name != gh_team.name or team.slug != gh_team.slug:
                    logger.info(
                        "Updating team name",
                        organization=organization,
                        slug=gh_team.slug,
                        old_slug=team.slug,
                    )
                    team.name = gh_team.name
                    team.slug = gh_team.slug
        for team in existing_teams:
            if team.github_database_id not in github_teams_hash:
                logger.info("Removing team", organization=organization, slug=team.slug)
                db.session.delete(team)
        db.session.commit()

        logger.info("Syncing the team hierarchy", organization=organization)
        for database_id, gh_team in github_teams_hash.items():
            team = existing_teams_dict[database_id]
            if gh_team.parent_team:
                parent_team = existing_teams_dict.get(
                    gh_team.parent_team.database_id, None
                )
                if not parent_team:
                    # This shouldn't happen. We will do the best we can.
                    logger.error(
                        "Cannot find parent team in DB",
                        organization=organization,
                        team_db_id=database_id,
                        parent_db_id=gh_team.parent_team.database_id,
                    )
                    team.parent_id = None
                else:
                    team.parent_id = parent_team.id
            else:
                team.parent_id = None
        db.session.commit()

        logger.info("Updating the members of the teams", organization=organization)
        for database_id, gh_team in github_teams_hash.items():
            team = existing_teams_dict[database_id]
            update_team_members(team, gql, gh_team)

        logger.info(
            "Updating the team automation rule inheritance", organization=organization
        )
        for database_id in github_teams_hash:
            team = existing_teams_dict[database_id]
            team_automation_rules.update_automation_rule_mappings_for_team(team)

        cache_record.synced_at = time_util.now()
        cache_record.status = SyncStatus.SUCCESS
    except GitHubOrganizationInvalidException:
        # NOTE (2/1/2024): Only organizations can have teams. Currently we do not have an
        # appropriate data model for organizations. If a repository belongs to an individual
        # user, we will get the user login as the "org_name" and we can't easily differentiate
        # it from real organizations. As a workaround, we will use this to flag these
        # "pseudo-orgs"

        logger.info("Invalid organization. Repo might belong to a user.")
        cache_record.status = SyncStatus.INVALID
    except Exception as exc:
        logger.error(
            "Failed to sync teams and membership",
            account_id=account.id,
            exc_info=exc,
        )

        if cache_record.status == SyncStatus.NEW:
            db.session.delete(cache_record)
        else:
            cache_record.status = SyncStatus.FAILED

    db.session.commit()


@celery.task
def fetch_teams_and_members_for_repo(
    repo_id: int,
    force_refetch: bool = False,
    freshness: timedelta = timedelta(weeks=1),
) -> None:
    repo: GithubRepo = GithubRepo.get_by_id_x(repo_id)
    account = repo.account
    _, github_client = common.get_client(repo)
    _fetch_teams_and_members_from_github(
        account=account,
        organization=repo.org_name,
        gql=github_client.gql_client,
        force_refetch=force_refetch,
        freshness=freshness,
    )




