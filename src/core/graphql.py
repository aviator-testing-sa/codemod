from __future__ import annotations

import dataclasses
import datetime
import itertools
import json
import traceback
from os import path
from typing import Any

import structlog

import schema.github
from core import gh_rest, rate_limit
from core.models import GithubRepo, PullRequest
from errors import GitHubOrganizationInvalidException
from lib import gh_graphql
from lib.gh_graphql import (
    CreatedPullRequestInfo,
    CreatePullRequestInput,
    FileChanges,
    GitCommit,
    GithubTeamInfo,
    GithubUserInfo,
)
from lib.gh_graphql.graphql_client.exceptions import GraphQLClientGraphQLMultiError
from lib.gh_graphql.graphql_client.flex_review_batch_pull_requests import (
    FlexReviewBatchPullRequestsRepositoryPullRequests,
)
from lib.gh_graphql.graphql_client.fragments import (
    ATCPullRequestInfo,
    ChangedFile,
    FlexReviewPullRequestInfo,
    PullRequestReviewers,
    SignalsPullRequestInfo,
)
from lib.gh_graphql.graphql_client.pull_request_merge_commit_sha import (
    PullRequestMergeCommitSHARepositoryPullRequests,
)
from main import app
from util import req, time_util

logger = structlog.stdlib.get_logger()

DEFAULT_API_URL = "https://api.github.com"
MAX_PAGES = 5
RATE_LIMIT_SUB_QUERY = """
  rateLimit {
    limit
    cost
    remaining
    resetAt
  }
"""


@dataclasses.dataclass
class GraphqlResponse:
    # the raw response text
    raw: str

    # the data from the response
    data: dict[str, Any] | None

    # any errors that the query generated
    errors: list[dict[str, Any]] | None

    def raise_for_error(self) -> None:
        # check for generic issue
        if "Something went wrong while executing your query" in self.raw:
            raise GithubGqlQueryException(self.raw)
        # check the response data
        self.must_response_data()
        # now check specific errors
        if not self.errors:
            return
        elif any([err.get("type") == "NOT_FOUND" for err in self.errors]):
            raise GithubGqlNotFoundException(self.raw)
        else:
            raise GithubGqlException(self.raw)

    def must_response_data(self) -> dict[str, Any]:
        """
        :raises GithubGqlNoDataException: if data is not present
        :return: the response data
        """
        if not self.data:
            raise GithubGqlNoDataException(self.raw)
        return self.data


class GithubGqlException(Exception):
    pass


# exceptions
class GithubGqlNotFoundException(GithubGqlException):
    pass


class GithubGqlQueryException(GithubGqlException):
    pass


class GithubGqlMutationException(GithubGqlException):
    pass


class GithubGqlNoDataException(GithubGqlException):
    pass


