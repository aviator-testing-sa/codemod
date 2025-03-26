from __future__ import annotations

import datetime
from typing import cast

import requests
import sqlalchemy as sa
import structlog

import errors
import schema
from auth.models import Account
from billing.controller import get_subscription
from core import common, connect, gh_rest, github_user
from core.graphql import GithubGql
from core.models import (
    BaseBranch,
    GithubRepo,
    GithubTest,
    GithubUser,
    PullRequest,
    RepoCollaborator,
)
from main import app, celery, db

logger = structlog.stdlib.get_logger()
MAX_COLLABORATOR_PAGES = 10


@celery.task
def analyze_repo_async(repo_id: int) -> None:
    repo = GithubRepo.get_by_id_x(repo_id)
    analyze_repo(repo)


@celery.task
def analyze_repo_name_async(repo_name: str) -> None:
    repo: GithubRepo | None = common.get_repo_by_name(repo_name)
    if not repo:
        return
    analyze_repo(repo)


@celery.task
def analyze_first_pull(repo_id: int, pr_number: int | None = None) -> None:
    repo = GithubRepo.get_by_id_x(repo_id)
    repo.bind_contextvars()
    if not repo.account.billing_active:
        return

    logger.info("Analyzing the PR for repo", pr_number=pr_number)

    try:
        access_token = common.get_access_token(repo)
        if not access_token:
            return
    except errors.AccessTokenException as e:
        logger.info("Unable to fetch access token")
        return

    # first read from protected branches, so we make sure we covered everything
    try:
        analyze_repo(repo)
    except Exception as e:
        logger.info("Failed to analyze a repository", exc_info=e)
    analyze_tests_in_pr(repo, pr_number)


def analyze_tests_in_pr(repo: GithubRepo, pr_number: int | None) -> None:
    access_token, client = common.get_client(repo)

    sha: str | None = None
    if pr_number:
        pull = client.get_pull(pr_number)
        if pull:
            sha = pull.head.ref
    if not sha:
        gql = GithubGql(access_token.token, repo.account_id)
        pr = gql.get_latest_merged_pull_request(
            owner=repo.org_name, name=repo.repo_name
        )
        if not pr:
            logger.info("Repo has no closed PRs")
            return
        sha = pr.head_ref_oid

    new_tests = 0
    if sha:
        logger.info("Found sha to analyze", sha=sha)
        tests = []
        test_list = set(client.get_test_list(sha))
        for test_name in test_list:
            if common.is_av_custom_check(test_name):
                # Ignore the Aviator check run.
                continue
            test: GithubTest | None = GithubTest.query.filter_by(
                name=test_name, repo_id=repo.id
            ).first()
            if not test:
                test = GithubTest(name=test_name, repo_id=repo.id, ignore=True)
                new_tests += 1
                db.session.add(test)
            tests.append(test)
        db.session.commit()
        # end after first pull
        logger.info("Found new tests in repo", num_new_tests=new_tests)


def analyze_repo(repo: GithubRepo) -> None:
    _analyze_repo_required_tests(repo)
    _analyze_repo_base_branches(repo)
    db.session.commit()


def _analyze_repo_required_tests(repo: GithubRepo) -> None:
    required_tests = set(connect.analyze_tests(repo))
    for test_name in required_tests:
        if common.is_av_custom_check(test_name):
            # Ignore the Aviator check run.
            continue
        test = GithubTest.query.filter_by(name=test_name, repo_id=repo.id).first()
        if not test:
            logger.info("Found missing required test", test_name=test_name)
            test = GithubTest(
                name=test_name, repo_id=repo.id, ignore=False, github_required=True
            )
            db.session.add(test)
        elif not test.github_required:
            logger.info("Required check changed detected", test_name=test_name)
            test.ignore = False
            test.github_required = True
    # Find any other tests that are not required by Github, and mark them so.
    existing_tests = GithubTest.query.filter_by(
        repo_id=repo.id, github_required=True
    ).all()
    for et in existing_tests:
        if et.name not in required_tests:
            logger.info("Flagging existing test as not required", test_name=et.name)
            et.github_required = False


