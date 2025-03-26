from __future__ import annotations

import dataclasses
import datetime
import time

import jwt
import structlog
from dateutil import parser

import schema
import util.posthog_util
from auth.models import AccessToken, Account
from core import gh_rest, locks
from core.apply_config import apply_config_from_gh_file
from core.models import GithubRepo
from errors import AccessTokenException
from lib import gh
from main import MQ_KEY, app, celery, db
from util import req, time_util

logger = structlog.stdlib.get_logger()


@dataclasses.dataclass
class RawRepositoryData:
    """Raw data extracted from GitHub API."""

    full_name: str
    default_branch: str


def generate_access_token(
    installation_id: int,
) -> tuple[str, datetime.datetime] | tuple[None, None]:
    current_time = int(time.time())
    iss = app.config["GITHUB_APP_ID"]
    secret = MQ_KEY
    payload = {
        "iat": current_time,
        "exp": int(current_time + 300),
        "iss": iss,
    }

    encoded_jwt = jwt.encode(
        payload,
        secret,
        algorithm=app.config["GITHUB_JWT_ALGORITHM"],
    )

    url = (
        app.config["GITHUB_API_BASE_URL"]
        + f"/app/installations/{installation_id}/access_tokens"
    )
    r = req.post(
        url,
        data={},
        headers={
            "Authorization": "Bearer " + encoded_jwt,
            "Accept": "application/vnd.github.machine-man-preview+json",
        },
    )
    if r.status_code == 201:
        json_response = r.json()
        token = json_response.get("token")
        expiration = parser.parse(json_response.get("expires_at"))
        logger.info("Got token for installation", installation_id=installation_id)
        return token, expiration
    logger.info(
        "Got error to while authenticating installation_id %d: %s",
        installation_id,
        r.content,
    )
    return None, None


def _get_accessible_repos(token: str, page: int) -> tuple[list[RawRepositoryData], int]:
    url = (
        app.config["GITHUB_API_BASE_URL"]
        + "/installation/repositories?page="
        + str(page)
    )
    r = req.get(
        url,
        headers={
            "Authorization": "token " + token,
            "Accept": "application/vnd.github.v3+json",
        },
    )
    repos = []
    total_count = 0
    if r.status_code == 200:
        json_response = r.json()
        for repo in json_response.get("repositories", []):
            repos.append(
                RawRepositoryData(
                    full_name=repo["full_name"], default_branch=repo["default_branch"]
                )
            )
        total_count = json_response.get("total_count", 0)
    else:
        logger.info("Got error to while fetching repos", response=r.content)
    return repos, total_count


def analyze_tests(repo: GithubRepo) -> list[str]:
    from core.common import get_client

    access_token, client = get_client(repo)
    head_branch = client.repo.default_branch
    url = (
        app.config["GITHUB_API_BASE_URL"]
        + "/repos/"
        + repo.name
        + "/branches/"
        + head_branch
    )
    r = req.get(
        url,
        headers={
            "Authorization": "token " + access_token.token,
            "Accept": "application/vnd.github.v3+json",
        },
    )

    contexts: list[str] = []
    if r.status_code == 200:
        json_response = r.json()
        protections = json_response.get("protection", {})
        if protections.get("enabled") and protections.get("required_status_checks"):
            contexts = protections["required_status_checks"].get("contexts", [])
            logger.info("Found %d contexts on %s repo", len(contexts), repo.name)
        if not contexts:
            # only fetch results from rulesets if there are no default tests
            contexts = analyze_rulesets(repo, access_token.token)
        return contexts
    raise Exception(
        "Could not find the list of required checks or protection not enabled %s"
        % repo.name
    )


class Rulesets(schema.BaseModel):
    type: str
    parameters: dict | None = None
    ruleset_source_type: str | None = None
    ruleset_source: str | None = None
    ruleset_id: int | None = None


def analyze_rulesets(repo: GithubRepo, token: str) -> list[str]:
    rulesets = gh_rest.get(
        f"repos/{repo.name}/rules/branches/{repo.head_branch}",
        list[Rulesets],
        token=token,
        account_id=repo.account_id,
    )
    if not rulesets:
        return []
    required_tests: set[str] = set()
    for ruleset in rulesets:
        if ruleset.type == "required_status_checks" and ruleset.parameters:
            params: list[dict] = ruleset.parameters["required_status_checks"]
            required_tests.update([x["context"] for x in params])
    if required_tests:
        logger.info(
            "Found required tests in rulesets",
            repo_id=repo.id,
            test_count=len(required_tests),
        )
    return list(required_tests)


