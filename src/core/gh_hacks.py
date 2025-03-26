from __future__ import annotations

import datetime
from typing import Any

from github.GithubObject import GithubObject, _ValuedAttribute

original_makeDatetimeAttribute = GithubObject._makeDatetimeAttribute


def _makeDatetimeAttribute(*args: Any) -> Any:
    """
    Override the GithubObject._makeDatetimeAttribute method to set the
    timezone to UTC.
    """
    # This can either return a _ValuedAttribute or a _BadAttribute.
    # It's further processed by PyGitHub to only return the underlying value
    # when you access (e.g.) pull.created_at, but here we still need to make
    # sure to return the _ValuedAttribute.
    res = original_makeDatetimeAttribute(*args)
    if isinstance(res, _ValuedAttribute):
        value = res.value
        if isinstance(value, datetime.datetime):
            value = value.replace(tzinfo=datetime.timezone.utc)
            return _ValuedAttribute(value)
    return res


GithubObject._makeDatetimeAttribute = staticmethod(_makeDatetimeAttribute)  # type: ignore[assignment]