def _analyze_repo_base_branches(repo: GithubRepo) -> None:
    """
    Set a default value for base_branch_patterns.
    """
    _, client = common.get_client(repo)
    repo.head_branch = client.repo.default_branch
    if repo.base_branch_patterns:
        # Don't override if it's already been set.
        return
    branch = BaseBranch(
        repo_id=repo.id,
        name=client.repo.default_branch,
    )
    db.session.add(branch)


@celery.task
def remove_inactive_collaborators_for_account(account_id: int) -> None:
    if app.config["ACTIVE_COLLABORATOR_BILLING_LIMIT_DAYS"] > 0:
        remove_inactive_collaborators_for_account_onprem(
            account_id, app.config["ACTIVE_COLLABORATOR_BILLING_LIMIT_DAYS"]
        )
    else:
        remove_inactive_collaborators_for_account_cloud(account_id)


def remove_inactive_collaborators_for_account_onprem(
    account_id: int, days: int
) -> None:
    logger.info(
        "Starting remove_inactive_collaborators for account",
        account_id=account_id,
    )
    account = Account.get_by_id_x(account_id)
    try:
        logger.info(
            "Querying users with PRs created in the last N days",
            days=days,
        )
        q = (
            sa.select(
                GithubUser,
                sa.func.sum(
                    sa.case(
                        [
                            (
                                PullRequest.created
                                >= sa.func.now() - datetime.timedelta(days=days),
                                1,
                            )
                        ],
                        else_=0,
                    ),
                ),
            )
            # With outer join, even if a GithubUser have never created a PR, the
            # result contains that user. In that case, the PR count becomes 0.
            .join(PullRequest, GithubUser.id == PullRequest.creator_id, isouter=True)
            .join(GithubRepo, PullRequest.repo_id == GithubRepo.id)
            .where(
                GithubUser.account_id == account.id,
                sa.or_(GithubRepo.active == True, GithubRepo.flexreview_active == True),
            )
            .group_by(GithubUser.id)
        )
        all_users_and_pr_count = cast(
            list[tuple[GithubUser, int]], db.session.execute(q).all()
        )
        github_usernames = active_github_usernames_onprem(account.id)
        total_removed = 0
        removed_users = []
        for user, pr_count in all_users_and_pr_count:
            user.active_collaborator = user.username in github_usernames
            if not user.billed:
                continue
            if not user.active_collaborator or pr_count == 0:
                # If the user is not an active collaborator or has not created any PRs
                # within the last N days, remove the user from billing.
                user.billed = False
                user.active = False
                total_removed += 1
                removed_users.append(user.username)
                logger.info(
                    "Removing user from billing for account",
                    user_id=user.id,
                    active_collaborator=user.active_collaborator,
                    pr_count=pr_count,
                    account_id=account.id,
                )
        if total_removed > 0:
            subscription = get_subscription(account.id)
            loss = subscription.amount_cents * total_removed
            if subscription.plan_length == "monthly":
                logger.info(
                    "Removed users from monthly plan",
                    total_removed=total_removed,
                    account_id=account.id,
                    loss=loss,
                )
            else:
                logger.info(
                    "Removed users from annual plan",
                    total_removed=total_removed,
                    account_id=account.id,
                    monthly_loss=loss,
                )
            logger.info(
                "Removed users for account",
                account_id=account.id,
                removed_user=removed_users,
            )
        db.session.commit()
    except errors.QueryLimitException:
        logger.info("Query limit reached, skipping remove_inactive_collaborators")
    logger.info("Finished remove_inactive_collaborators")


