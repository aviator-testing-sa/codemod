from __future__ import annotations

import dataclasses
import datetime
import time
from collections.abc import Iterable

import pydantic
import sqlalchemy as sa
import structlog
import typing_extensions as TE
from sqlalchemy.orm import joinedload

import schema
from core import checks, common, gh_rest, locks
from core.checks import GithubTestInfo
from core.models import (
    AcceptableTestStatus,
    BotPr,
    GitCommit,
    GithubRepo,
    GithubTest,
    GithubTestStatus,
    PullRequest,
    TestStatus,
)
from main import app, db
from util import profile_util, time_util

logger = structlog.stdlib.get_logger()
MAX_PENDING_DISPLAY = 5
MAX_FAILURE_DISPLAY = 5

GITHUB_API_BASE_URL: str = app.config["GITHUB_API_BASE_URL"]

DEFAULT_FAILURE_STATES: tuple[TestStatus, ...] = (
    # Check run
    TestStatus.Cancelled,
    TestStatus.TimedOut,
    TestStatus.ActionRequired,
    TestStatus.Stale,
    # Commit status
    TestStatus.Error,
    # Both check run and commit status
    TestStatus.Failure,
)

DEFAULT_SUCCESS_STATES: tuple[TestStatus, ...] = (
    # Check run
    TestStatus.Neutral,
    TestStatus.Skipped,
    # Both check run and commit status
    TestStatus.Success,
)


class GHCheckSuite(schema.BaseModel):
    #: The database ID of the check run.
    gh_database_id: int = pydantic.Field(alias="id")

    head_branch: str | None = None


class GHCheckRun(schema.BaseModel):
    """
    A GitHub ``CheckRun`` object retrieved from the GitHub REST API.
    https://docs.github.com/en/rest/checks/runs

    Check runs are the newer (circa 2018) way to report information to a commit
    (and by proxy, for a pull request). GitHub actions (for example) use check
    runs to report their statuses.

    Check runs support a wider range of different results than commit statuses
    (such as skipped, neutral, etc.) and can also include annotations (i.e.,
    inline code comments in the code review UI).

    See also: ``GHCommitStatus``.
    """

    #: The database ID of the check run.
    gh_database_id: int = pydantic.Field(alias="id")

    #: The GraphQL ID of the check run.
    node_id: str

    #: The name of the check (e.g., "test-coverage").
    name: str

    #: The SHA of the commit that is being checked.
    head_sha: str

    #: An external URL that will have the full details of the check
    #: (e.g., a link to CircleCI).
    details_url: str | None = None

    #: The GitHub URL of the status check.
    #: NB: According to the GitHub API schema, this can be nullable but it never
    #: actually seems to be null in practice.
    html_url: str

    #: The current status of the check run.
    #: If this is "completed", then the ``conclusion`` field will be set.
    #: NB: The REST API docs (as of 2023-03-08) say that this should only be
    #: one of "queued", "in_progress", or "completed" but in practice we've
    #: seen "pending" as well.
    status: TE.Literal[
        "pending",
        "queued",
        "in_progress",
        "completed",
    ]

    #: The conclusion (i.e., failure/success status) of the check.
    #: Set if and only if the status is "completed".
    conclusion: (
        None
        | (
            TE.Literal[
                "action_required",
                "cancelled",
                "failure",
                "neutral",
                "skipped",
                "stale",
                "success",
                "timed_out",
            ]
        )
    )

    #: The time when the check started.
    started_at: datetime.datetime | None

    #: The time when the check completed.
    completed_at: datetime.datetime | None

    check_suite: GHCheckSuite

    #: The webhook receives pull_requests.
    pull_requests: list[GHPullRequest]


class GHCommitCheckRunsList(schema.BaseModel):
    total_count: int
    check_runs: list[GHCheckRun]


def fetch_gh_check_runs(
    token: str, repo_name: str, commit_sha: str, *, account_id: int
) -> list[GHCheckRun]:
    """
    Fetch all the check runs for a given commit.
    """
    check_runs: list[GHCheckRun] = []

    override_headers = {}
    if app.config.get("GITHUB_ENTERPRISE_2_2"):
        # COMPAT WORKAROUND:
        # Bosch runs a very old version of GHE which requires this special
        # preview header.
        override_headers["Accept"] = "application/vnd.github.antiope-preview+json"

    page = 1
    has_more = True
    while has_more:
        # Let's be a little defensive here.
        assert page <= 20, "too many pages of check runs"

        current_page = gh_rest.get(
            f"repos/{repo_name}/commits/{commit_sha}/check-runs?per_page=100&page={page}",
            GHCommitCheckRunsList,
            token=token,
            override_headers=override_headers,
            account_id=account_id,
        )
        check_runs.extend(current_page.check_runs)
        has_more = (
            len(current_page.check_runs) > 0
            and len(check_runs) < current_page.total_count
        )
        page += 1

    return sorted(check_runs, key=lambda c: c.gh_database_id, reverse=True)