def setup_installation(
    account: Account,
    installation_id: int,
    token: str,
    expiration: datetime.datetime,
) -> AccessToken:
    access_token: AccessToken | None = AccessToken.query.filter_by(
        account_id=account.id, installation_id=installation_id
    ).first()

    if access_token:
        # Don't override the token if a dev token is set.
        if is_dev_token(access_token):
            return access_token
        access_token.token = token
        access_token.expiration_date = expiration
    else:
        access_token = AccessToken(
            account_id=account.id,
            installation_id=installation_id,
            encrypted_token=AccessToken.encrypt_token(token),
            expiration_date=expiration,
        )
        db.session.add(access_token)
    db.session.commit()
    return access_token


def is_dev_token(access_token: AccessToken) -> bool:
    """
    Determine whether or not the access token is a dev token.

    A dev token is a GitHub personal access token (PAT) that Aviator will use
    instead of the normal GitHub app installation.
    """
    return bool(access_token) and access_token.token.startswith("ghp")


def ensure_access_token(
    access_token: AccessToken,
    force: bool = False,
) -> AccessToken | None:
    if not access_token:
        return None
    if access_token.expiration_date > datetime.datetime(
        2050, 1, 1, tzinfo=datetime.UTC
    ):
        # ignore force if the token is dev-token
        return access_token

    now = time_util.now() + datetime.timedelta(seconds=20)
    if access_token.expiration_date <= now or force:
        token, expiration = generate_access_token(access_token.installation_id)
        if not token or not expiration:
            return None
        access_token.token = token
        access_token.expiration_date = expiration
        db.session.commit()
    return access_token


@celery.task
def fetch_repos_from_installation(installation_id: int) -> None:
    access_token: AccessToken | None = AccessToken.query.filter_by(
        installation_id=installation_id,
    ).first()
    if not access_token:
        logger.info(
            "No associated account found for installation",
            installation_id=installation_id,
        )
        return
    access_token = ensure_access_token(access_token)
    if not access_token:
        logger.info(
            "Could not validate access token for installation",
            installation_id=installation_id,
        )
        return
    fetch_repos(access_token)


@celery.task
def fetch_repos_async(account_id: int) -> None:
    all_access_tokens = AccessToken.query.filter_by(account_id=account_id)
    for access_token in all_access_tokens:
        access_token = ensure_access_token(access_token)
        page = 1
        has_more_pages = fetch_repos(access_token, page)
        while has_more_pages:
            page += 1
            has_more_pages = fetch_repos(access_token, page)


@celery.task
def fetch_repos_for_access_token_async(access_token_id: int) -> None:
    access_token = ensure_access_token(AccessToken.get_by_id_x(access_token_id))
    if not access_token:
        return

    page = 1
    has_more_pages = fetch_repos(access_token, page)
    while has_more_pages:
        page += 1
        has_more_pages = fetch_repos(access_token, page)


def fetch_repos(access_token: AccessToken, page: int = 1) -> bool:
    """Returns true if there are more pages to fetch."""
    if is_dev_token(access_token):
        return fetch_authenticated_user_repos(
            access_token.token, access_token.account_id, page
        )
    else:
        return fetch_repos_with_access_token(access_token, page)


def fetch_repos_with_access_token(access_token: AccessToken, page: int = 1) -> bool:
    raw_repo_data, total_count = _get_accessible_repos(access_token.token, page)
    logger.info(
        "Got repos for account",
        num_repos=len(raw_repo_data),
        account_id=access_token.account_id,
        total_num_repos=total_count,
    )
    _add_new_repos(access_token, raw_repo_data)

    per_page = 30
    return total_count > page * per_page


def fetch_authenticated_user_login(*, token: str) -> gh.User | None:
    r = req.get(
        url=app.config["GITHUB_API_BASE_URL"] + "/user",
        headers={
            "Authorization": "token " + token,
            "Accept": "application/vnd.github.v3+json",
        },
    )
    if r.status_code == 200:
        return gh.User.model_validate(r.json())
    logger.warning(
        "Failed to fetch user info for token",
        content=r.content,
        status_code=r.status_code,
    )
    return None


