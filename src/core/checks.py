from __future__ import annotations

import dataclasses
import datetime
import fnmatch
from collections.abc import Iterable

import structlog
import structlog.contextvars
import typing_extensions as TE

from core import (
    client,
    common,
    custom_checks,
    gh_rest,
    pygithub,
)
from core.models import GithubRepo, GithubTest, GithubTestStatus, TestStatus
from util import time_util

logger = structlog.stdlib.get_logger()

# IMPORTANT TERMINOLOGY NOTE:
# There are many similar words that we use with slightly different meanings:
#   - `status` can refer to the `status` field of a commit status (which
#     describes whether the status is pending, success, or failure)
#   - `status` can also refer to the `status` field of a check run (which
#     describes whether the check run is queued, in progress, or completed).
#   - `conclusion` refers to the `conclusion` field of a check run (which
#     describes whether the check run is success, failure, or neutral).
#   - `result` describes what Aviator/MergeQueue thinks of the commit status/
#     check run (e.g., whether it is success, failure, or pending). This
#     requires knowledge of the repo configuration (since, e.g., the repo might
#     be configured to allow a `skipped` conclusion to be considered a success
#     on one check run but not another).
# TODO: This should be an enum rather than a literal
TestResult = TE.Literal["success", "pending", "failure"]

EMOJI_SUCCESS = "✅"
EMOJI_PENDING = "⏳"
EMOJI_FAILURE = "❌"
EMOJI_UNKNOWN = "❓"


@dataclasses.dataclass
class CombinedTestSummary:
    """
    The overall status of a PR's test suite.
    """

    #: The overall result of the test suite.
    #: This is success if all the required tests are passing, failure if any of
    #: the required tests are failing, and ``pending`` otherwise (i.e., if there
    #: is at least one required test that is pending and none that are failing).
    result: TestResult

    tests: list[GithubTestInfo]

    success_patterns: set[str]
    pending_patterns: set[str]
    missing_patterns: set[str]
    failure_patterns: set[str]

    #: A mapping from the name of a successful check to its URL (if any).
    success_urls: dict[str, str]

    #: A mapping from the name of a pending check to its URL (if any).
    pending_urls: dict[str, str]

    #: A mapping from the name of a failed check to its URL (if any).
    failure_urls: dict[str, str]

    #: A human-readable description (if any) of the test status.
    reason: str | None = None

    @property
    def check_urls(self) -> dict[str, str]:
        """
        The URLs of the checks that correspond to the overall test status.

        This is mostly just here to make it compatible with existing code.
        """
        if self.result == "success":
            return self.success_urls
        elif self.result == "pending":
            return self.pending_urls
        elif self.result == "failure":
            return self.failure_urls
        else:
            TE.assert_never(self.result)


@dataclasses.dataclass
class GithubTestInfo:
    #: The name of the test.
    #: For check runs (https://docs.github.com/en/rest/checks/runs), this
    #: corresponds to the ``name`` property.
    #: For commit statuses (https://docs.github.com/en/rest/commits/statuses),
    #: this corresponds to the ``context`` property.
    name: str

    #: The underlying GithubTestStatus object.
    #: Can be None if this GithubTestInfo corresponds to a test that is missing
    #: (not yet reported by GitHub but required to merge).
    github_test_status: GithubTestStatus | None

    #: The result of the test (i.e., if Aviator considers it a success).
    #: This can be ``success`` even if the status is not ``success`` (e.g., if
    #: the repo is configured to allow a ``skipped`` conclusion on a check run
    #: to be considered a success).
    result: TestResult

    #: The repo test patterns that matched this test.
    #: For example, if the repo is configured to require ``lint-*`` and
    #: ``test-*``, and this test has the name ``test-golang``, then this will be
    #: ``{"test-*"}``.
    test_patterns: set[str]

    #: The raw GitHub status of the test.
    #: Most places should consult the ``result`` field instead.
    gh_status: TestStatus

    @property
    def is_success(self) -> bool:
        """
        Whether the test is considered successful.
        """
        return self.result == "success"

    #: The URL (if any) of the test in the external system (e.g., a link to the
    #: CircleCI report).
    url: str | None

    @property
    def status_emoji(self) -> str:
        if self.result == "success":
            return EMOJI_SUCCESS
        elif self.result == "pending":
            # "unknown" is generally a sub-case of pending, but it usually
            # indicates that the test has not been reported (often because it
            # hasn't run on the PR, e.g., because the conditions to run the test
            # weren't met). It makes sense to display a different emoji for
            # that so that it's clear that it (likely) won't just be fixed by
            # waiting long enough.
            if self.gh_status is TestStatus.Unknown:
                return EMOJI_UNKNOWN
            return EMOJI_PENDING
        elif self.result == "failure":
            return EMOJI_FAILURE
        else:
            TE.assert_never(self.result)


