from __future__ import annotations

import dataclasses

from main import redis_client
from util import time_util

# Percentage of rate limits where we stop performing low importance actions.
LOW_LIMITS_PCT = 20


@dataclasses.dataclass
class RateLimit:
    remaining: int
    total: int


def set_gh_rest_api_limit(account_id: int, remaining: int, total: int) -> None:
    """
    Sets the GitHub REST API rate limit in Redis.
    """
    redis_client.set(
        f"gh_rest_api_limit_{account_id}",
        f"{remaining}/{total}",
        ex=time_util.ONE_DAY_SECONDS,
    )


def get_gh_rest_api_limit(account_id: int) -> str | None:
    """
    Returns the GitHub REST API rate limit in Redis.
    :return: A string of the form "remaining/total" or None if the limit is unknown.
    """
    result = redis_client.get(f"gh_rest_api_limit_{account_id}")
    if result:
        return result.decode("utf-8")
    return None


def is_gh_rest_api_limit_low(account_id: int) -> bool:
    """
    Returns True if the GitHub REST API rate limit is low.
    """
    s = get_gh_rest_api_limit(account_id)
    if not s:
        return False
    remaining_s, total_s = s.split("/")
    remaining = int(remaining_s)
    total = int(total_s)
    return int(remaining / total * 100) < LOW_LIMITS_PCT


# TODO(ankit): Also capture rate limits for graphql.
def set_gh_graphql_api_limit(account_id: int, remaining: int, total: int) -> None:
    """
    Sets the GitHub GraphQL API rate limit in Redis.
    """
    redis_client.set(
        f"gh_graphql_api_limit_{account_id}",
        f"{remaining}/{total}",
        ex=time_util.ONE_DAY_SECONDS,
    )


def get_gh_graphql_api_limit(account_id: int) -> str | None:
    """
    Returns the GitHub GraphQL API rate limit in Redis.
    :return: A string of the form "remaining/total" or None if the limit is unknown.
    """
    result = redis_client.get(f"gh_graphql_api_limit_{account_id}")
    if result:
        return result.decode("utf-8")
    return None


def get_parsed_gh_graphql_api_limit(account_id: int) -> RateLimit | None:
    s = get_gh_graphql_api_limit(account_id)
    if not s:
        return None
    remaining_s, total_s = s.split("/")
    return RateLimit(remaining=int(remaining_s), total=int(total_s))