def fetch_authenticated_user_repos(token: str, account_id: int, page: int = 1) -> bool:
    url = app.config["GITHUB_API_BASE_URL"] + "/user/repos?page=" + str(page)
    r = req.get(
        url,
        headers={
            "Authorization": "token " + token,
            "Accept": "application/vnd.github.v3+json",
        },
    )
    if r.status_code != 200:
        raise Exception(
            "Failed to fetch repos for authenticated user on account %d", account_id
        )

    raw_repo_data = []
    for repo in r.json():
        raw_repo_data.append(
            RawRepositoryData(
                full_name=repo.get("full_name"),
                default_branch=repo.get("default_branch"),
            )
        )
    if len(raw_repo_data) == 0:
        return False
    logger.info(
        "Got repos for authenticated user",
        num_repos=len(raw_repo_data),
        account_id=account_id,
    )
    access_token = AccessToken.query.filter_by(token=token).first()
    _add_new_repos(access_token, raw_repo_data)

    # the response does not contain total count so we assume there is a next page if the page is full
    return len(raw_repo_data) == 30


def _add_new_repos(
    access_token: AccessToken, raw_repo_data: list[RawRepositoryData]
) -> None:
    with locks.for_account(access_token.account_id):
        new_repos = []
        for repo_data in raw_repo_data:
            repo: GithubRepo | None = GithubRepo.query.filter_by(
                name=repo_data.full_name, account_id=access_token.account_id
            ).first()
            if not repo:
                repo = GithubRepo(
                    name=repo_data.full_name,
                    account_id=access_token.account_id,
                    access_token_id=access_token.id,
                    active=False,  # always add GitHub with MQ off by default
                    head_branch=repo_data.default_branch,
                )
                db.session.add(repo)
                new_repos.append(repo)
            if not repo.access_token_id and access_token:
                repo.access_token_id = access_token.id
        db.session.commit()

        for repo in new_repos:
            # add initial config history entry for repo
            # since this is a new repo we know there is no existing config
            apply_config_from_gh_file.delay(
                repo_name=repo.name, aviator_user_id=repo.account.first_admin.id
            )
            util.posthog_util.capture_repository_event(
                util.posthog_util.PostHogEvent.CONNECT_REPO,
                repo,
            )


def ensure_renamed_repository(old_repo: GithubRepo, new_name: str) -> None:
    """
    If a repository is renamed, we need to update the name in the database.
    Let's also make sure that a new repo wasn't created already to avoid duplicates.
    """
    with locks.for_account(old_repo.account_id):
        new_repo: GithubRepo | None = GithubRepo.query.filter_by(
            name=new_name, account_id=old_repo.account_id
        ).first()
        if new_repo:
            logger.error(
                "New repo already exists for renamed repo",
                old_repo_id=old_repo.id,
                new_repo_id=new_repo.id,
            )
            return

        old_repo.name = f"{old_repo.org_name}/{new_name}"
        db.session.commit()
        logger.info("Renamed repo", old_repo_id=old_repo.id, new_name=new_name)


def validate_access_token(token: str, account_id: int) -> bool:
    from core.common import get_client

    repo = GithubRepo.query.filter_by(
        account_id=account_id, active=True, enabled=True
    ).first()
    try:
        if repo:
            _, _ = get_client(repo)
        else:
            fetch_authenticated_user_repos(token, account_id)
        return True
    except Exception as e:
        logger.warning("Validation failed", account_id=account_id, exc_info=e)
    return False


def num_existing_repos(account_id: int) -> int:
    count: int = GithubRepo.query.filter_by(account_id=account_id).count()
    return count


def ensure_access_token_from_installation(installation_id: int | None) -> AccessToken:
    assert installation_id is not None, "Missing installation id"
    access_token: AccessToken | None = AccessToken.query.filter_by(
        installation_id=installation_id
    ).first()
    if not access_token:
        logger.error("App not installed", installation_id=installation_id)
        raise AccessTokenException("App installation not found.")
    access_token = ensure_access_token(access_token)
    if access_token is None:
        raise AccessTokenException("Unable to obtain access token.")

    return access_token