def required_test_summary(
    repo: GithubRepo,
    test_statuses: list[GithubTestStatus],
    required_tests: list[GithubTest],
    acceptable_statuses_map: dict[int, list[TestStatus]],
    *,
    botpr: bool,
) -> CombinedTestSummary:
    """
    Determine the overall status of a PR's test suite with respect to the
    required tests configured with Aviator.

    The overall status is determined by the following rules:
      - If any failing tests match the required test patterns, then the overall
        status is failure.
      - If any pending tests match the required test patterns, then the overall
        status is pending.
      - If there are any required tests patterns with no matching test results,
        then the overall status is pending.
      - Otherwise, the overall status is success.

    Essentially, this means that we require two things for a set of tests to be
    considered successful:
      - No required test may be pending or failing.
      - Every configured test pattern must match at least one successful test.

    :param repo: The repo that the PR is in.
    :param test_statuses: The statuses of the tests that have been run on
        this PR at the head SHA.
    :param required_tests: The required tests configured for the repo.
    :param acceptable_statuses_map: A mapping from the ID of a GitHubTest to the
        override acceptable statuses configured for that test.
    """
    required_patterns_map: dict[str, int] = {
        test.name: test.id for test in required_tests
    }
    require_all_checks_pass = (
        repo.parallel_mode_config.require_all_draft_checks_pass
        if botpr
        else repo.require_all_checks_pass
    )
    if require_all_checks_pass:
        required_patterns_map["*"] = -1

    test_infos: list[GithubTestInfo] = []

    success_patterns: set[str] = set()
    failure_patterns: set[str] = set()
    pending_patterns: set[str] = set()
    success_ci_urls: dict[str, str] = {}
    failure_ci_urls: dict[str, str] = {}
    pending_ci_urls: dict[str, str] = {}
    parsed_test_names: set[str] = set()

    for test_status in sorted(
        test_statuses,
        # Order by status_updated_at first, then use fetched_at to break tie by
        # using tuple of two datetimes.
        key=lambda st: (
            time_util.ensuze_tz(st.status_updated_at or datetime.datetime.min),
            time_util.ensuze_tz(st.fetched_at or datetime.datetime.min),
        ),
        reverse=True,
    ):
        test_name = test_status.github_test.name

        # Skip the duplicate runs of the same tests. Since we read the results
        # in reverse chronological order, we will just skip any tests that we've
        # already seen. This ensures we don't qualify a stale run of the same test.
        # This is what GitHub already does internally to calculate mergeability.
        if test_name in parsed_test_names:
            continue
        parsed_test_names.add(test_name)

        # Check if this is a required test (and skip it if not).
        # A given test may match more than one pattern, so here we're operating
        # on a set of patterns rather than a single string.
        patterns = list(get_matching_patterns(required_patterns_map.keys(), test_name))
        if not patterns:
            continue

        # If there are multiple patterns that match this test, we take the longest one
        # as the best match.
        patterns.sort(key=len, reverse=True)
        best_match_pattern = patterns[0]
        matching_test_id = required_patterns_map[best_match_pattern]
        success_statuses = acceptable_statuses_map.get(matching_test_id)
        if not success_statuses:
            success_statuses = list(custom_checks.DEFAULT_SUCCESS_STATES)

        job_url = test_status.job_url or ""
        test_result: TestResult
        if test_status.status in success_statuses:
            test_result = "success"
            success_patterns.add(best_match_pattern)
            success_ci_urls[test_name] = job_url
        elif test_status.status is TestStatus.Pending:
            test_result = "pending"
            pending_patterns.add(best_match_pattern)
            pending_ci_urls[test_name] = job_url
        else:
            test_result = "failure"
            failure_patterns.add(best_match_pattern)
            failure_ci_urls[test_name] = job_url

        test_info = GithubTestInfo(
            name=test_name,
            github_test_status=test_status,
            test_patterns={best_match_pattern},
            result=test_result,
            gh_status=test_status.status,
            url=job_url,
        )
        test_infos.append(test_info)

    missing_patterns = (
        required_patterns_map.keys()
        - success_patterns
        - pending_patterns
        - failure_patterns
    )

    # If a pattern has no matching test cases, we always mark the PR as pending
    # before proceeding.
    # BUT, AS A SPECIAL CASE:
    # If a pattern lists "pending" as an acceptable status, we shouldn't block
    # even if there are no matching tests reported by GitHub.
    # This feature was added to support a workflow where Figma wanted to run a
    # job after merging (and therefore didn't want to block merging the PR on
    # waiting for that status to be reported).
    acceptable_missing_patterns = {
        test.name
        for test in required_tests
        if TestStatus.Missing in acceptable_statuses_map.get(test.id, [])
    }
    missing_patterns -= acceptable_missing_patterns
    success_patterns.update(acceptable_missing_patterns)

    overall_result: TestResult
    if failure_patterns:
        # Failure is the highest priority state here.
        overall_result = "failure"
    elif pending_patterns or missing_patterns:
        overall_result = "pending"
    else:
        overall_result = "success"

    reason: str | None = None
    if missing_patterns:
        # Sort the missing_patterns to make things more deterministic.
        missing = sorted(missing_patterns)

        for pattern in missing:
            pending_ci_urls[pattern] = ""
            test_infos.append(
                GithubTestInfo(
                    name=pattern,
                    github_test_status=None,
                    test_patterns={pattern},
                    result="pending",
                    gh_status=TestStatus.Missing,
                    url=None,
                )
            )

        # Provide a slightly better error message here.
        # We should probably deprecate `require_all_checks_pass` in favor of
        # just using a "*" pattern in the future.
        if missing == ["*"] and require_all_checks_pass:
            reason = "At least one test must be present when `require_all_checks_pass` is enabled."
        else:
            reason = "At least one test must be present for wildcard: " + ", ".join(
                f"`{pattern}`" for pattern in missing
            )

    combined = CombinedTestSummary(
        result=overall_result,
        tests=test_infos,
        success_patterns=success_patterns,
        pending_patterns=pending_patterns,
        missing_patterns=missing_patterns,
        failure_patterns=failure_patterns,
        success_urls=success_ci_urls,
        failure_urls=failure_ci_urls,
        pending_urls=pending_ci_urls,
        reason=reason,
    )
    return combined