def remove_inactive_collaborators_for_account_cloud(account_id: int) -> None:
    logger.info(
        "Starting remove_inactive_collaborators for account",
        account_id=account_id,
    )
    account = Account.get_by_id_x(account_id)
    try:
        all_users: list[GithubUser] = GithubUser.query.filter_by(
            account_id=account.id
        ).all()
        all_billed_users = [u for u in all_users if u.billed]
        github_usernames = active_github_usernames_cloud(account.id)
        total_removed = 0
        removed_users = []
        for user in all_users:
            user.active_collaborator = user.username in github_usernames
        for user in all_billed_users:
            if user.username not in github_usernames:
                user.billed = False
                user.active = False
                total_removed += 1
                removed_users.append(user.username)
                logger.info(
                    "Removing user from billing for account",
                    user_id=user.id,
                    account_id=account.id,
                )
        if total_removed > 0:
            subscription = get_subscription(account.id)
            loss = subscription.amount_cents * total_removed
            if subscription.plan_length == "monthly":
                logger.info(
                    "Removed users from monthly plan",
                    total_removed=total_removed,
                    account_id=account.id,
                    loss=loss,
                )
            else:
                logger.info(
                    "Removed users from annual plan",
                    total_removed=total_removed,
                    account_id=account.id,
                    monthly_loss=loss,
                )
            logger.info(
                "Removed users for account",
                account_id=account.id,
                removed_user=removed_users,
            )
        db.session.commit()
    except errors.QueryLimitException:
        logger.info("Query limit reached, skipping remove_inactive_collaborators")
    logger.info("Finished remove_inactive_collaborators")


def active_github_usernames_onprem(account_id: int) -> set[str]:
    all_repos: list[GithubRepo] = GithubRepo.query.filter(
        GithubRepo.account_id == account_id,
        sa.or_(GithubRepo.active == True, GithubRepo.flexreview_active == True),
    ).all()
    all_github_usernames = set()
    for repo in all_repos:
        try:
            access_token, _ = common.get_client(repo)
        except errors.AccessTokenException:
            continue
        github_usernames = _get_collaborators(repo, access_token.token)
        all_github_usernames.update(github_usernames)
    return all_github_usernames


def active_github_usernames_cloud(account_id: int) -> set[str]:
    all_repos = GithubRepo.get_all_for_account(account_id)
    all_github_usernames = set()
    for repo in all_repos:
        try:
            access_token, _ = common.get_client(repo)
        except errors.AccessTokenException:
            continue
        github_usernames = _get_collaborators(repo, access_token.token)
        all_github_usernames.update(github_usernames)
    return all_github_usernames


class GHCollaborator(schema.BaseModel):
    login: str


def _get_collaborators(repo: GithubRepo, token: str, page: int = 1) -> list[str]:
    if page > MAX_COLLABORATOR_PAGES:
        raise errors.QueryLimitException("Max collaborator pages reached")
    try:
        github_collabs = gh_rest.get(
            f"repos/{repo.name}/collaborators?per_page=100&page={page}",
            list[GHCollaborator],
            token=token,
            account_id=repo.account_id,
        )
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            # The repository might be deleted. Consider that there's no active
            # collaborators on this repo.
            return []
        raise
    github_usernames = [collab.login for collab in github_collabs]
    if len(github_collabs) == 100:
        github_usernames.extend(_get_collaborators(repo, token, page + 1))
    return github_usernames


@celery.task
def analyze_users(repo_id: int) -> None:
    repo = GithubRepo.get_by_id_x(repo_id)
    if not repo.account.billing_active:
        return
    repo.bind_contextvars()

    logger.info("Analyzing users for repository")
    access_token, client = common.get_client(repo)
    github_repo = client.repo
    # fetch collaborators
    github_collabs = github_repo.get_collaborators()

    existing_collabs = repo.get_collaborators()
    existing_usernames = [u.username for u in existing_collabs]

    for collab in github_collabs:
        if collab.login not in existing_usernames:
            user = github_user.ensure(
                account_id=repo.account_id,
                login=collab.login,
                gh_type=collab.type,
                gh_node_id=collab.node_id,
                gh_database_id=collab.id,
            )
            user.avatar_url = collab.avatar_url
            db.session.add(RepoCollaborator(user=user, repo=repo))
    db.session.commit()
    logger.info("Analyzed users for repository")
