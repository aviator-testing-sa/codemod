from __future__ import annotations

import dataclasses
import datetime
import json
import random
import time
import traceback
import urllib.parse
from os import path
from typing import Any

import pydantic
import requests
import structlog
import typing_extensions as TE
from dateutil.parser import parse

# TODO: Can we just use requests.adapters.Retry?
from urllib3.util.retry import Retry

import errors
import instrumentation
import schema
from auth.models import AccessToken
from core import connect, custom_checks, gh_rest, models, pygithub, rate_limit
from core.commit import get_updated_body
from core.graphql import GithubGql, GithubGqlNoDataException, GithubGqlNotFoundException
from core.models import GithubRepo, RegexConfig
from core.native_git import NativeGit
from errors import MergeConflictException
from lib.niche_git import NicheGitClient
from main import app
from schema.gh_rest.comment import IssueComment

logger = structlog.stdlib.get_logger()
DEFAULT_BRANCH_PREFIX = "mq-bot-"

# Non-official HTTP status code https://http.dev/598
_HTTP_NETWORK_READ_TIMEOUT_ERROR = 598

_github_api_call_latency = instrumentation.Histogram(
    "github_api_call_latency",
    "Time taken by GitHub API calls",
    ["status", "method"],
)


class CustomSuperHackyGithub(pygithub.Github):
    def __init__(self, account_id: int, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        original_requestJson = self._Github__requester.requestJson  # type: ignore[attr-defined]

        # Signature copied from PyGithub. The naming convention is odd here
        # because of that. We usually use snake_case.
        # https://github.com/PyGithub/PyGithub/blob/v1.59.0/github/Requester.py#L820
        def log(
            verb: str,
            url: str,
            requestHeaders: dict[str, str],
            input: Any | None,
            status: int | None,
            responseHeaders: dict[str, Any],
            output: str | None,
        ) -> None:
            # Do nothing. We handle logging in the requestJson method below.
            return

        def requestJson(
            verb: str,
            url: str,
            parameters: dict[str, Any] | None = None,
            headers: dict[str, Any] | None = None,
            input: object | None = None,
            cnx: object | None = None,
        ) -> Any:
            stack = traceback.extract_stack()
            caller = []
            for frame in stack:
                if "core/" in frame.filename and "client.py" not in frame.filename:
                    caller.append(
                        f"{path.basename(frame.filename)}:{frame.lineno}:{frame.name}"
                    )

            start = time.monotonic()
            try:
                headers = headers or {}
                # We need to set this to force GitHub to return the new-style
                # global Node IDs (since we sometimes use those to uniquely
                # identify objects). See:
                # https://docs.github.com/en/graphql/guides/migrating-graphql-global-node-ids
                # (note the doc only talks about GraphQL, but this header also
                # works for the REST API).
                headers["X-Github-Next-Global-ID"] = "1"
                http_status_code, response_headers, output = original_requestJson(
                    verb, url, parameters, headers, input, cnx
                )
                elapsed = time.monotonic() - start
                remaining, limit = self._Github__requester.rate_limiting  # type: ignore[attr-defined]

                # HTTP headers are case insensitive, but this is coming as a dict.
                # The keys apparently are stored in the lower case.
                github_request_id = response_headers.get("x-github-request-id", None)
                if 200 <= http_status_code and http_status_code < 300:
                    logger.info(
                        "GitHub REST API request (success)",
                        github_api_verb=verb,
                        github_api_url=url,
                        github_api_status=http_status_code,
                        github_request_id=github_request_id,
                        github_rest_rate_limit_remaining=remaining,
                        github_rest_rate_limit_limit=limit,
                        elapsed=elapsed,
                        caller=caller,
                    )
                else:
                    logger.info(
                        "GitHub REST API request (failure)",
                        github_api_verb=verb,
                        github_api_url=url,
                        github_api_status=http_status_code,
                        github_request_id=github_request_id,
                        github_rest_rate_limit_remaining=remaining,
                        github_rest_rate_limit_limit=limit,
                        github_rest_error_output=output[:1000],
                        elapsed=elapsed,
                        caller=caller,
                    )
                _github_api_call_latency.labels(
                    status=http_status_code, method=verb
                ).observe(elapsed)
                rate_limit.set_gh_rest_api_limit(account_id, remaining, limit)
                return http_status_code, response_headers, output
            except ConnectionError as exc:
                # There is a TCP connection level error. By converting this
                # ConnectionError to GithubException, the caller can handle this
                # same as other GitHub errors.
                elapsed = time.monotonic() - start
                logger.info(
                    "GitHub REST API request (connection failure)",
                    github_api_verb=verb,
                    github_api_url=url,
                    elapsed=elapsed,
                    caller=caller,
                )
                _github_api_call_latency.labels(
                    status=_HTTP_NETWORK_READ_TIMEOUT_ERROR, method=verb
                ).observe(elapsed)
                raise pygithub.GithubException(
                    _HTTP_NETWORK_READ_TIMEOUT_ERROR, {}, None
                ) from exc

        self._Github__requester.requestJson = requestJson  # type: ignore[attr-defined]
        self._Github__requester._Requester__log = log  # type: ignore[attr-defined]


class GithubClient:
    def __init__(self, access_token: AccessToken, repo: GithubRepo):
        self.db_repo = repo
        self._client = CustomSuperHackyGithub(
            access_token.account_id,
            auth=pygithub.Auth.Token(access_token.token),
            per_page=20,
            timeout=app.config["REQUEST_TIMEOUT_SEC"],
            base_url=app.config["GITHUB_API_BASE_URL"],
            retry=Retry(
                total=app.config["REQUEST_RETRY_COUNT"],
                backoff_factor=app.config["REQUEST_RETRY_BACKOFF"],
            ),
        )
        self._access_token = access_token
        self.repo = self.client.get_repo(repo.name, lazy=True)

    _access_token: AccessToken
    _client: pygithub.Github

    def _refresh_token_if_necessary(self) -> bool:
        old_token = self._access_token.token
        token = connect.ensure_access_token(self._access_token)
        if not token:
            raise errors.AccessTokenException("failed to refresh access token")
        self._access_token = token
        did_refresh = old_token != token.token
        if did_refresh:
            logger.info(
                "Repo %d refreshed access token for installation %d",
                self.db_repo.id,
                self._access_token.installation_id,
            )
        return did_refresh

    @property
    def client(self) -> pygithub.Github:
        if self._refresh_token_if_necessary():
            # HACKHACKHACK
            # We have to monkey-patch the requester object because all of the
            # objects that PyGithub returns embed the requester in case they
            # need to make other API calls (e.g., every `PullRequest` object
            # can then have methods like pull.create_comment called) which would
            # use an outdated token without this.
            self._client._Github__requester._Requester__authorizationHeader = (  # type: ignore[attr-defined]
                f"token {self._access_token.token}"
            )

        return self._client

    @property
    def gql_client(self) -> GithubGql:
        return GithubGql(self.access_token, self.db_repo.account_id)

    @property
    def niche_git_client(self) -> NicheGitClient:
        return NicheGitClient(self.db_repo.html_url, self.access_token)

    @property
    def access_token(self) -> str:
        self._refresh_token_if_necessary()
        return self._access_token.token

    def get_pull(self, pull_number: int) -> pygithub.PullRequest:
        self._refresh_token_if_necessary()
        return self.repo.get_pull(pull_number)

    # TODO: Client should not depend on any models
    def get_completed_checks(
        self, pull: pygithub.PullRequest
    ) -> tuple[list[pygithub.CommitStatus], list[custom_checks.GHCheckRun]]:
        self._refresh_token_if_necessary()
        commit = self.repo.get_commit(pull.head.sha)
        return self.get_checks_on_commit(commit)

    def get_checks_on_commit(
        self, commit: pygithub.Commit
    ) -> tuple[list[pygithub.CommitStatus], list[custom_checks.GHCheckRun]]:
        self._refresh_token_if_necessary()
        combined_status = commit.get_combined_status()
        completed_statuses = [
            status
            for status in combined_status.statuses
            if status.state in ["failure", "error", "success"]
        ]

        check_runs = custom_checks.fetch_gh_check_runs(
            self.access_token,
            self.repo.full_name,
            commit.sha,
            account_id=self.db_repo.account_id,
        )
        completed_checks = [
            check for check in check_runs if check.status == "completed"
        ]
        return completed_statuses, completed_checks

    # TODO: this doesn't make an API call so shouldn't be in client
    def should_skip_delete(
        self, pull: pygithub.PullRequest, skip_delete_labels: list[str]
    ) -> bool:
        labels = [label.name for label in pull.labels]
        return any([label in skip_delete_labels for label in labels])

    # TODO: this doesn't make an API call so shouldn't be in client
    # TODO: Client should not depend on any models
    def is_auto_sync(
        self, pull: pygithub.PullRequest, auto_sync_label: models.GithubLabel
    ) -> bool:
        labels = [label.name for label in pull.labels]
        return auto_sync_label.name in labels

    def add_label(self, pull: pygithub.PullRequest, label: str) -> None:
        # HACK: Check if the pull object was artificially created.
        if not pull.raw_headers:
            pull = self.get_pull(pull.number)
        pull.add_to_labels(label)

    def update_pull(self, pull: pygithub.PullRequest) -> None:
        """
        Returns true of the branch was updated
        """
        if not pull.raw_headers:
            pull = self.get_pull(pull.number)

        # Regardless of whether the branch was updated or not, this method should not raise.
        # It only raises an HTTP exception if the method failed to process.
        # We will let that message bubble up to make sure we don't make assumptions
        # for the state of the branch.
        result = gh_rest.put(
            f"repos/{self.repo.full_name}/pulls/{pull.number}/update-branch",
            data={},
            return_type=UpdateBranch,
            token=self.access_token,
            account_id=self.db_repo.account_id,
        )
        logger.info(
            "Updating pull request, received message",
            message=result.message,
            pr_number=pull.number,
            repo_id=self.db_repo.id,
        )

    def remove_label(self, pull: pygithub.PullRequest, label: str) -> None:
        # HACK: Check if the pull object was artificially created.
        if not pull.raw_headers:
            pull = self.get_pull(pull.number)
        pull.remove_from_labels(label)

    def remove_label_by_pr_number(self, pull_number: int, label: str) -> None:
        try:
            gh_rest.delete(
                f"repos/{self.repo.full_name}/issues/{pull_number}/labels/{label}",
                token=self.access_token,
                account_id=self.db_repo.account_id,
            )
        except requests.HTTPError as e:
            logger.info(
                "Failed to remove label",
                pull_number=pull_number,
                label=label,
                status_code=e.response.status_code,
            )
            if 400 < e.response.status_code < 500:
                # Label not found, ignore
                return
            raise e

    def create_issue_comment(self, pr_number: int, body: str) -> IssueComment:
        return gh_rest.post(
            f"repos/{self.db_repo.name}/issues/{pr_number}/comments",
            {"body": body},
            IssueComment,
            token=self.access_token,
            account_id=self.db_repo.account_id,
        )

    def upsert_issue_comment(
        self, pr_number: int, body: str, node_id: str | None
    ) -> str:
        """Create or update the issue comment."""
        if node_id:
            return self.update_issue_comment(pr_number, body, node_id)
        else:
            return self.create_issue_comment(pr_number, body).node_id

    def update_issue_comment(self, pr_number: int, body: str, node_id: str) -> str:
        """Update the PR issue comment and return the node ID."""
        res = self.gql_client.fetch(
            """
            mutation updateIssueComment($input: UpdateIssueCommentInput!) {
              updateIssueComment(input: $input) {
                issueComment {
                  id
                }
              }
            }
            """,
            input={
                "id": node_id,
                "body": body,
            },
        )
        try:
            res.raise_for_error()
            data = res.must_response_data()
            return str(data["updateIssueComment"]["issueComment"]["id"])
        except GithubGqlNotFoundException:
            # This error is what we would get if sticky comment was deleted
            # so remake the sticky comment directly
            logger.info("No sticky comment found on PR", exc=res.errors)
            create_res = self.create_issue_comment(pr_number, body)
            return str(create_res.node_id)
        except GithubGqlNoDataException as exc:
            # This should not happen after already checking errors but let's
            # double-check
            logger.error(
                "No sticky comment found",
                exc_info=exc,
            )
            raise

    def create_label(self, name: str, color: str) -> None:
        try:
            self.repo.create_label(name, color)
        except Exception as e:
            logger.info("Failed creating label", name=name, exc_info=e)

    def create_review_request(self, pr_number: int, reviewer_logins: list[str]) -> None:
        gh_rest.post(
            f"repos/{self.db_repo.name}/pulls/{pr_number}/requested_reviewers",
            {"reviewers": reviewer_logins},
            dict,
            token=self.access_token,
            account_id=self.db_repo.account_id,
        )

    def remove_review_request(
        self, pr_number: int, reviewer_logins: list[str], team_slugs: list[str]
    ) -> None:
        gh_rest.delete_with_data(
            f"repos/{self.db_repo.name}/pulls/{pr_number}/requested_reviewers",
            {"reviewers": reviewer_logins, "team_reviewers": team_slugs},
            dict,
            token=self.access_token,
            account_id=self.db_repo.account_id,
        )

    # TODO: this doesn't make an API call so shouldn't be in client
    def is_approved(
        self,
        pull: pygithub.PullRequest,
        approvals: int | None,
        required_approvers: list[str] | None = None,
    ) -> bool:
        if not required_approvers:
            required_approvers = []
        review_map = {}
        reviews = pull.get_reviews()  # these are chronologically ordered
        most_recent_review = ""
        # get latest review from each reviewer
        for review in reviews:
            if review.state in ["APPROVED", "CHANGES_REQUESTED"]:
                review_map[review.user.login] = review.state
                most_recent_review = review.state

        # only flag if the most recent review is changes requested
        if most_recent_review == "CHANGES_REQUESTED":
            return False

        approve_set = [key for key in review_map if review_map[key] == "APPROVED"]
        if approvals is None:
            approvals = 1
        if len(approve_set) < approvals:
            return False

        # either no required approver or at least one reqd approver has approved.
        return len(required_approvers) == 0 or any(
            [ra in approve_set for ra in required_approvers]
        )
def get_test_list(self, sha: str) -> list[str]:
        combined_statuses = custom_checks.fetch_gh_commit_statuses(
            self.access_token,
            self.repo.full_name,
            sha,
            account_id=self.db_repo.account_id,
        )
        test_list = [status.context for status in combined_statuses]
        try:
            check_runs = custom_checks.fetch_gh_check_runs(
                self.access_token,
                self.repo.full_name,
                sha,
                account_id=self.db_repo.account_id,
            )
            test_list += [cr.name for cr in check_runs]
        except Exception as e:
            logger.warning(
                "Failed to fetch check runs", repo_id=self.db_repo.id, exc_info=e
            )
        return test_list

    def get_last_commit_date(self, pull: pygithub.PullRequest) -> datetime.datetime:
        try:
            commit = self.repo.get_commit(pull.head.sha)
            dt: str = commit._rawData["commit"]["committer"]["date"]
            return parse(dt)
        except Exception as e:
            logger.info(
                "Failed to fetch commit date",
                repo_id=self.db_repo.id,
                pull_number=pull.number,
                exc_info=e,
            )
            return pull.created_at.replace(tzinfo=datetime.UTC)

    # TODO: Client should not depend on any models
    def merge(
        self,
        pull: pygithub.PullRequest,
        merge_method: pygithub.MergeMethod = "squash",
        db_merge_labels: list[models.GithubLabel] | None = None,
        use_title_and_body: bool | None = True,
        commit_message: str = "",
        commit_title: str = "",
        regex_configs: list[RegexConfig] | None = None,
        stacked_pulls: list[pygithub.PullRequest] = [],
    ) -> str:
        """
        Returns back the commit sha if the merge was successful, None otherwise.
        """
        regex_configs = regex_configs or []
        if db_merge_labels:
            labels = [label.name for label in pull.labels]
            # find any of the labels matching
            for db_label in db_merge_labels:
                if db_label.name in labels:
                    merge_method = db_label.purpose.value  # type:ignore[assignment]
                    logger.info(
                        "Repo %d PR %d PullRequest merge override found %s",
                        self.db_repo.id,
                        pull.number,
                        merge_method,
                    )
                    break

        title: str = ""
        body: str = ""
        if commit_message or commit_title:
            title = commit_title or ("%s (#%d)" % (pull.title, pull.number))
            body = commit_message or pull.body or ""
        elif use_title_and_body:
            title = "%s (#%d)" % (pull.title, pull.number)
            body = get_updated_body(self.db_repo, pull)

        if title:
            for config in regex_configs:
                title = title.replace(config.pattern, config.replace)

        # Aggregate PR bodies for all stacked PRs. Ensure that we trim av-cli metadata if it exists.
        if stacked_pulls:
            for p in stacked_pulls:
                title += f"(#{p.number})"
                body += "\n"
                body += p.title + "\n" + get_updated_body(self.db_repo, p)
                body += f"\nCloses #{p.number}\n"

        try:
            merge_status: pygithub.PullRequestMergeStatus = pull.merge(
                merge_method=merge_method,
                # We would also set empty string to body to avoid getting the default
                # coauthors set by GitHub.
                commit_message=body if body or use_title_and_body else pygithub.NotSet,
                commit_title=title or pygithub.NotSet,
            )
            return merge_status.sha
        except pygithub.GithubException as gh_exc:
            if gh_exc.status != 500:
                raise
            # Workaround for spurious 500 errors while trying to merge a PR
            logger.info(
                "Repo %d PR %d merge failed with REST API, trying with GraphQL",
                self.db_repo.id,
                pull.number,
            )
            try:
                return self.gql_client.merge_pull_request(
                    pull._rawData["node_id"],
                    merge_method=merge_method.upper(),
                    commit_headline=title,
                    commit_body=body,
                )
            except Exception as e:
                if _is_merge_conflict(str(e)):
                    logger.info(
                        "Repo %d PR %d merge conflict detected",
                        self.db_repo.id,
                        pull.number,
                    )
                else:
                    logger.error(
                        "Merge failed with GraphQL",
                        repo_id=self.db_repo.id,
                        pr_number=pull.number,
                        exc_info=e,
                    )
                # Raise the original pygithub.GithubException here so we can piggyback
                # on existing error handling.
                raise gh_exc

    def merge_head(self, pull: pygithub.PullRequest, base_branch: str) -> bool:
        """
        Merge the base_branch into the pull request's head branch.

        Returns true if a merge commit was created or false if the operation was
        a no-op.
        """
        if (
            pull.head.repo.full_name != self.repo.full_name
            or pull.base.ref == base_branch
        ):
            # We use the GitHub update branch API to update the PR. But since that API does
            # not tell us if the PR was updated or not, we need to check if the PR is up to
            # date before the update.
            mergeable_state = pull.mergeable_state  # clean, dirty, unknown
            if mergeable_state == "dirty":
                raise MergeConflictException("Merge conflict")
            else:
                if self.is_up_to_date(pull, base_branch):
                    logger.info(
                        "The PR is already up to date, skipping merge_head",
                        repo_id=self.db_repo.id,
                        pr_number=pull.number,
                        base_branch=base_branch,
                    )
                    return False
                else:
                    self.update_pull(pull)
                    return True
        else:
            head_branch = pull.head.ref
            merge_comment = f"Merge branch '{base_branch}' into {head_branch}"
            git_branch = self.get_branch(base_branch)
            merge_commit = self.merge_sha_to_branch(
                head_branch, git_branch.commit.sha, merge_comment
            )
            # no merge commit created if branch up to date with head_branch
            return bool(merge_commit)

    def get_branch(self, branch_name: str, attempt: int = 0) -> pygithub.Branch:
        # HACK/WORKAROUND:
        # Apparently, the GitHub client doesn't deal with URL escaping
        # properly, so we have to do it manually here. Without this, this
        # fails to get the branch if it has special characters in it.
        try:
            branch_name_quoted = urllib.parse.quote(branch_name)
            return self.repo.get_branch(branch_name_quoted)
        except pygithub.GithubException as e:
            if e.status == 404:
                if attempt < 3:
                    logger.info(
                        "git branch not found, retrying",
                        repo_id=self.db_repo.id,
                        branch=branch_name,
                    )
                    time.sleep(2 * attempt)
                    return self.get_branch(branch_name, attempt + 1)
            raise

    def merge_branch_to_branch(
        self, from_branch: str, to_branch: str
    ) -> pygithub.Commit | None:
        """
        Merge ta branch into another branch.

        Returns true if a merge commit was created or false if the operation was
        a no-op (because ``to_branch`` is already up-to-date).
        """
        git_branch = self.get_branch(from_branch)
        merge_comment = f"Merge branch '{git_branch.name}' into {to_branch}"
        merge_commit = self.merge_sha_to_branch(
            to_branch, git_branch.commit.sha, merge_comment
        )
        # MER-2117: Adding additional logging to capture the merge commit behavior.
        #  This issue is remarkably similar to MER-2109 where a commit entirely disappeared.
        if merge_commit:
            logger.info(
                "merged %s sha: %s into %s. created %s",
                from_branch,
                git_branch.commit.sha,
                to_branch,
                merge_commit.sha,
            )
        else:
            logger.info("no merge created for %s into %s", from_branch, to_branch)
        return merge_commit

    def fast_forward_merge(self, pull: pygithub.PullRequest) -> None:
        """
        Fast-forward the base branch of the pull request to the head commit of
        the pull request.
        """
        branch_name = pull.base.ref
        git_ref = self.get_git_ref(branch_name)
        commit_sha = pull.head.sha
        git_ref.edit(sha=commit_sha)

    def create_pull(
        self,
        branch: str,
        base: str,
        title: str,
        body: str,
        *,
        draft: bool | None = None,
    ) -> pygithub.PullRequest:
        return self.repo.create_pull(
            title=title,
            body=body,
            head=branch,
            base=base,
            draft=app.config["USE_DRAFT_PR"] if draft is None else draft,
        )

    def close_pull(self, pull: pygithub.PullRequest) -> None:
        self._refresh_token_if_necessary()
        if not pull.raw_headers:
            pull = self.get_pull(pull.number)
        pull.edit(state="closed")

    def delete_branch(
        self, pull: pygithub.PullRequest, skip_delete_labels: list[str]
    ) -> None:
        """
        Delete the branch of the pull request unless it has a skip-delete label.
        """
        if not self.should_skip_delete(pull, skip_delete_labels):
            self.delete_ref(pull.head.ref)

    def delete_ref(self, branch_name: str) -> None:
        try:
            gh_rest.delete(
                f"repos/{self.repo.full_name}/git/refs/heads/" + branch_name,
                token=self.access_token,
                account_id=self.db_repo.account_id,
            )
        except Exception:
            # we should never raise error for deleting a ref. It's not mission critical.
            logger.info(
                "Repo %d failed to delete branch %s, ignore and continue",
                self.db_repo.id,
                branch_name,
            )

    def rebase_pull(self, pull: pygithub.PullRequest, base_branch: str) -> bool:
        """
        Rebase a pull request against its base branch.

        Uses native Git (not GitHub APIs).

        Returns true if the rebase created a new commit or false if the rebase
        was a no-op.
        """
        self._refresh_token_if_necessary()
        old_sha = pull.head.sha
        with self.native_git(pull) as git:
            git.rebase(pull, base_branch)
        time.sleep(2)
        new_pull = self.get_pull(pull.number)
        # if old and new sha are same, it's up to date
        logger.info(
            "Repo %d rebase old sha %s new sha %s",
            self.db_repo.id,
            old_sha,
            new_pull.head.sha,
        )
        return old_sha != new_pull.head.sha

    def replicate_branch(self, last_pr_number: int, new_pr_number: int) -> str:
        self._refresh_token_if_necessary()
        pull = self.get_pull(last_pr_number)
        return self.copy_to_new_branch(pull.head.sha, str(new_pr_number))

    def create_latest_branch(
        self,
        pull: pygithub.PullRequest,
        base_branch: str,
        *,
        tmp_only: bool = False,
        prefix: str = DEFAULT_BRANCH_PREFIX,
    ) -> str:
        self._refresh_token_if_necessary()
        tmp_branch = self.copy_to_new_branch(
            pull.head.sha, str(pull.number), prefix="mq-tmp-"
        )

        try:
            merge_comment = f"Merge branch '{base_branch}' into {tmp_branch}"
            git_branch = self.get_branch(base_branch)
            merge_commit = self.merge_sha_to_branch(
                tmp_branch, git_branch.commit.sha, merge_comment
            )
            # If no merge commit exist that means branch was already up to date with base_branch.
            sha = merge_commit.sha if merge_commit else pull.head.sha
            if tmp_only:
                return tmp_branch
            target_branch = self.copy_to_new_branch(
                sha, str(pull.number), prefix=prefix
            )
        finally:
            if not tmp_only:
                self.delete_ref(tmp_branch)
        return target_branch

    def create_latest_branch_with_squash_using_tmp(
        self,
        pull: pygithub.PullRequest,
        base_branch_name: str,
        *,
        use_branch_in_title: bool = False,
        commit_post_message: str | None = None,
    ) -> tuple[str, str]:
        """
        Creates a new branch by using squash commit. This particular workflow acts like
        a no-ff option where we create a commit that is not a duplicate of the commit SHA
        from the source pull. Note that this may still push the same SHA to the tmp_branch
        if the source pull is already up to date with the base branch. But the final branch
        will always have a new SHA.
        Returns the final branch created and the new SHA at the top of the branch.
        """
        self._refresh_token_if_necessary()
        base_branch = self.get_branch(base_branch_name)
        sha = base_branch.commit.sha
        tmp_branch = self.generate_branch_name(sha, str(pull.number), prefix="mq-tmp-")
        tmp_ref = self.push_git_ref(tmp_branch, sha=sha)
        try:
            new_sha = self.create_squash_commit(
                pull,
                tmp_branch,
                use_branch_in_title=use_branch_in_title,
                commit_message_trailer=commit_post_message,
            )
            return tmp_branch, new_sha
        except Exception as e:
            tmp_ref.delete()
            raise e

    def create_squash_commit(
        self,
        source_pull: pygithub.PullRequest,
        target_branch: str,
        *,
        use_branch_in_title: bool = False,
        commit_message_trailer: str | None = None,
    ) -> str:
        """
        This method creates a squash commit using a temporary branch.
        - create a temporary branch with target branch sha
        - merge the source head sha on it
        - use this git tree to create a new commit on target branch.
        - fast forward the target_branch to this sha.
        - delete the temporary branch

        :param source_pull: The PR that has to be squashed onto the target_branch
        :param target_branch: The branch name of the target draft PR
        :param use_branch_in_title: If True, the title of the new commit will
            contain name of the source branch instead of pull request number.
        :param commit_message_trailer: If set, a message appended after the rest of
            the commit message body.
        :return: squashed commit sha on the target branch.
        """
        pr_to_merge_sha = self.create_squash_commit_per_pull(
            [
                SquashPullInput(
                    source_pull,
                    use_branch_in_title=use_branch_in_title,
                    commit_message_trailer=commit_message_trailer,
                )
            ],
            target_branch,
        )
        return pr_to_merge_sha[source_pull.number]

    def create_squash_commit_per_pull(
        self, pulls: list[SquashPullInput], target_branch: str
    ) -> dict[int, str]:
        """
        Returns a PR number to merge_commit_sha dictionary.
        """
        self._refresh_token_if_necessary()
        target_ref = self.get_git_ref(target_branch)
        target_sha = target_ref.object.sha

        tmp_branch = target_branch + "-" + str(random.randint(0, 10000))
        tmp_ref = self.push_git_ref(tmp_branch, sha=target_sha)

        # We work off of two branches here. First, we create a temporary branch
        # where we merge each pull (using a merge commit). Then, for every merge
        # commit (corresponding to each pull request), we use the commit's tree
        # to create a squash commit. The main reason we do it this way is to
        # allow stacked PRs to work correctly (i.e., we deduplicate commits on
        # the branch that we're using merge commits on).
        # Graphically, if we have:
        #   PR1 := main -> 1a -> 1b
        #   PR2 := main -> 2a -> 2b
        #   PR3 := main -> 2a -> 2b -> 3a -> 3b
        # then we'll construct the temporary merge commit branch:
        #   temp-mc := main ->           M1 -> M2 -> M3
        #                 |--> 1a -> 1b ^      ^     ^
        #                 |--> 2a -> 2b -------|     |
        #                             | -> 3a -> 3b -|
        # and from that, "squash" each merge commit to erase the "messy history"
        # of how the branches diverged and returned to main:
        #   final-squash := main -> S1 -> S2 -> S3
        # This works by taking the tree of each merge commit and creating a new
        # commit on main with that tree but with only one parent (the previous
        # commit on main).
        merge_commits: list[pygithub.Commit] = []
        squash_commits: list[pygithub.GitCommit] = [
            self.repo.get_commit(target_sha).commit,
        ]
        pr_to_merge_sha = {}try:
            for input in pulls:
                pull = input.pull
                # Create the merge commit M[N]. Since we're applying it to the
                # temp-mc branch, each commit is played on top of M[N-1]. So
                # the parents of M[N] are M[N-1] and the head of the PR.
                commit = self.merge_sha_to_branch(tmp_branch, pull.head.sha)
                # repo.merge can apparently return None. It's not entirely clear
                # when, but I think it's possible if the commit has already been
                # merged into the branch (GH docs say that the return is
                # HTTP 204 No Content).
                assert commit, (
                    f"failed to create merge commit for repo {self.repo.full_name}"
                    f" pull {pull.number}"
                )
                merge_commits.append(commit)

                # Now we create S[N] by taking the tree of M[N] and only
                # parenting it on top of S[N-1]. Since we're creating a commit
                # from a tree, GitHub doesn't automatically add it to a branch
                # and we have to keep track of the parent commit automatically.
                squash_parent = squash_commits[-1]
                if input.use_branch_in_title:
                    commit_message = "Merge branch '{}' into {}".format(
                        pull.head.ref,
                        target_branch,
                    )
                else:
                    commit_message = "%s (#%d)\n\n%s" % (
                        pull.title,
                        pull.number,
                        get_updated_body(self.db_repo, pull),
                    )
                if input.commit_message_trailer:
                    commit_message += "\n\n" + input.commit_message_trailer
                squash_commit = self.repo.create_git_commit(
                    commit_message,
                    commit.commit.tree,
                    [squash_parent],
                    author=self._get_commit_input_git_author(pull),
                )
                # MER-2109: Capturing the parent and child commit SHAs during
                # a squash event to identify if the commits were successful.
                # We had a scenario with benchling where both SHAs were same, so this can
                # help debug how that happened.
                logger.info(
                    "Repo %d PR %d created squash commit %s from squash parent %s",
                    self.db_repo.id,
                    pull.number,
                    squash_commit.sha,
                    squash_parent.sha,
                )
                squash_commits.append(squash_commit)
                pr_to_merge_sha[pull.number] = squash_commit.sha

            sha = squash_commits[-1].sha
            # This should not require forcing ref update as it's a simple fast-forward.
            target_ref.edit(sha=sha)
            return pr_to_merge_sha
        finally:
            tmp_ref.delete()

    def copy_to_new_branch(
        self, sha: str, suffix: str, prefix: str = DEFAULT_BRANCH_PREFIX
    ) -> str:
        target_branch = self.generate_branch_name(sha, suffix, prefix)
        self.create_branch(target_branch, sha)
        return target_branch

    def copy_branch_to_new_branch(
        self,
        branch: str,
        suffix: str,
        prefix: str = DEFAULT_BRANCH_PREFIX,
    ) -> str:
        sha = self.get_git_ref(branch).object.sha
        return self.copy_to_new_branch(sha, suffix, prefix)

    def create_branch(
        self, target_branch: str, sha: str, *, force: bool = False
    ) -> pygithub.GitRef:
        return self.push_git_ref(target_branch, sha=sha, force=force)

    def get_git_ref(self, branch: str, attempt: int = 0) -> pygithub.GitRef:
        try:
            return self.repo.get_git_ref("heads/" + branch)
        except pygithub.UnknownObjectException as e:
            if e.status == 404:
                # This can happen if the branch could not be found due to replication
                # lag. We retry a few times to see if it shows up.
                if attempt < 3:
                    logger.info(
                        "git ref not found, retrying",
                        repo_id=self.db_repo.id,
                        ref=branch,
                    )
                    time.sleep(2 * attempt)
                    return self.get_git_ref(branch, attempt + 1)
            raise

    def _get_commit_input_git_author(
        self, pull: pygithub.PullRequest
    ) -> pygithub.InputGitAuthor:
        """
        Get the commit author info (as a ``InputGitAuthor`` object) for the
        given pull request. We use this to copy commit author information from
        the pull request to the new commit.
        """
        author: pygithub.GitAuthor | None = None
        for commit in pull.get_commits():
            if commit.author and commit.author.login == pull.user.login:
                author = commit.commit.author
                break
        # Try to copy the information directly from the underlying Git commit
        if author and author.email:
            return pygithub.InputGitAuthor(name=author.name, email=author.email)
        # Sometimes, the author email isn't set, so we just fall back to the
        # pull request author here.
        # In some cases, pull.user.name is empty, so we use the login instead.
        return pygithub.InputGitAuthor(
            name=pull.user.name or pull.user.login,
            email=f"{pull.user.id}+{pull.user.login}@users.noreply.github.com",
        )

    def push_git_ref(
        self, target_branch: str, sha: str, *, force: bool = False
    ) -> pygithub.GitRef:
        if force:
            # Try to get the ref first, if it exists, update it. Otherwise create one.
            try:
                ref = self.get_git_ref(target_branch, attempt=3)
                ref.edit(sha=sha, force=True)
                return ref
            except pygithub.UnknownObjectException:
                logger.info("Branch not found, creating new one", ref=target_branch)
        return self.repo.create_git_ref(ref="refs/heads/" + target_branch, sha=sha)

    def merge_sha_to_branch(
        self,
        target_branch: str,
        sha: str,
        commit_message: str | pygithub.NotSet = "",
    ) -> pygithub.Commit | None:
        if not commit_message:
            commit_message = pygithub.NotSet
        try:
            return self.repo.merge(target_branch, sha, commit_message)
        except pygithub.GithubException as e:
            if e.status == 500:
                target_sha = self.get_branch_head_sha(target_branch)
                logger.warning(
                    "Received 500 error from GitHub, using native git",
                    repo_id=self.db_repo.id,
                    target_branch=target_branch,
                    target_sha=target_sha,
                    from_char=sha,
                    exc_info=e,
                )
                # This is a workaround for a bug in the GitHub API. We will use native git here.
                tmp_branch = self.generate_branch_name(
                    sha, target_branch[-5:], prefix="mq-tmp-"
                )
                with NativeGit(
                    self.access_token,
                    origin_repo=self.repo.full_name,
                    upstream_repo=self.repo.full_name,
                ) as git:
                    git.create_git_ref(tmp_branch, sha)
                try:
                    return self.repo.merge(target_branch, sha, commit_message)
                finally:
                    self.delete_ref(tmp_branch)
            raise

    # TODO: this doesn't make an API call so shouldn't be in client
    def generate_branch_name(
        self, sha: str, suffix: str, prefix: str = DEFAULT_BRANCH_PREFIX
    ) -> str:
        branch = prefix + sha[:6] + "-" + str(random.randint(0, 10000)) + "-" + suffix
        return branch

    def force_push_branch(self, source_branch: str, to_branch: str) -> str:
        ref = self.get_git_ref(to_branch)
        sha = self.get_branch_head_sha(source_branch)
        ref.edit(sha=sha, force=True)
        return sha

    def get_branch_head_sha(self, branch_name: str) -> str:
        branch = self.get_branch(branch_name)
        return branch.commit.sha

    def is_up_to_date(self, pull: pygithub.PullRequest, base_branch: str) -> bool:
        git_branch = self.get_branch(base_branch)
        base_sha = git_branch.commit.sha
        files = self.repo.compare(pull.head.sha, base_sha).files
        return not files

    def native_git(self, pull: pygithub.PullRequest) -> NativeGit:
        return NativeGit(
            self.access_token,
            origin_repo=pull.head.repo.full_name,
            upstream_repo=pull.base.repo.full_name,
        )

    def native_git_for_repo(self) -> NativeGit:
        return NativeGit(
            self.access_token,
            origin_repo=self.repo.full_name,
            upstream_repo=self.repo.full_name,
        )

    def fetch_diff(self, pr_number: int) -> str:
        # Use different media type to get a diff.
        # https://docs.github.com/en/rest/using-the-rest-api/media-types?apiVersion=2022-11-28#diff-media-type-for-commits-commit-comparison-and-pull-requests
        return gh_rest.get_non_json(
            f"/repos/{self.db_repo.name}/pulls/{pr_number}",
            token=self.access_token,
            account_id=self.db_repo.account_id,
            override_headers={"Accept": "application/vnd.github.diff"},
        )

    def fetch_three_dot_diff(self, base: str, head: str) -> str:
        return gh_rest.get_non_json(
            f"/repos/{self.db_repo.name}/compare/{base}...{head}",
            token=self.access_token,
            account_id=self.db_repo.account_id,
            override_headers={"Accept": "application/vnd.github.diff"},
        )

    def create_workflow_dispatch(
        self,
        gh_workflow_id: int,
        data: dict,
    ) -> None:
        gh_rest.post(
            path=f"repos/{self.db_repo.name}/actions/workflows/{gh_workflow_id}/dispatches",
            data=data,
            return_type=WorkflowDispatchResponse,
            token=self.access_token,
            account_id=self.db_repo.account_id,
        )

    def cancel_workflow_run(
        self,
        workflow_run_id: int,
    ) -> None:
        gh_rest.post(
            path=f"repos/{self.db_repo.name}/actions/runs/{workflow_run_id}/cancel",
            data={},
            return_type=CancelWorkflowRunResponse,
            token=self.access_token,
            account_id=self.db_repo.account_id,
        )

    def fetch_github_workflows(self) -> list[GHWorkflow]:
        r = gh_rest.get(
            f"repos/{self.db_repo.name}/actions/workflows",
            GHWorkflowList,
            token=self.access_token,
            account_id=self.db_repo.account_id,
        )
        return r.workflows


def _is_merge_conflict(error_str: str) -> bool:
    try:
        result = json.loads(error_str)
        errors_list = result.get("errors", [])
        for error in errors_list:
            if (
                error.get("type") == "UNPROCESSABLE"
                and error.get("message") == "Pull Request is not mergeable"
            ):
                return True
        return False
    except Exception:
        return False


@dataclasses.dataclass
class SquashPullInput:
    pull: pygithub.PullRequest
    commit_message_trailer: str | None = None
    use_branch_in_title: bool = False


class UpdateBranch(schema.BaseModel):
    message: str
    url: str


class WorkflowDispatchResponse(schema.BaseModel):
    pass


class CancelWorkflowRunResponse(schema.BaseModel):
    pass


class GHWorkflow(schema.BaseModel):
    #: The database ID of the workflow.
    gh_database_id: int = pydantic.Field(alias="id")

    #: The GraphQL ID of the workflow.
    node_id: str

    #: The name of the workflow.
    name: str

    #: The path of the workflow (e.g., ".github/workflows/blank.yaml").
    path: str

    #: The current state of the workflow.
    state: TE.Literal[
        "active",
        "deleted",
        "disabled_fork",
        "disabled_inactivity",
        "disabled_manually",
    ]


class GHWorkflowList(schema.BaseModel):
    total_count: int
    workflows: list[GHWorkflow]