def _ensure_github_test(
    repo: GithubRepo,
    known_tests: dict[str, GithubTest],
    test_name: str,
) -> GithubTest | None:
    """
    Helper function to ensure a test exists in the database.
    """
    github_test = known_tests.get(test_name, None)
    if not github_test:
        github_test = common.ensure_github_test(repo, test_name)
    if not github_test:
        logger.error(
            "Failed to ensure github test %s for repo %d",
            test_name,
            repo.id,
        )
        return None
    return github_test


def fetch_latest_test_statuses(
    client: client.GithubClient,
    repo: GithubRepo,
    pull: pygithub.PullRequest,
    *,
    botpr: bool,
) -> CombinedTestSummary:
    """
    Fetch the latest test statuses from GitHub.
    """
    with structlog.contextvars.bound_contextvars(
        repo_id=repo.id,
        pr_number=pull.number,
        head=pull.head.sha,
        botpr=botpr,
    ):
        logger.info("Fetching latest commit statuses/check runs from GitHub")

        commit = common.ensure_commit(repo.id, pull.head.sha)

        # Load all the tests up front to prevent querying for every loop iteration.
        known_tests = {test.name: test for test in GithubTest.all_for_repo(repo.id)}

        fetched_statuses = _fetch_latest_commit_statuses(
            client, repo, commit, known_tests
        )
        fetched_statuses.extend(
            _fetch_latest_check_runs(client, repo, commit, known_tests)
        )
        test_statuses = custom_checks.update_test_statuses(
            repo=repo,
            commit=commit,
            fetched_at=time_util.now(),
            fetched_statuses=fetched_statuses,
        )

        required_tests = repo.get_required_tests(botpr=botpr)
        test_ids = [test.id for test in required_tests]
        acceptable_statuses_map = custom_checks.get_acceptable_statuses_map(
            test_ids, for_override=botpr
        )
        return required_test_summary(
            repo, test_statuses, required_tests, acceptable_statuses_map, botpr=botpr
        )