class GHCommitStatus(schema.BaseModel):
    """
    A GitHub ``CommitStatus`` object retrieved from the GitHub REST API.

    Commit statuses are the original (circa 2012) way to report information from
    a CI system to a commit (and by proxy, for a pull request). Travis CI and
    Circle CI, for example, uses commit statuses to report back to GitHub.

    See also: ``GHCheckRun``.
    """

    state: TE.Literal["error", "failure", "pending", "success"]
    target_url: str | None = None
    description: str | None = None
    context: str
    updated_at: datetime.datetime

    @property
    def url(self) -> str:
        # According to the GH API, target_url can be null but a lot of code
        # assumes that it is not. :shrug:
        return self.target_url or ""


class GHCombinedStatus(schema.BaseModel):
    """
    See https://docs.github.com/en/rest/commits/statuses?apiVersion=2022-11-28#get-the-combined-status-for-a-specific-reference.
    """

    #: The combined state of all the commit statuses.
    #: - failure if any of the contexts report as error or failure
    #: - pending if there are no statuses or a context is pending
    #: - success if the latest status for all contexts is success
    state: TE.Literal["failure", "pending", "success"]
    total_count: int
    statuses: list[GHCommitStatus]


class GHPullRequest(schema.BaseModel):
    """
    See https://docs.github.com/en/webhooks/webhook-events-and-payloads#check_run
    Defining a model for webhooks as other models require property not passed from Checkrun webhook
    """

    id: int
    number: int
    url: str


def fetch_gh_commit_statuses(
    token: str, repo_name: str, commit_sha: str, *, account_id: int
) -> list[GHCommitStatus]:
    """
    Fetch all the commit statuses for the given commit.
    """
    statuses: list[GHCommitStatus] = []

    page = 1
    has_more = True
    while has_more:
        # Let's be a little defensive here.
        assert page <= 20, "too many pages of status checks"

        current_page = gh_rest.get(
            f"repos/{repo_name}/commits/{commit_sha}/status?per_page=100&page={page}",
            GHCombinedStatus,
            token=token,
            override_headers={
                "Accept": "application/vnd.github.antiope-preview+json",
            },
            account_id=account_id,
        )
        statuses.extend(current_page.statuses)
        has_more = (
            len(current_page.statuses) > 0 and len(statuses) < current_page.total_count
        )
        page += 1
    return statuses


@dataclasses.dataclass
class FetchedTestStatus:
    gh_test: GithubTest
    job_url: str
    status: TestStatus
    status_updated_at: datetime.datetime | None

    @staticmethod
    def from_status(gh_test: GithubTest, status: GHCommitStatus) -> FetchedTestStatus:
        return FetchedTestStatus(
            gh_test=gh_test,
            job_url=status.url,
            status=common.str_to_test_status(status.state),
            status_updated_at=status.updated_at,
        )

    @staticmethod
    def from_checkrun(gh_test: GithubTest, check_run: GHCheckRun) -> FetchedTestStatus:
        # There can be multiple checkruns per GithubTest. Those checkruns are
        # created for different PRs with the same commit hash. Each has a unique
        # ID, but for historical reasons, we use the URL for the uniqueness key
        # (which anyway has a unique ID in it).
        url = check_run.details_url or check_run.html_url
        return FetchedTestStatus(
            gh_test=gh_test,
            job_url=url,
            status=common.str_to_test_status(check_run.conclusion),
            status_updated_at=check_run.completed_at,
        )