class GithubGql:
    def __init__(self, access_token: str, account_id: int):
        base_url = (
            DEFAULT_API_URL
            if (app.config["GITHUB_API_BASE_URL"] == DEFAULT_API_URL)
            else (app.config["GITHUB_BASE_URL"] + "/api")
        )
        self.url = base_url + "/graphql"
        self.headers = {
            "Authorization": "bearer " + access_token,
            gh_rest.HEADER_NEXT_GLOBAL_ID: "1",
        }
        self.account_id = account_id

        # Type-safe client backed by codegen.
        # Ultimately, we'll probably want to just use that, but we're
        # transitioning towards than incrementally.
        self.__client = gh_graphql.Client(
            self.url,
            headers=self.headers,
            rate_limit_hook=self.__record_rate_limit,
        )

    def fetch(self, query: str, **kwargs: Any) -> GraphqlResponse:
        stack = traceback.extract_stack()
        caller = []
        query_name = ""
        for frame in stack:
            if "core/" in frame.filename and "graphql.py" not in frame.filename:
                caller.append(
                    f"{path.basename(frame.filename)}:{frame.lineno}:{frame.name}"
                )
            if frame.name != "fetch":
                query_name = frame.name
        data = json.dumps(
            {
                "query": query,
                "variables": kwargs,
            }
        )
        response = req.post(
            self.url,
            headers=self.headers,
            data=data,
            timeout=app.config["REQUEST_TIMEOUT_SEC"],
        )
        github_request_id = response.headers.get("x-github-request-id", None)

        if not response.ok:
            logger.info(
                "GitHub GraphQL API request (HTTP failure)",
                github_gql_query_name=query_name,
                github_api_status=response.status_code,
                github_request_id=github_request_id,
                elapsed=response.elapsed.total_seconds(),
                caller=caller,
            )
            response.raise_for_status()

        result = response.json()
        rate_limit_data = result.get("data", {}).get("rateLimit", {}) or {}
        if rate_limit_data:
            rate_limit.set_gh_graphql_api_limit(
                self.account_id,
                remaining=rate_limit_data.get("remaining", 0),
                total=rate_limit_data.get("limit", 0),
            )
        logger.info(
            "GitHub GraphQL API request (HTTP success)",
            github_gql_query_name=query_name,
            github_api_status=response.status_code,
            github_request_id=github_request_id,
            github_gql_rate_limit_remaining=rate_limit_data.get("remaining"),
            github_gql_rate_limit_limit=rate_limit_data.get("limit"),
            github_gql_rate_limit_cost=rate_limit_data.get("cost"),
            elapsed=response.elapsed.total_seconds(),
            caller=caller,
        )

        return GraphqlResponse(
            raw=response.text,
            data=result.get("data"),
            errors=result.get("errors"),
        )

    def get_atc_pull_request(
        self, owner: str, name: str, number: int
    ) -> ATCPullRequestInfo:
        res = self.__client.atc_pull_request(owner=owner, name=name, number=number)
        repo = res.repository
        if not repo:
            raise GithubGqlNotFoundException()
        pr = repo.pull_request
        if not pr:
            raise GithubGqlNotFoundException()
        return pr

    def get_signals_pull_request(
        self, owner: str, name: str, number: int
    ) -> SignalsPullRequestInfo:
        res = self.__client.signals_pull_request(owner=owner, name=name, number=number)
        repo = res.repository
        if not repo:
            raise GithubGqlNotFoundException()
        gql_pr = repo.pull_request
        if not gql_pr:
            raise GithubGqlNotFoundException()
        return gql_pr

    def get_latest_merged_pull_request(
        self,
        *,
        owner: str,
        name: str,
    ) -> gh_graphql.PullRequestInfo | None:
        try:
            res = self.__client.latest_merged_pull_request(owner=owner, name=name)
        except gh_graphql.GraphQLClientError as exc:
            logger.error("Failed to fetch latest merged pull request", exc_info=exc)
            return None

        repo = res.repository
        if not repo:
            logger.info("GitHub repository was not found")
            return None

        pr = repo.pull_requests.nodes and repo.pull_requests.nodes[0]
        if not pr:
            logger.info("No merged pull requests found")
            return None

        return pr

    def get_pull_requests_by_node_id(
        self, ids: list[str]
    ) -> list[gh_graphql.PullRequestInfo | None]:
        ret: list[gh_graphql.PullRequestInfo | None] = []
        for batch_ids in itertools.batched(ids, 100):
            res = self.__client.pull_requests_by_node_id(ids=list(batch_ids))
            ret.extend(
                [
                    # Convert to None if the node is not a pull request.
                    # This shouldn't actually ever happen, but it's theoretically
                    # possible, and we need this to appease type-checking.
                    node if node and node.typename__ == "PullRequest" else None
                    for node in res.nodes
                ]
            )
        return ret

    def get_approved_pulls(
        self,
        repo_owner: str,
        repo_name: str,
        *,
        labels: list[str],
        count: int = 20,
    ) -> list[int]:
        """
        Get a list of pull request numbers that are approved and match any of
        the given labels.
        """
        try:
            query = (
                """
                query ($owner: String!, $name: String!, $labels: [String!], $count: Int!) {
                  %s
                  repository(owner: $owner, name: $name) {
                    pullRequests(states: [OPEN], first: $count, labels: $labels, orderBy: {field: UPDATED_AT, direction: DESC}) {
                      nodes {
                        number
                        reviewDecision
                      }
                    }
                  }
                }
                """
                % RATE_LIMIT_SUB_QUERY
            )
            result = self.fetch(
                query,
                owner=repo_owner,
                name=repo_name,
                labels=labels,
                count=count,
            )

            if not result.data:
                logger.error(
                    "Query failed to fetch labeled pull requests from GraphQL",
                    result=result,
                )
                return []

            try:
                nodes = result.data["repository"]["pullRequests"]["nodes"]
            except (AttributeError, TypeError) as e:
                logger.info(
                    "Fetching labeled pull requests returned no data",
                    result=result,
                    exc_info=e,
                )
                return []

            numbers = []
            for node in nodes:
                # In case of no review requirement, the decision may come out as empty.
                # We should process those PRs as well.
                if node["reviewDecision"] == "APPROVED" or not node["reviewDecision"]:
                    numbers.append(node["number"])
            return numbers
        except Exception as e:
            logger.info("Failed to fetch approved pulls", exc_info=e)
        return []

    def get_review_decision(self, repo_name: str, pr_number: int) -> str | None:
        """
        Get the PR review decision for the given PR.

        None is returned only if both the base branch doesn't have a branch
        protection rule requiring reviews *and* no review has been
        explicitly requested.
        Further reading: https://github.community/t/when-will-pullrequest-reviewdecision-be-null/134274

        See possible enum values here: https://docs.github.com/en/graphql/reference/enums#pullrequestreviewdecision

        :return: Either the decision status (GraphQL enum value) or None.
        """
        try:
            org, repo = repo_name.split("/")
            query = (
                """
            query ($org: String!, $repo: String!, $pr_number: Int!) {
              %s
              repository(name: $repo, owner: $org) {
                pullRequest(number: $pr_number) {
                  reviewDecision
                  reviews(last: 50) {
                    nodes {
                      state
                    }
                  }
                }
              }
            }
            """
                % RATE_LIMIT_SUB_QUERY
            )
            result = self.fetch(
                query,
                org=org,
                repo=repo,
                pr_number=pr_number,
            )

            if not result.data:
                logger.error(
                    "Failed to fetch data for repo %s from github graphql %s",
                    repo_name,
                    result,
                )
                return ""

            try:
                pr = result.data["repository"]["pullRequest"]
            except AttributeError:
                logger.error(
                    "Failed to fetch pull request for repo %s from github graphql %s",
                    repo_name,
                    result,
                )
                return ""

            status: str | None = pr.get("reviewDecision")

            # If status is None/null, that means that there is no branch
            # protection rule setup to require reviews. We'll get None even if
            # there is an approving review added (however if there is a changes
            # requested review, we will still get back CHANGES_REQUESTED).
            # This mostly only matters for testing purposes (where there are no
            # branch protection rules), but we still want to handle this
            # correctly, so as a fallback we just check if there are any
            # approving reviews added.
            if status is None:
                logger.info(
                    "Repo %s PR %s has no review decision", repo_name, pr_number
                )
                reviews = pr.get("reviews", {}).get("nodes", [])
                has_approving_review = any(
                    review["state"] == "APPROVED" for review in reviews
                )
                if has_approving_review:
                    status = "APPROVED"

            if status != "APPROVED":
                logger.info("Repo %s PR %d status %s", repo_name, pr_number, status)
            return status
        except Exception as e:
            logger.info(
                "Failed to fetch results for repo",
                repo_name=repo_name,
                pr_number=pr_number,
                exc_info=e,
            )
            raise

    def get_resolved_conversation_pulls(
        self, repo: GithubRepo, pr_list: list[PullRequest], count: int = 20
    ) -> list[int]:
        try:
            query = (
                """
                query ($ids: [ID!]!, $count: Int!) {
                  %s
                  nodes(ids: $ids){
                    ... on PullRequest {
                        number
                        reviewThreads(first: $count) {
                          nodes {
                            isResolved
                          }
                        }
                    }
                  }
                }
                """
                % RATE_LIMIT_SUB_QUERY
            )

            pr_ids = [pr.gh_node_id for pr in pr_list if pr.gh_node_id]
            result = self.fetch(
                query,
                ids=pr_ids,
                count=count,
            )

            if not result.data:
                logger.error(
                    "Failed to fetch data for repo %s from github graphql %s",
                    repo.id,
                    result,
                )
                return []

            try:
                nodes = result.data["nodes"]
            except (AttributeError, TypeError):
                logger.info(
                    "Failed to fetch resolved nodes for repo %s from github graphql %s",
                    repo.id,
                    result,
                )
                return []

            numbers = []
            for node in nodes:
                numbers.append(node["number"])
            return numbers
        except Exception as e:
            logger.info(
                "Failed to fetch resolved pulls for repo",
                repo_id=repo.id,
                exc_info=e,
            )
        return []

    def fetch_teams_in_org(
        self, org_name: str, root_only: bool = False
    ) -> dict[int, GithubTeamInfo]:
        has_next_page = True
        after: str | None = None
        team_mapping: dict[int, GithubTeamInfo] = {}
        try:
            while has_next_page:
                try:
                    result = self.__client.github_team_for_organization(
                        org_name=org_name, after=after, root_teams_only=root_only
                    )
                except GraphQLClientGraphQLMultiError as e:
                    raise GitHubOrganizationInvalidException(e)

                # NOTE: Is there a better practice to handle all these optionals?
                if not result.organization:
                    raise Exception("Organization does not exists")
                if not result.organization.teams:
                    raise Exception("Teams do not exists")
                if result.organization.teams.nodes is None:
                    raise Exception("Missing team nodes")
                if not result.organization.teams.nodes:  # when nodes is []
                    break
                if not result.organization.teams.page_info:
                    raise Exception("Incomplete response missing pageInfo")
                teams = result.organization.teams
                if teams.nodes:
                    for team in teams.nodes:
                        if isinstance(team, GithubTeamInfo):
                            team_mapping[team.database_id] = team
                has_next_page = teams.page_info.has_next_page == True
                if not teams.page_info.end_cursor:
                    raise Exception("Incomplete response missing endCursor")
                after = teams.page_info.end_cursor
        except Exception as e:
            logger.info(
                "Failed to fetch team information",
                organization=org_name,
                exc_info=e,
            )
            raise e

        return team_mapping

    def fetch_team_members(
        self, org_name: str, team_slug: str, after: str | None = None
    ) -> list[GithubUserInfo]:
        members: list[GithubUserInfo] = []
        try:
            result = self.__client.team_member_list(
                org_name=org_name, team_slug=team_slug, after=after
            )
            if not result.organization:
                raise Exception("Organization does not exists")
            if not result.organization.team:
                raise Exception("Team do not exists")
            if not result.organization.team.members:
                raise Exception("Members do not exists")
            if result.organization.team.members.nodes is None:
                raise Exception("Missing members nodes")
            if not result.organization.team.members.nodes:  # nodes is []
                return []
            if not result.organization.team.members.page_info:
                raise Exception("Incomplete response missing pageInfo")
            for member in result.organization.team.members.nodes:
                if isinstance(member, GithubUserInfo):
                    members.append(member)
            page_info = result.organization.team.members.page_info
            if page_info.has_next_page:
                if not page_info.end_cursor:
                    raise Exception("Incomplete response missing endCursor")
                members += self.fetch_team_members(
                    org_name, team_slug, after=page_info.end_cursor
                )
        except Exception as e:
            logger.info(
                "Failed to fetch team information",
                organization=org_name,
                exc_info=e,
            )
        return members

    def get_team_from_slug(
        self, org_name: str, team_slug: str
    ) -> schema.github.Team | None:
        try:
            query = (
                """
            query ($team_slug: String!, $org_name: String!) {
              %s
              organization(login: $org_name) {
                team(slug: $team_slug) {
                  members {
                    edges {
                      node {
                        login
                      }
                    }
                  }
                  name
                  id
                }
              }
            }
            """
                % RATE_LIMIT_SUB_QUERY
            )
            result = self.fetch(
                query,
                org_name=org_name,
                team_slug=team_slug,
            )
            result.raise_for_error()
            data = result.must_response_data()
            team_data = data.get("organization", {}).get("team", {})
            if not team_data:
                logger.info(
                    "Failed to find team for %s/%s",
                    org_name,
                    team_slug,
                )
                return None
            return schema.github.Team(
                id=team_data["id"],
                name=team_data["name"],
                members=[
                    schema.github.User(login=member["node"]["login"])
                    for member in team_data["members"]["edges"]
                ],
            )
        except Exception as e:
            logger.info(
                "Failed to fetch github team",
                org_name=org_name,
                team_slug=team_slug,
                exc_info=e,
            )
        return None

    def are_comments_resolved(self, repo_name: str, pr_number: int) -> bool:
        try:
            org, repo = repo_name.split("/")
            query = (
                """
            query ($org: String!, $repo: String!, $pr_number: Int!) {
              %s
              repository(name: $repo, owner: $org) {
                pullRequest(number: $pr_number) {
                  reviewThreads(first: 100) {
                    nodes {
                      isResolved
                    }
                  }
                }
              }
            }
            """
                % RATE_LIMIT_SUB_QUERY
            )
            result = self.fetch(
                query,
                org=org,
                repo=repo,
                pr_number=pr_number,
            )

            if not result.data:
                logger.error(
                    "Failed to fetch data for repo %s from github graphql %s",
                    repo_name,
                    result,
                )
                return False

            try:
                review_threads = result.data["repository"]["pullRequest"].get(
                    "reviewThreads"
                )
            except AttributeError:
                logger.error(
                    "Failed to fetch reviewThreads for repo %s from github graphql %s",
                    repo_name,
                    result,
                )
                return False

            if review_threads:
                for node in review_threads["nodes"]:
                    if not node.get("isResolved"):
                        logger.info(
                            "Repo %s PR %d unresolved comment found, review nodes: %s",
                            repo_name,
                            pr_number,
                            review_threads["nodes"],
                        )
                        return False
        except Exception as e:
            logger.info(
                "Failed to fetch review threads",
                repo_name=repo_name,
                pr_number=pr_number,
                exc_info=e,
            )
        return True

    def get_pr_commits_info(self, repo_name: str, pr_number: int) -> list[CommitInfo]:
        """
        Fetch information about commits in a PR from the GitHub API.

        :param repo_name: The name of the repository (`owner/repo`)
        :param pr_number: The GitHub PR number
        :raises: Exception if the request fails
        :return: A list of :class:`CommitInfo` objects associated with the PR.
        """
        org, repo = repo_name.split("/")
        query = (
            """
        query ($org: String!, $repo: String!, $pr_number: Int!) {
          %s
          repository(name: $repo, owner: $org) {
            pullRequest(number: $pr_number) {
              commits(first: 100) {
                nodes {
                  commit {
                    oid
                    abbreviatedOid
                    signature {
                      isValid
                    }
                  }
                }
              }
            }
          }
        }
        """
            % RATE_LIMIT_SUB_QUERY
        )
        result = self.fetch(
            query,
            org=org,
            repo=repo,
            pr_number=pr_number,
        )
        if not result.data:
            logger.error(
                "Failed to fetch data for repo %s from github graphql %s", repo, result
            )
            return []

        try:
            nodes = result.data["repository"]["pullRequest"]["commits"].get("nodes", [])
        except AttributeError:
            logger.error(
                "Failed to fetch nodes for repo %s from github graphql %s", repo, result
            )
            return []

        commits = []
        for n in nodes:
            signature = n["commit"]["signature"] or {}
            has_signature = bool(signature)
            info = CommitInfo(
                oid=n["commit"]["oid"],
                oid_short=n["commit"]["abbreviatedOid"],
                signed=has_signature,
                verified=signature.get("isValid", False),
            )
            commits.append(info)
        return commits

    def get_branches(
        self,
        repo_name: str,
        branch_filter: str,
        after: str = "",
        page: int = 0,
    ) -> list[str]:
        org, repo = repo_name.split("/")
        query = (
            """
        query ($org: String!, $repo: String!, $branch_filter: String!, $after: String!) {
          %s
          repository(name: $repo, owner: $org) {
            refs(refPrefix: "refs/heads/", query: $branch_filter, first: 100, after: $after) {
              edges {
                node {
                  name
                  target {
                    ... on Commit {
                      committedDate
                    }
                  }
                }
              }
              pageInfo {
                hasNextPage
                endCursor
              }
            }
          }
        }
        """
            % RATE_LIMIT_SUB_QUERY
        )
        result = self.fetch(
            query,
            org=org,
            repo=repo,
            branch_filter=branch_filter,
            after=after,
        )

        if not result.data:
            logger.error(
                "Failed to fetch data for repo %s from github graphql %s", repo, result
            )
            return []

        one_hour_ago = time_util.now() - datetime.timedelta(hours=1)
        branches = []
        try:
            edges = result.data["repository"]["refs"].get("edges", [])
            page_info = result.data["repository"]["refs"]["pageInfo"]
            for edge in edges:
                name = edge["node"]["name"]
                committed_date_str = edge["node"]["target"]["committedDate"]
                committed_date = datetime.datetime.strptime(
                    committed_date_str, "%Y-%m-%dT%H:%M:%SZ"
                )
                # avoid branches created in the past hour
                if committed_date < one_hour_ago:
                    branches.append(name)

            if page_info["hasNextPage"] and page < MAX_PAGES:
                branches += self.get_branches(
                    repo_name=repo_name,
                    branch_filter=branch_filter,
                    after=page_info["endCursor"],
                    page=page + 1,
                )
        except AttributeError:
            logger.error(
                "Failed to fetch nodes for repo %s from github graphql %s", repo, result
            )
            return []
        return branches

    def merge_pull_request(
        self,
        pull_request_id: str,
        *,
        commit_headline: str | None = None,
        commit_body: str | None = None,
        expected_head_oid: str | None = None,
        merge_method: str | None = None,
        author_email: str | None = None,
    ) -> str:
        """
        Merge a pull request on GitHub.

        :param pull_request_id: The GraphQL ID of the pull request to merge.
        :param commit_headline: The headline of the merge commit that will be
            created. If omitted, GitHub will use the pull request's title.
        :param commit_body: The body of the merge commit that will be created.
            If omitted, GitHub will use the pull request's body.
        :param expected_head_oid: The expected head OID (commit SHA) of the pull
            request.
        :param merge_method: The merge method to use. Can be "MERGE", "SQUASH",
            or "REBASE". If omitted, defaults to "MERGE".
        :param author_email: The email address of the author of the merge commit.
        """
        query = """
        mutation ($input: MergePullRequestInput!) {
          mergePullRequest(input: $input) {
            pullRequest {
              id
              state
              mergeCommit {
                oid
              }
            }
          }
        }
        """
        res = self.fetch(
            query,
            input={
                "pullRequestId": pull_request_id,
                "commitHeadline": commit_headline,
                "commitBody": commit_body,
                "expectedHeadOid": expected_head_oid,
                "mergeMethod": merge_method,
                "authorEmail": author_email,
            },
        )
        res.raise_for_error()
        state = res.must_response_data()["mergePullRequest"]["pullRequest"]["state"]
        sha: str = res.must_response_data()["mergePullRequest"]["pullRequest"][
            "mergeCommit"
        ]["oid"]
        if state != "MERGED":
            raise Exception("Failed to merge pull request")
        return sha

    def get_pull_approvers_and_changed_files(
        self, repo_name: str, pr_num: int
    ) -> tuple[PullRequestReviewers, list[ChangedFile]]:
        """Get the users who approved the PR and the changed files.

        :param repo_name: The repository name.
        :param pr_num: The PR number.

        :raises GithubGqlException: Raised if it fails to fetch data.
        """
        owner, name = repo_name.split("/")
        try:
            # We need to see all the files that are modified in order to make an
            # approval decision. If we miss some files, we cannot calculate
            # whether PR can be merged or not.
            #
            # We can address the pagination part later.
            res = self.__client.pull_request_approvers_and_changed_files(
                owner,
                name,
                pr_num,
            )
        except gh_graphql.GraphQLClientError as exc:
            raise GithubGqlException() from exc

        if (
            not res.repository
            or not res.repository.pull_request
            or not res.repository.pull_request.files
            or not res.repository.pull_request.files.nodes
        ):
            raise GithubGqlNotFoundException()

        files: list[ChangedFile] = []
        for file in res.repository.pull_request.files.nodes:
            if file:
                files.append(file)
        return (res.repository.pull_request, files)

    def get_flexreview_pull_request(
        self, *, owner: str, name: str, number: int
    ) -> FlexReviewPullRequestInfo:
        try:
            res = self.__client.flex_review_pull_request(
                owner=owner,
                name=name,
                number=number,
            )
        except gh_graphql.GraphQLClientError as exc:
            raise GithubGqlException() from exc

        if not res.repository or not res.repository.pull_request:
            raise GithubGqlNotFoundException()

        return res.repository.pull_request

    def get_flexreview_batch_pull_requests(
        self,
        repo_name: str,
        before_cursor: str | None,
    ) -> FlexReviewBatchPullRequestsRepositoryPullRequests:
        owner, name = repo_name.split("/")
        try:
            # This takes a long time to fetch. Set the longer timeout.
            res = self.__client.flex_review_batch_pull_requests(
                owner, name, before_cursor, timeout=30
            )
        except gh_graphql.GraphQLClientError as exc:
            raise GithubGqlException() from exc

        if not res.repository or not res.repository.pull_requests:
            raise GithubGqlNotFoundException()

        return res.repository.pull_requests

    def get_pull_requests_with_merge_commit_sha(
        self,
        repo_full_name: str,
        before_cursor: str | None = None,
    ) -> PullRequestMergeCommitSHARepositoryPullRequests:
        owner, name = repo_full_name.split("/")
        try:
            # This takes a long time to fetch. Set the longer timeout.
            res = self.__client.pull_request_merge_commit_sha(
                owner, name, before_cursor, timeout=30
            )
        except gh_graphql.GraphQLClientError as exc:
            raise GithubGqlException() from exc

        if not res.repository or not res.repository.pull_requests:
            raise GithubGqlNotFoundException()

        return res.repository.pull_requests

    def create_commit_on_branch(
        self,
        repo_full_name: str,
        branch_name: str,
        expected_head_oid: str,
        message: str,
        message_body: str | None = None,
        file_changes: FileChanges | None = None,
    ) -> GitCommit:
        try:
            result = self.__client.create_commit_on_branch(
                repo_full_name=repo_full_name,
                branch_name=branch_name,
                expected_head_oid=expected_head_oid,
                message=message,
                message_body=message_body,
                file_changes=file_changes,
            )
        except gh_graphql.GraphQLClientError as exc:
            raise GithubGqlException() from exc

        if (
            not result.create_commit_on_branch
            or not result.create_commit_on_branch.commit
        ):
            raise GithubGqlNoDataException()
        return result.create_commit_on_branch.commit

    def create_branch(
        self,
        branch_name: str,
        head_oid: str,
        repo_node_id: str,
    ) -> str:
        try:
            result = self.__client.create_ref(
                name=f"refs/heads/{branch_name}",
                oid=head_oid,
                repo_node_id=repo_node_id,
            )
        except gh_graphql.GraphQLClientError as exc:
            raise GithubGqlException() from exc

        if (
            not result.create_ref
            or not result.create_ref.ref
            or not result.create_ref.ref.id
        ):
            raise GithubGqlNoDataException()
        return result.create_ref.ref.id

    def update_branch(
        self,
        branch_name: str,
        head_oid: str,
        repo_node_id: str,
        force: bool = False,
    ) -> None:
        try:
            self.__client.update_refs(
                name=f"refs/heads/{branch_name}",
                oid=head_oid,
                repo_node_id=repo_node_id,
                force=force,
            )
        except gh_graphql.GraphQLClientError as exc:
            raise GithubGqlMutationException() from exc

    def create_pull_request(
        self,
        repo: GithubRepo,
        repo_node_id: str,
        head_branch_name: str,
        title: str,
        body: str | None = None,
        draft: bool | None = None,
    ) -> CreatedPullRequestInfo:
        try:
            result = self.__client.create_pull_request(
                input=CreatePullRequestInput(
                    base_ref_name=repo.head_branch,
                    body=body,
                    draft=draft,
                    head_ref_name=head_branch_name,
                    repository_id=repo_node_id,
                    title=title,
                )
            )
        except gh_graphql.GraphQLClientError as exc:
            raise GithubGqlException() from exc

        if (
            not result.create_pull_request
            or not result.create_pull_request.pull_request
        ):
            raise GithubGqlNoDataException()
        return result.create_pull_request.pull_request

    def get_pull_request_and_commit_for_sandbox_pr_approval(
        self,
        org_name: str,
        repo_name: str,
        pr_number: int,
    ) -> SandboxPullRequestApprovalInfo:
        try:
            result = self.__client.pull_request_for_sandbox_approval(
                org_name=org_name,
                repo_name=repo_name,
                pr_number=pr_number,
            )
        except gh_graphql.GraphQLClientError as exc:
            raise GithubGqlException() from exc

        if (
            not result.viewer
            or not result.viewer.login
            or not result.repository
            or not result.repository.pull_request
            or not result.repository.pull_request.id
            or not result.repository.pull_request.author
            or not result.repository.pull_request.author.login
            or not result.repository.pull_request.commits
            or not result.repository.pull_request.commits.total_count
            or not result.repository.pull_request.commits.nodes
            or not result.repository.pull_request.commits.nodes[0]
            or not result.repository.pull_request.commits.nodes[0].commit
            or not result.repository.pull_request.commits.nodes[0].commit.author
            or not result.repository.pull_request.commits.nodes[0].commit.author.user
            or not result.repository.pull_request.commits.nodes[
                0
            ].commit.author.user.login
            or not result.repository.pull_request.commits.nodes[0].commit.signature
            or result.repository.pull_request.commits.nodes[0].commit.signature.is_valid
            is None
        ):
            raise GithubGqlNoDataException()
        return SandboxPullRequestApprovalInfo(
            pr_node_id=result.repository.pull_request.id,
            viewer=result.viewer.login,
            pr_author=result.repository.pull_request.author.login,
            commit_author=result.repository.pull_request.commits.nodes[
                0
            ].commit.author.user.login,
            commit_count=result.repository.pull_request.commits.total_count,
            commit_verified=result.repository.pull_request.commits.nodes[
                0
            ].commit.signature.is_valid,
        )

    def get_default_branch_head_sha(
        self,
        org_name: str,
        repo_name: str,
    ) -> tuple[str, str, str]:
        try:
            result = self.__client.default_branch_head_sha(
                org_name=org_name, repo_name=repo_name
            )
        except gh_graphql.GraphQLClientError as exc:
            raise GithubGqlException() from exc

        if (
            not result
            or not result.repository
            or not result.repository.id
            or not result.repository.default_branch_ref
            or not result.repository.default_branch_ref.name
            or not result.repository.default_branch_ref.target
            or not result.repository.default_branch_ref.target.oid
        ):
            raise GithubGqlNoDataException()
        return (
            result.repository.id,
            result.repository.default_branch_ref.name,
            result.repository.default_branch_ref.target.oid,
        )

    def get_head_sha_for_branch(
        self,
        org_name: str,
        repo_name: str,
        branch_name: str,
    ) -> str:
        try:
            result = self.__client.head_sha_for_branch(
                org_name=org_name,
                repo_name=repo_name,
                branch_ref=f"refs/heads/{branch_name}",
            )
        except gh_graphql.GraphQLClientError as exc:
            raise GithubGqlException() from exc

        if (
            not result
            or not result.repository
            or not result.repository.ref
            or not result.repository.ref.target
            or not result.repository.ref.target.oid
        ):
            raise GithubGqlNoDataException()

        return result.repository.ref.target.oid

    def get_repo_node_id_and_branch_sha(
        self,
        org_name: str,
        repo_name: str,
        branch_name: str,
    ) -> tuple[str, str]:
        try:
            result = self.__client.head_sha_for_branch(
                org_name=org_name,
                repo_name=repo_name,
                branch_ref=f"refs/heads/{branch_name}",
            )
        except gh_graphql.GraphQLClientError as exc:
            raise GithubGqlException() from exc

        if (
            not result
            or not result.repository
            or not result.repository.ref
            or not result.repository.ref.target
            or not result.repository.ref.target.oid
        ):
            raise GithubGqlNoDataException()

        return result.repository.id, result.repository.ref.target.oid

    def approve_pr(self, pr_node_id: str) -> str:
        try:
            result = self.__client.approve_pull_request(pull_request_id=pr_node_id)
        except gh_graphql.GraphQLClientError as exc:
            raise GithubGqlException() from exc

        if (
            not result.add_pull_request_review
            or not result.add_pull_request_review.pull_request_review
        ):
            raise GithubGqlNoDataException()
        return result.add_pull_request_review.pull_request_review.id

    def get_pull_requests_for_label(
        self,
        repo: GithubRepo,
        label: str,
    ) -> list[int]:
        try:
            result = self.__client.pull_requests_with_label(
                org_name=repo.org_name,
                repo_name=repo.repo_name,
                label=label,
            )
        except gh_graphql.GraphQLClientError as exc:
            raise GithubGqlException() from exc

        if (
            not result.repository
            or not result.repository.label
            or not result.repository.label.pull_requests
            or result.repository.label.pull_requests.nodes is None
        ):
            # It is expected that label might not exist or no data.
            return []

        return [pr.number for pr in result.repository.label.pull_requests.nodes if pr]

    def mark_pull_request_ready_for_review(self, pr_node_id: str) -> None:
        try:
            result = self.__client.mark_ready_for_review(pull_request_id=pr_node_id)
        except gh_graphql.GraphQLClientError as exc:
            raise GithubGqlException from exc

        if (
            not result
            or not result.mark_pull_request_ready_for_review
            or not result.mark_pull_request_ready_for_review.pull_request
        ):
            raise GithubGqlNoDataException

        if result.mark_pull_request_ready_for_review.pull_request.is_draft:
            raise GithubGqlMutationException("Failed to mark PR as ready for review")

    def __record_rate_limit(self, data: gh_graphql.RateLimitInfo) -> None:
        rate_limit.set_gh_graphql_api_limit(
            self.account_id,
            remaining=data.remaining,
            total=data.limit,
        )


@dataclasses.dataclass
class CommitInfo:
    # The commit id (i.e., commit SHA)
    oid: str
    # The truncated commit ID (for easier to read messages)
    oid_short: str
    # True if the commit has a GPG signature attached.
    signed: bool
    # True if the commit is signed and GitHub has verified the signature.
    verified: bool


@dataclasses.dataclass
class SandboxPullRequestApprovalInfo:
    pr_node_id: str
    viewer: str
    pr_author: str
    commit_author: str
    commit_count: int
    commit_verified: bool