def _fetch_latest_commit_statuses(
    client: client.GithubClient,
    repo: common.GithubRepo,
    commit: common.GitCommit,
    known_tests: dict[str, GithubTest],
) -> list[custom_checks.FetchedTestStatus]:
    # Get the statuses for the check and update their statuses in the database.
    commit_statuses: list[custom_checks.GHCommitStatus] = (
        custom_checks.fetch_gh_commit_statuses(
            client.access_token,
            repo.name,
            commit.sha,
            account_id=repo.account_id,
        )
    )
    fetched_statuses: list[custom_checks.FetchedTestStatus] = []
    for commit_status in commit_statuses:
        # This method is optimized to reduce the number of queries to the db.
        github_test = _ensure_github_test(repo, known_tests, commit_status.context)
        if not github_test:
            continue
        fetched_statuses.append(
            custom_checks.FetchedTestStatus.from_status(github_test, commit_status)
        )
    return fetched_statuses


def _fetch_latest_check_runs(
    client: client.GithubClient,
    repo: common.GithubRepo,
    commit: common.GitCommit,
    known_tests: dict[str, GithubTest],
) -> list[custom_checks.FetchedTestStatus]:
    # Get all the check runs for the commit (excluding our own custom check)
    # and update their statuses in the database.
    try:
        check_runs: list[custom_checks.GHCheckRun] = custom_checks.fetch_gh_check_runs(
            client.access_token,
            repo.name,
            commit.sha,
            account_id=repo.account_id,
        )
    except gh_rest.GHForbiddenError as exc:
        # NB: Some very old installations of the Aviator app do not have the
        # checks permission and this API call will always fail. In that case, we
        # just skip the check runs.
        logger.warning(
            "Failed to fetch check runs from GitHub",
            exc_info=exc,
        )
        return []

    fetched_statuses: list[custom_checks.FetchedTestStatus] = []
    for check_run in check_runs:
        if common.is_av_custom_check(check_run.name):
            continue
        # This method is optimized to reduce the number of queries to the db.
        github_test = _ensure_github_test(repo, known_tests, check_run.name)
        if not github_test:
            continue
        fetched_statuses.append(
            custom_checks.FetchedTestStatus.from_checkrun(github_test, check_run)
        )
    return fetched_statuses


def _group_globs(
    patterns: Iterable[str],
    elts: Iterable[str],
) -> dict[str, list[str]]:
    """
    Group elements by glob patterns.

    Patterns with no matches are omitted from the result dict.

    >>> _group_globs(["*.py", "*.txt", "*.md"], ["foo.py", "bar.txt", "baz.py"])
    {'*.py': ['foo.py', 'baz.py'], '*.txt': ['bar.txt']}
    """
    matched: dict[str, list[str]] = dict()
    for pattern in patterns:
        for elt in elts:
            if fnmatch.fnmatch(elt, pattern):
                matched.setdefault(pattern, []).append(elt)
    return matched


def get_matching_patterns(patterns: Iterable[str], elt: str) -> set[str]:
    """
    Get the patterns that match at least one element.

    >>> matches = get_matching_patterns(["*.py", "*.txt", "*.md"], "foo.py")
    >>> assert matches == {"*.py"}, f"Got {matches}"
    """
    return {pattern for pattern in patterns if fnmatch.fnmatch(elt, pattern)}
