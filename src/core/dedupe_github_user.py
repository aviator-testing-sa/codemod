from __future__ import annotations

import dataclasses
from collections import defaultdict

import sqlalchemy as sa
import structlog

from auth.models import User
from core.models import (
    ConfigHistory,
    GithubUser,
    MergeOperation,
    PullRequest,
    PullRequestUserSubscription,
    SlackUser,
)
from flexreview.moddb.pull_request_reviewer import PullRequestReviewer
from main import db

logger = structlog.stdlib.get_logger()


@dataclasses.dataclass
class _DuplicatedUserData:
    main_user: GithubUser
    other_users: list[GithubUser]
    aviator_users: list[User]
    pr_reviewers: list[PullRequestReviewer]
    prs: list[PullRequest]
    subscriptions: list[PullRequestUserSubscription]
    mos: list[MergeOperation]
    slack_users: list[SlackUser]
    config_histories: list[ConfigHistory]


def dedupe_github_users(*, account_id: int | None, wetrun: bool) -> None:
    preferred_users = _find_preferred_users(account_id)
    if len(preferred_users) == 0:
        logger.info("No duplicate users found")
        return
    logger.info("Found duplicate users", count=len(preferred_users))
    num_gh_users = 0
    num_prs = 0
    num_aviator_users = 0
    num_pr_reviewers = 0
    num_subscriptions = 0
    num_mos = 0
    num_slack_users = 0
    num_config_histories = 0
    for data in preferred_users:
        num_gh_users += len(data.other_users)
        num_prs += len(data.prs)
        num_aviator_users += len(data.aviator_users)
        num_pr_reviewers += len(data.pr_reviewers)
        num_subscriptions += len(data.subscriptions)
        num_mos += len(data.mos)
        num_slack_users += len(data.slack_users)
        num_config_histories += len(data.config_histories)

    logger.info(
        "Updating the existing databases to fix the foreign keys",
        num_gh_users=num_gh_users,
        num_prs=num_prs,
        num_aviator_users=num_aviator_users,
        num_pr_reviewers=num_pr_reviewers,
        num_subscriptions=num_subscriptions,
        num_mos=num_mos,
        num_slack_users=num_slack_users,
        num_config_histories=num_config_histories,
    )
    for data in preferred_users:
        if wetrun:
            logger.info("Updating the existing reference for user", user=repr(data))
            for user in data.aviator_users:
                user.github_user_id = None
            for rev in data.pr_reviewers:
                rev.github_user_id = data.main_user.id
            for pr in data.prs:
                pr.creator_id = data.main_user.id
            for sub in data.subscriptions:
                db.session.delete(sub)
            for mo in data.mos:
                mo.ready_actor_github_user_id = data.main_user.id
            for slack_user in data.slack_users:
                slack_user.github_user_id = data.main_user.id
            for config_history in data.config_histories:
                config_history.github_author_id = data.main_user.creator_id
            db.session.commit()
            logger.info("Deleting duplicated users", users=repr(data.other_users))
            for user in data.other_users:
                db.session.delete(user)
            db.session.commit()
        else:
            logger.info(
                "Would have updated the following users",
                main_user=data.main_user,
                other_users=data.other_users,
            )


def _find_preferred_users(account_id: int | None) -> list[_DuplicatedUserData]:
    users = _find_duplicate_users(account_id)
    if len(users) == 0:
        return []

    dupes = defaultdict(list)
    for user in users:
        key = (user.account_id, user.username)
        dupes[key].append(user)
    ret = []
    for dupes_list in dupes.values():
        main_user: GithubUser | None = None
        for user in dupes_list:
            if user.gh_database_id:
                main_user = user
                break
        if not main_user:
            logger.info(
                "No GH with GitHub database ID found. Falling back to the latest one",
                users=dupes_list,
            )
            dupes_list.sort(key=lambda x: x.id, reverse=True)
            main_user = dupes_list[0]
        other_users = list(filter(lambda x: x != main_user, dupes_list))
        other_users_ids = [user.id for user in other_users]
        aviator_users = db.session.scalars(
            sa.select(User).filter(User.github_user_id.in_(other_users_ids))
        ).all()
        pr_reviewers = db.session.scalars(
            sa.select(PullRequestReviewer).where(
                PullRequestReviewer.github_user_id.in_(other_users_ids)
            )
        ).all()
        prs = db.session.scalars(
            sa.select(PullRequest).where(PullRequest.creator_id.in_(other_users_ids))
        ).all()
        subscriptions = db.session.scalars(
            sa.select(PullRequestUserSubscription).where(
                PullRequestUserSubscription.github_user_id.in_(other_users_ids)
            )
        ).all()
        # required_repo_users?
        mos = db.session.scalars(
            sa.select(MergeOperation).where(
                MergeOperation.ready_actor_github_user_id.in_(other_users_ids)
            )
        ).all()
        slack_users = db.session.scalars(
            sa.select(SlackUser).where(SlackUser.github_user_id.in_(other_users_ids))
        ).all()
        config_histories = db.session.scalars(
            sa.select(ConfigHistory).where(
                ConfigHistory.github_author_id.in_(other_users_ids)
            )
        ).all()
        ret.append(
            _DuplicatedUserData(
                main_user,
                other_users,
                aviator_users,
                pr_reviewers,
                prs,
                subscriptions,
                mos,
                slack_users,
                config_histories,
            )
        )
    return ret


def _find_duplicate_users(account_id: int | None) -> list[GithubUser]:
    user_table1 = sa.orm.aliased(GithubUser)
    user_table2 = sa.orm.aliased(GithubUser)
    duplicates = (
        sa.select(user_table1.account_id, user_table1.username)
        .group_by(user_table1.account_id, user_table1.username)
        .having(sa.func.count(user_table1.id) > 1)
        .cte("duplicates")
    )
    q = sa.select(user_table2).join(
        duplicates,
        sa.and_(
            user_table2.account_id == duplicates.c.account_id,
            user_table2.username == duplicates.c.username,
        ),
    )
    if account_id is not None:
        q = q.where(user_table2.account_id == account_id)
    return db.session.scalars(q).all()
