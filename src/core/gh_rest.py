from __future__ import annotations

import traceback
from os import path
from typing import Any, TypeVar

import pydantic
import structlog
import typing_extensions as TE
from requests import Response

from auth.models import AccessToken
from core import rate_limit
from main import app
from util import req

logger = structlog.stdlib.get_logger()

Ret = TypeVar("Ret")

GITHUB_API_BASE_URL: str = app.config["GITHUB_API_BASE_URL"]
REQUEST_TIMEOUT_SEC: int = int(app.config["REQUEST_TIMEOUT_SEC"])

HEADER_RATE_LIMIT = "x-ratelimit-limit"
HEADER_RATE_REMAINING = "x-ratelimit-remaining"
HEADER_NEXT_GLOBAL_ID = "x-github-next-global-id"


def get(
    path: str,
    return_type: type[Ret],
    *,
    token: str | AccessToken,
    account_id: int,
    override_headers: dict[str, str] | None = None,
) -> Ret:
    """
    Make a (typesafe) GET request to the GitHub API.
    """
    r = _request(account_id, "get", path, token, override_headers=override_headers)
    return pydantic.TypeAdapter(return_type).validate_python(r.json())


def get_non_json(
    path: str,
    *,
    token: str | AccessToken,
    account_id: int,
    override_headers: dict[str, str] | None = None,
) -> str:
    """
    Make a GET request to the GitHub API.

    This is used for making a non-JSON GitHub API endpoint. See
    https://docs.github.com/en/rest/using-the-rest-api/media-types?apiVersion=2022-11-28
    """
    r = _request(account_id, "get", path, token, override_headers=override_headers)
    return r.text


def get_raw(
    path: str,
    *,
    token: str | AccessToken,
    account_id: int,
    override_headers: dict[str, str] | None = None,
) -> bytes:
    """
    Make a GET request to the GitHub API. This method returns the raw byte response,
    use sparingly, such as for downloading files.
    """
    r = _request(account_id, "get", path, token, override_headers=override_headers)
    return r.content


def post(
    path: str,
    data: dict[str, Any],
    return_type: type[Ret],
    *,
    token: str | AccessToken,
    account_id: int,
) -> Ret:
    """
    Make a (typesafe) POST request to the GitHub API.

    This function transforms the JSON response into the given return type.
    """
    r = _request(account_id, "post", path, token, json=data)
    if r.status_code not in {202, 204}:
        return pydantic.TypeAdapter(return_type).validate_python(r.json())
    else:
        # Not ideal, but works around mypy for now
        return return_type()


def put(
    path: str,
    data: dict[str, Any],
    return_type: type[Ret],
    *,
    token: str | AccessToken,
    account_id: int,
) -> Ret:
    """
    Make a (typesafe) POST request to the GitHub API.

    This function transforms the JSON response into the given return type.
    """
    r = _request(account_id, "put", path, token, json=data)
    return pydantic.TypeAdapter(return_type).validate_python(r.json())


def delete(
    path: str,
    *,
    token: str | AccessToken,
    account_id: int,
) -> None:
    """
    Make a DELETE request to the GitHub API.
    """
    _request(account_id, "delete", path, token)


def delete_with_data(
    path: str,
    data: dict[str, Any],
    return_type: type[Ret],
    *,
    token: str | AccessToken,
    account_id: int,
) -> Ret:
    """
    Make a DELETE request to the GitHub API with request data.
    """
    r = _request(account_id, "delete", path, token, json=data)
    return pydantic.TypeAdapter(return_type).validate_python(r.json())


def _request(
    account_id: int,
    method: TE.Literal["get", "post", "delete", "put"],
    path: str,
    token: str | AccessToken,
    **kwargs: Any,
) -> Response:
    """
    Make a request to the GitHub API.

    This function is a wrapper around `requests.request` that adds some
    additional functionality, such as rate limiting and error handling.
    """
    assert not path.startswith("http"), "API path should not start with http(s)"
    path = path.lstrip("/")

    if isinstance(token, AccessToken):
        token = token.token

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        HEADER_NEXT_GLOBAL_ID: "1",
    }
    if "override_headers" in kwargs:
        # override_headers might be set as a kwarg, but it could be None
        override_headers = kwargs.pop("override_headers")
        if override_headers:
            headers.update(override_headers)

    url = f"{GITHUB_API_BASE_URL}/{path}"
    r = req.request(method, url, headers=headers, timeout=REQUEST_TIMEOUT_SEC, **kwargs)
    _log_and_capture_rate_limit(r, account_id)

    if r.status_code == 403:
        raise GHForbiddenError(http_code=r.status_code)
    else:
        r.raise_for_status()
    return r


def _log_and_capture_rate_limit(r: Response, account_id: int) -> None:
    """
    Log response and capture rate limit information from the given headers.

    This function is called by `get` and `post` to capture rate limit
    information from the response headers.
    """
    remaining = None
    limit = None
    if HEADER_RATE_LIMIT in r.headers and HEADER_RATE_REMAINING in r.headers:
        remaining = int(r.headers[HEADER_RATE_REMAINING])
        limit = int(r.headers[HEADER_RATE_LIMIT])
        rate_limit.set_gh_rest_api_limit(account_id, remaining, limit)

    stack = traceback.extract_stack()
    caller = []
    for frame in stack:
        if "core/" in frame.filename and "gh_rest.py" not in frame.filename:
            caller.append(
                f"{path.basename(frame.filename)}:{frame.lineno}:{frame.name}"
            )
    github_request_id = r.headers.get("x-github-request-id", None)
    if 200 <= r.status_code and r.status_code < 300:
        logger.info(
            "GitHub REST API request (success)",
            github_api_verb=r.request.method,
            github_api_url=r.url,
            github_api_status=r.status_code,
            github_request_id=github_request_id,
            github_rest_rate_limit_remaining=remaining,
            github_rest_rate_limit_limit=limit,
            elapsed=r.elapsed.total_seconds(),
            caller=caller,
        )
    else:
        logger.info(
            "GitHub REST API request (failure)",
            github_api_verb=r.request.method,
            github_api_url=r.url,
            github_api_status=r.status_code,
            github_request_id=github_request_id,
            github_rest_rate_limit_remaining=remaining,
            github_rest_rate_limit_limit=limit,
            github_rest_error_output=r.text[:1000],
            elapsed=r.elapsed.total_seconds(),
            caller=caller,
        )


class GHRestError(Exception):
    def __init__(self, *, http_code: int) -> None:
        self.http_code = http_code


class GHForbiddenError(GHRestError):
    pass
