from __future__ import annotations

from typing import Literal, Protocol

import sqlalchemy as sa

from core import client, locks, models, pygithub
from lib import gh
from main import db, logger


def ensure(
    *,
    account_id: int,
    login: str,
    gh_type: str,
    gh_node_id: str | None,
    gh_database_id: int,
) -> models.GithubUser:
    """
    Ensure that a GithubUser model exists.

    :param account_id: The Aviator account ID associated with the user.
    :param login: The GitHub login (username) of the user.
    :param gh_type: The GitHub type of the user. Should be one of "User" or
        "Bot". Typed as a string instead of an enum or literal to make
        interacting with GitHub API structs easier.
    :param gh_node_id: The GitHub GraphQL node ID of the user.
    :param gh_database_id: The (integer) GitHub database ID of the user. This is
        used (along with gh_type) to ensure uniqueness in the database and
        prevent duplicates.
    """
    # Find an existing GithubUser model associated with the account ID and
    # GitHub login.

    # We only want to store the new style ("next") GitHub global node IDs here.
    is_next_global_node_id = gh_node_id is not None and any(
        gh_node_id.startswith(prefix)
        for prefix in (gh.ID_PREFIX_BOT, gh.ID_PREFIX_ORGANIZATION, gh.ID_PREFIX_USER)
    )
    if not is_next_global_node_id:
        gh_node_id = None

    # Try to find an existing record with the database ID filled.
    # This doesn't require a lock because the database ID is immutable and
    # uniqueness is enforced by the database.
    existing: models.GithubUser | None = db.session.execute(
        sa.select(models.GithubUser).filter(
            models.GithubUser.account_id == account_id,
            models.GithubUser.deleted == False,
            models.GithubUser.gh_type == models.GithubUserType(gh_type),
            models.GithubUser.gh_database_id == gh_database_id,
        )
    ).scalar_one_or_none()
    if existing:
        existing.gh_node_id = gh_node_id or existing.gh_node_id
        # This might be a slight data race here, but that's overall fine since
        # we'll just catch it on the next processing of the user.
        existing.username = login
        db.session.commit()
        return existing

    # We don't have a row with an existing database ID, so we need to see if
    # there are any rows with a NULL database ID and a matching login.
    # We need to lock here since multiple threads might be trying to back-fill
    # the same row.
    # This lock shouldn't™ be toooooo bad for performance since we only need to
    # go through this once per user. Once we've done this once, the code above
    # should handle it just fine without any locking required.
    with locks.lock("github_user", f"github_user/{account_id}/{login}"):
        # Find a row with a NULL database ID and a matching login.
        # Since there might be duplicate GithubUser rows in the database, we
        # break ties by choosing the row with the most pull requests.
        row = db.session.execute(
            sa.select(models.GithubUser)
            .select_from(
                sa.join(
                    models.GithubUser.__table__,
                    models.PullRequest.__table__,
                    models.PullRequest.creator_id == models.GithubUser.id,
                    # Default is left inner join which will cause this query to
                    # return no data if the GithubUser has no pull requests. We
                    # need to make this an outer join so that even if the user
                    # has no pull requests, we still get the GithubUser row.
                    isouter=True,
                )
            )
            .where(models.GithubUser.account_id == account_id)
            .where(models.GithubUser.username == login)
            .where(models.GithubUser.deleted == False)
            .where(models.GithubUser.gh_type.is_(None))
            .where(models.GithubUser.gh_database_id.is_(None))
            .group_by(models.GithubUser.__table__)
            .order_by(
                sa.func.count(models.PullRequest.id).desc(),
                models.GithubUser.id.asc(),
            )
        ).first()
        existing = row[0] if row else None
        if existing:
            assert isinstance(existing, models.GithubUser), (
                f"Expected GithubUser, got {type(existing)}"
            )
            existing.gh_type = models.GithubUserType(gh_type)
            existing.gh_database_id = gh_database_id
            existing.gh_node_id = gh_node_id or existing.gh_node_id
            db.session.commit()
            return existing

    # Finally, no row exists, so we need to create a new one.
    # We use an UPSERT (with ON CONFLICT ...) here to avoid any possible
    # uniqueness conflicts caused by racing tasks.
    upserted: models.GithubUser = db.session.scalar(
        sa.select(models.GithubUser)
        .from_statement(
            sa.dialects.postgresql.insert(models.GithubUser)
            .values(
                account_id=account_id,
                username=login,
                gh_type=models.GithubUserType(gh_type),
                gh_node_id=gh_node_id,
                gh_database_id=gh_database_id,
            )
            # No-op update here, but we can't use `on_conflict_do_nothing` since
            # that won't return the row if it already exists.
            .on_conflict_do_update(
                index_elements=["account_id", "gh_type", "gh_database_id"],
                set_=dict(
                    gh_node_id=gh_node_id,
                ),
            )
            .returning(models.GithubUser)
        )
        .execution_options(populate_existing=True)
    )
    assert isinstance(upserted, models.GithubUser), (
        f"Expected GithubUser, got {type(upserted)}"
    )
    db.session.commit()
    return upserted


