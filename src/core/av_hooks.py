from __future__ import annotations

import dataclasses

import pydantic
import structlog

import schema.github
import schema.hooks
from auth.models import AccessToken
from core import configfile, pygithub
from core.client import GithubClient
from core.models import GithubRepo, PullRequest
from main import app
from pilot.context import ScenarioContext
from pilot.function.script import execute_js

logger = structlog.stdlib.get_logger()


@dataclasses.dataclass
class HookExecutionError(Exception):
    # The error message returned by the hook.
    error: str


def ready(
    client: GithubClient,
    access_token: AccessToken,
    repo: GithubRepo,
    pr: PullRequest,
    pull: pygithub.PullRequest,
) -> bool:
    """
    Execute the ready hook for a given repository and pull request.

    :returns: True if the hook was executed (or False if it there was no
        configured ready hook for the repository).
    :raises
        HookExecutionError: if the hook throws an error during execution.
    """
    hook_config = configfile.RepoConfigFile.load(repo, configfile.HOOK_READY)
    if not hook_config:
        return False

    event = schema.hooks.ReadyHookEvent(
        pull_request=schema.github.PullRequest.model_validate(
            pull._rawData,
        ),
    )
    context = ScenarioContext(
        github=client,
        repo=repo,
        account_id=repo.account_id,
        gh_token=access_token.token,
        installation_id=access_token.installation_id,
    )

    slog = logger.bind(
        repo_id=repo.id,
        pr_id=pr.id,
        pr_number=pr.number,
    )
    slog.info("Executing ready hook")
    res = execute_js(
        hook_config.text,
        event,
        context,
        function="ready",
    )

    if res.error:
        slog.info("Got error while executing ready hook", error=res.error)
        raise HookExecutionError(error=res.error)
    return True