def update_test_statuses(
    *,
    repo: GithubRepo,
    commit: GitCommit,
    fetched_at: datetime.datetime,
    fetched_statuses: list[FetchedTestStatus],
) -> list[GithubTestStatus]:
    """Update the GithubTestStatus based on the fetched statuses.

    The GithubTestStatus is identified by a pair of (gh_test, job_url) for a
    given commit. If there's a such GithubTestStatus, this updates its status.
    Otherwise, this creates a new GithubTestStatus based on the given status.

    When updating an existing GithubTestStatus, it checks the fetched_at time.
    If the existing one has a newer fetched_at, it won't make an update.
    """
    start_time = time.monotonic()

    # HISTORY
    #
    # This function is called from two sites: GitHub webhook and the queue processing
    # code. The queue processing code fetches all the status checks via the API and then
    # call this. The webhook code calls this with just one status check. In some cases,
    # a PR can have 1000+ test statuses.
    #
    # Initially, we used to take a lock at the commit level. This becomes problematic
    # because a lot of webhooks can come in at the same time, and they have a lock
    # contention. This make the celery queue length to be very long. We then switched to
    # a fine grained lock at commit+test_id level
    # (https://github.com/aviator-co/mergeit/pull/4058).
    #
    # This worked for 2 years, and then we got a report that a PR won't get merged for a
    # long time. The reason was that because this takes a lot of locks + db
    # transactions, it takes a long time to process. Let's say a PR has 1000 test
    # statuses, and if each test status takes 100ms (lock + db query + db update), it
    # can take 100 seconds to process.
    #
    # As of 2025-02-04, checking the code, it seems that even if there's a duplicate,
    # core.checks.required_test_summary will deduplicate the test statuses based on the
    # status_updated_at / fetched_at. So, we can just skip the lock and just update the
    # rows. Since this won't take a lock, it's possible that we have duplicate rows in
    # some situation, but we can live with it.
    use_lock = bool(app.config.get("TAKE_LOCK_ON_UPDATE_TEST_STATUS"))
    try:
        if use_lock:
            with locks.lock("github_test_status", f"github_test_status_{commit.sha}"):
                return _update_test_statuses(
                    repo=repo,
                    commit=commit,
                    fetched_at=fetched_at,
                    fetched_statuses=fetched_statuses,
                )
        return _update_test_statuses(
            repo=repo,
            commit=commit,
            fetched_at=fetched_at,
            fetched_statuses=fetched_statuses,
        )
    finally:
        logger.info(
            "Updated test statuses",
            use_lock=use_lock,
            duration=time.monotonic() - start_time,
            num=len(fetched_statuses),
        )


def _update_test_statuses(
    *,
    repo: GithubRepo,
    commit: GitCommit,
    fetched_at: datetime.datetime,
    fetched_statuses: list[FetchedTestStatus],
) -> list[GithubTestStatus]:
    results: list[GithubTestStatus] = []
    status_list: list[GithubTestStatus] = db.session.execute(
        sa.select(GithubTestStatus).where(
            GithubTestStatus.repo_id == repo.id,
            GithubTestStatus.commit_id == commit.id,
            GithubTestStatus.deleted.is_(False),
            GithubTestStatus.github_test_id.in_(
                [st.gh_test.id for st in fetched_statuses],
            ),
        ),
    ).scalars().all()
    statuses: dict[tuple[int, str | None], GithubTestStatus] = {
        (status.github_test_id, status.job_url): status for status in status_list
    }
    for fetched in fetched_statuses:
        gh_status: GithubTestStatus | None = statuses.get(
            (fetched.gh_test.id, fetched.job_url),
        )
        if not gh_status:
            gh_status = GithubTestStatus(
                github_test_id=fetched.gh_test.id,
                repo_id=repo.id,
                commit_id=commit.id,
                head_commit_sha=commit.sha,
                status=fetched.status,
                job_url=fetched.job_url,
                fetched_at=fetched_at,
            )
            db.session.add(gh_status)
            logger.info(
                "Creating new GithubTestStatus database entry",
                github_test_id=gh_status.github_test_id,
                job_url=gh_status.job_url,
                new_status=gh_status.status,
                new_status_updated_at=fetched.status_updated_at,
            )
        elif fetched.status_updated_at and time_util.is_before(
            gh_status.status_updated_at,
            fetched.status_updated_at,
        ):
            logger.info(
                "Updating existing GithubTestStatus",
                github_test_id=gh_status.github_test_id,
                job_url=gh_status.job_url,
                prev_status=gh_status.status,
                prev_status_updated_at=gh_status.status_updated_at,
                new_status=fetched.status,
                new_status_updated_at=fetched.status_updated_at,
            )
            gh_status.status = fetched.status
            gh_status.status_updated_at = fetched.status_updated_at
            gh_status.fetched_at = fetched_at
        elif gh_status.status != fetched.status and time_util.is_before(
            gh_status.fetched_at,
            fetched_at,
        ):
            logger.info(
                "Updating existing GithubTestStatus",
                github_test_id=gh_status.github_test_id,
                job_url=gh_status.job_url,
                prev_status=gh_status.status,
                prev_fetched_at=gh_status.fetched_at,
                new_status=fetched.status,
                new_fetched_at=fetched_at,
            )
            gh_status.status = fetched.status
            gh_status.fetched_at = fetched_at
        else:
            gh_status.fetched_at = fetched_at

        results.append(gh_status)
    db.session.commit()
    return results


