from __future__ import annotations

from collections.abc import Iterable

from core import pygithub
from core.client import GithubClient
from core.models import BotPr, PullRequest


class Batch:
    client: GithubClient
    target_branch_name: str
    prs: list[PullRequest]
    pr_numbers: list[int]
    skip_line: bool

    def __init__(
        self,
        client: GithubClient,
        prs: list[PullRequest],
        skip_line: bool = False,
    ):
        assert len(prs) > 0, "Batch must have at least one PR"

        self.client = client
        self.prs = prs
        self.pr_numbers = [pr.number for pr in prs]
        self.skip_line = skip_line

        # we should never hit this inside the operation of the queue because
        # ensure_pull_request should have already set this information
        target_branch_name = prs[0].target_branch_name
        assert target_branch_name, (
            f"Batch must have a target branch (#{prs[0].number} has none)"
        )
        self.target_branch_name = target_branch_name

        self._internal_prs_to_pulls: dict[int, pygithub.PullRequest] = {}

    def __str__(self) -> str:
        return (
            f"Batch({self.target_branch_name}, {self.pr_numbers}"
            + (", SKIP LINE" if self.skip_line else ")")
            + ")"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Batch):
            # don't attempt to compare against unrelated types
            return False

        return self.prs == other.prs

    def get_first_pr(self) -> PullRequest:
        return self.prs[0]

    def get_first_pull(self) -> pygithub.PullRequest:
        return self._prs_to_pulls[self.get_first_pr().number]

    def get_all_pulls(self) -> Iterable[pygithub.PullRequest]:
        return self._prs_to_pulls.values()

    def get_pull(self, pr_number: int) -> pygithub.PullRequest:
        return self._prs_to_pulls[pr_number]

    def get_pr_by_number_x(self, pr_number: int) -> PullRequest:
        for pr in self.prs:
            if pr.number == pr_number:
                return pr
        raise AssertionError(f"PR {pr_number} not found in batch")

    @property
    def _prs_to_pulls(self) -> dict[int, pygithub.PullRequest]:
        if not self._internal_prs_to_pulls:
            self._internal_prs_to_pulls = {
                pr.number: self.client.get_pull(pr.number) for pr in self.prs
            }
        return self._internal_prs_to_pulls

    def did_optimistically_reuse_original_pr(self, bot_pr: BotPr) -> bool:
        """
        Returns true if the BotPr is the same as the original PR.
        This is only valid for batch_size=1.
        """
        return bot_pr.number == self.get_first_pr().number

    def is_skip_line(self) -> bool:
        return self.skip_line

    def is_bisected_batch(self) -> bool:
        return bool(
            self.prs
            and self.prs[0].merge_operations
            and self.prs[0].merge_operations.is_bisected
        )