class Actor(Protocol):
    """
    We have to use a protocol here because ariadne-codegen doesn't generate
    type annotations for the `Actor` fragment because it's on an interface.
    """

    @property
    def typename__(self) -> str: ...

    @property
    def id(self) -> str: ...

    @property
    def login(self) -> str: ...

    @property
    def database_id(self) -> int: ...

    @property
    def name(self) -> str | None: ...


def ensure_from_graphql_actor(actor: Actor, *, account_id: int) -> models.GithubUser:
    """
    Ensure that a GithubUser model exists given a GraphQL actor fragment.

    This is based on the `Actor` fragment in the `graphql_client` module which
    is code-generated.
    """
    return ensure(
        account_id=account_id,
        login=actor.login,
        gh_type=actor.typename__,
        gh_node_id=actor.id,
        gh_database_id=actor.database_id,
    )


def lookup_by_login_do_not_use_unless_you_know_what_youre_doing(
    *,
    account_id: int,
    login: str,
    yes_i_pinky_promise_i_know_what_im_doing: Literal[True],
) -> models.GithubUser | None:
    """
    Look up a GithubUser model by account ID and GitHub login.

    IMPORTANT:
        Only use this in situations where it is impossible to use the GitHub
        node ID. GitHub logins are **not** immutable and might be subject to
        change.

    :param account_id: The Aviator account ID associated with the user.
    :param login: The GitHub login (username) of the user.
    :param yes_i_pinky_promise_i_know_what_im_doing: You must pass
        ``yes_i_pinky_promise_i_know_what_im_doing=True`` to use this function.
        If you break a pinky promise, you'll make babies cry. Don't do that.
    """
    assert yes_i_pinky_promise_i_know_what_im_doing is True
    return db.session.execute(
        sa.select(models.GithubUser).filter(
            models.GithubUser.account_id == account_id,
            models.GithubUser.username == login,
            models.GithubUser.deleted == False,
        )
        # We miiiiiight have multiple GithubUser models with the same login.
        # Return the oldest just to ensure deterministic behavior.
        .order_by(models.GithubUser.id.asc())
    ).scalar_one_or_none()


def fetch_by_login_do_not_use_unless_you_know_what_youre_doing(
    gh_client: client.GithubClient,
    *,
    account_id: int,
    login: str,
    yes_i_pinky_promise_i_know_what_im_doing: Literal[True],
) -> models.GithubUser | None:
    """
    Fetch a GithubUser model by account ID and GitHub login if it exists.

    If the GithubUser model does not exist, fetch the GitHub user data from the
    GitHub API and create a new GithubUser model.

    Returns None if the GitHub user does not exist within the GitHub API.

    IMPORTANT:
        This should only be used in the limited contexts(*) where it's
        impossible  to know a GitHub user's node ID. GitHub logins are **not**
        immutable and might be subject to change.

        (*) The most common place this occurs is when trying to map a commit to
        a GitHub user since the underlying Git commit might not actually belong
        to a user with a GitHub account.
    """
    assert yes_i_pinky_promise_i_know_what_im_doing is True
    try:
        gh_user_data = gh_client.client.get_user(login)
    except pygithub.GithubException as exc:
        if exc.status == 404:
            logger.error("Failed to lookup GitHub user", login=login)
            return None
        raise

    return ensure(
        account_id=account_id,
        login=login,
        gh_type=gh_user_data.type,
        gh_node_id=gh_user_data.node_id,
        gh_database_id=gh_user_data.id,
    )