def get_acceptable_statuses_map(
    github_test_ids: Iterable[int],
    for_override: bool,
) -> dict[int, list[TestStatus]]:"""
    Get a map of github_test_id to a list of acceptable statuses.
    """
    ats: list[AcceptableTestStatus] = (
        db.session.scalars(
            sa.select(AcceptableTestStatus)
            .where(
                AcceptableTestStatus.github_test_id.in_(github_test_ids),
                AcceptableTestStatus.for_override == for_override,
            )
        )
        .all()
    )
    result: dict[int, list[TestStatus]] = {}
    for s in ats:
        result[s.github_test_id] = result.get(s.github_test_id, []) + [s.status]
    return result


@dataclasses.dataclass
class PRTestPreview:
    """
    A preview of the test statuses for a PR.

    This is useful for showing a short-form summary of the current status of a
    test suite (e.g., in the Aviator status comment or the web UI).
    """

    total_success_tests: int
    total_pending_tests: int
    total_failure_tests: int

    failure_test_previews: list[GithubTestInfo]
    pending_test_previews: list[GithubTestInfo]

    @property
    def total_tests(self) -> int:
        return (
            self.total_success_tests
            + self.total_pending_tests
            + self.total_failure_tests
        )

    @property
    def remaining_pending_tests(self) -> int:
        return self.total_pending_tests - len(self.pending_test_previews)

    @property
    def remaining_failure_tests(self) -> int:
        return self.total_failure_tests - len(self.failure_test_previews)


def get_pr_test_preview(
    pull_request: PullRequest,
    test_statuses: list[GithubTestStatus],
    required_tests: list[GithubTest],
    acceptable_statuses_map: dict[int, list[TestStatus]],
    *,
    botpr: bool = False,
) -> PRTestPreview:
    """
    Get a short-form summary of the test statuses for a PR.
    """
    summary = checks.required_test_summary(
        pull_request.repo,
        test_statuses,
        required_tests,
        acceptable_statuses_map,
        botpr=botpr,
    )

    failure: list[GithubTestInfo] = []
    pending: list[GithubTestInfo] = []
    success: list[GithubTestInfo] = []
    for test_info in summary.tests:
        if test_info.result == "pending":
            pending.append(test_info)
        elif test_info.result == "success":
            success.append(test_info)
        elif test_info.result == "failure":
            failure.append(test_info)
        else:
            TE.assert_never(test_info.result)

    preview = PRTestPreview(
        total_success_tests=len(success),
        total_pending_tests=len(pending),
        total_failure_tests=len(failure),
        pending_test_previews=pending[:MAX_PENDING_DISPLAY],
        failure_test_previews=failure[:MAX_FAILURE_DISPLAY],
    )
    return preview


@profile_util.profile_call
def get_many_pr_test_previews(
    repo: GithubRepo,
    pull_requests: list[PullRequest],
    for_bot_prs: bool = False,
) -> dict[int, PRTestPreview | None]:
    """
    Gets test statuses for the given PRs. This function makes queries in batches
    so is much more efficient than the equivalent number of get_pr_test_preview calls

    :param for_bot_prs: If true, find the test statuses for the associated bot prs.
    """
    if not pull_requests:
        return {}

    # We first collect all the required tests for the PRs and the
    # acceptable statuses for those tests.
    test_status_map: dict[int, PRTestPreview | None] = {}

    required_tests = repo.get_required_tests(botpr=for_bot_prs)
    acceptable_statuses_map = get_acceptable_statuses_map(
        [t.id for t in required_tests], for_override=for_bot_prs
    )

    # Find all the SHAs for the associated PRs, so we can make queries
    # in batches.
    if for_bot_prs:
        # fetch the head SHAs of bot PRs associated with the PRs.
        bot_pr_map = BotPr.get_many_latest_bot_pr(
            [pr.id for pr in pull_requests], account_id=repo.account_id
        )
        pr_sha_map = {
            pr_id: str(bot_pr.head_commit_sha) for pr_id, bot_pr in bot_pr_map.items()
        }
    else:
        pr_sha_map = {pr.id: str(pr.head_commit_sha) for pr in pull_requests}

    # Get all the test statuses for the PRs grouped by their SHA.
    # This helps avoid making multiple calls to the database.
    sha_list = list(pr_sha_map.values())
    github_test_statuses = _get_github_test_statuses(repo, sha_list)

    for pr in pull_requests:
        if for_bot_prs and pr.id not in pr_sha_map:
            # if no bot PR, then return no results
            test_status_map[pr.id] = None
            continue

        sha = pr_sha_map[pr.id]
        test_statuses = github_test_statuses.get(sha, [])

        test_status_map[pr.id] = get_pr_test_preview(
            pr,
            test_statuses,
            required_tests,
            acceptable_statuses_map,
            botpr=for_bot_prs,
        )
    return test_status_map


def _get_github_test_statuses(
    repo: GithubRepo,
    sha_list: list[str],
) -> dict[str, list[GithubTestStatus]]:
    """
    Get the most recent GithubTestStatuses for the given list of SHAs.
    It returns the dictionary of sha -> list of GithubTestStatuses.

    If a test has multiple statuses, it will only return the most recent.
    """
    # note: sort by created.asc so when we create status_dict, we get the most recent GithubTestStatus
    github_test_statuses = db.session.scalars(
        sa.select(GithubTestStatus)
        .where(
            GithubTestStatus.repo_id == repo.id,
            GithubTestStatus.head_commit_sha == sa.func.any(sha_list),
            GithubTestStatus.deleted.is_(False),
        )
        # After this, required_test_summary would access github_test.name. Load them
        # here to avoid N+1 queries.
        .options(joinedload(GithubTestStatus.github_test))
        .order_by(GithubTestStatus.created.asc()),
    ).all()
    result_map: dict[str, dict[int, GithubTestStatus]] = {}
    for gts in github_test_statuses:
        if gts.head_commit_sha not in result_map:
            result_map[gts.head_commit_sha] = {}
        result_map[gts.head_commit_sha][gts.github_test_id] = gts

    result = {}
    for sha, statuses in result_map.items():
        result[sha] = list(statuses.values())
    return result


def get_test_statuses_for_pr(pr: PullRequest) -> list[GithubTestInfo]:
    """
    Gets test statuses for the given pr. This method does not support bot_pr.
    """
    repo = pr.repo
    sha = str(pr.head_commit_sha)
    github_test_status_map = _get_github_test_statuses(repo, [sha])
    required_tests = repo.get_required_tests()
    test_ids = [t.id for t in required_tests]
    acceptable_statuses_map = get_acceptable_statuses_map(test_ids, for_override=False)
    combined = checks.required_test_summary(
        repo,
        github_test_status_map.get(sha, []),
        required_tests,
        acceptable_statuses_map,
        botpr=False,
    )
    return combined.tests


def update_test_statuses_for_botpr(botpr: BotPr) -> None:
    repo = botpr.repo
    sha = str(botpr.head_commit_sha)
    github_test_status_map = _get_github_test_statuses(repo, [sha])
    required_tests = repo.get_required_tests(botpr=True)
    test_ids = [t.id for t in required_tests]
    acceptable_statuses_map = get_acceptable_statuses_map(test_ids, for_override=True)
    combined = checks.required_test_summary(
        repo,
        github_test_status_map.get(sha, []),
        required_tests,
        acceptable_statuses_map,
        botpr=True,
    )
    update_test_statuses_from_summary(combined, botpr)


def update_test_statuses_from_summary(
    combined: checks.CombinedTestSummary,
    botpr: BotPr,
) -> None:
    # Background:
    # We used to use len(success_patterns) here, but this won't reflect the individual
    # CI checks (multiple CI checks are combined into one test pattern).
    # Using success_urls won't reflect the missing patterns.
    #
    # combined.tests has the individual CI checks + if there are missing patterns, they
    # are added to that list as a fake 'test'. This should represent the Bot PR test
    # status as accurately as possible.
    botpr.passing_test_count = 0
    botpr.pending_test_count = 0
    botpr.failing_test_count = 0
    for test in combined.tests:
        match test.result:
            case "success":
                botpr.passing_test_count += 1
            case "pending":
                botpr.pending_test_count += 1
            case "failure":
                botpr.failing_test_count += 1
    db.session.commit()


def get_github_test_from_url(
    repo_id: int, url: str, sha: str, github_test_id: int
) -> GithubTestStatus | None:
    return db.session.scalar(
        sa.select(GithubTestStatus)
        .where(
            GithubTestStatus.head_commit_sha == sha,
            GithubTestStatus.repo_id == repo_id,
            GithubTestStatus.github_test_id == github_test_id,
            GithubTestStatus.job_url == url,
        )
        .order_by(GithubTestStatus.created.desc())
    )