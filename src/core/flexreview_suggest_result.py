from __future__ import annotations

import dataclasses
import enum
from typing import TypeAlias

from schema import StrictBaseModel

FilePath: TypeAlias = str
OwnerName: TypeAlias = str
NormalizedExpertScore: TypeAlias = float
UserExpertScore: TypeAlias = tuple[int, NormalizedExpertScore]


class ExpertLevel(enum.Enum):
    EXPERT_LEVEL_LOW = "EXPERT_LEVEL_LOW"
    EXPERT_LEVEL_MEDIUM = "EXPERT_LEVEL_MEDIUM"
    EXPERT_LEVEL_HIGH = "EXPERT_LEVEL_HIGH"

    @classmethod
    def from_expert_score(cls, score: float) -> ExpertLevel:
        if score <= 0.2:
            return ExpertLevel.EXPERT_LEVEL_LOW
        elif score <= 0.5:
            return ExpertLevel.EXPERT_LEVEL_MEDIUM
        return ExpertLevel.EXPERT_LEVEL_HIGH


class ComplexityLevel(enum.Enum):
    COMPLEXITY_LEVEL_LOW = "COMPLEXITY_LEVEL_LOW"
    COMPLEXITY_LEVEL_MEDIUM = "COMPLEXITY_LEVEL_MEDIUM"
    COMPLEXITY_LEVEL_HIGH = "COMPLEXITY_LEVEL_HIGH"


class ApprovalRequirement(enum.Enum):
    ANY_APPROVAL = "ANY_APPROVAL"
    EXPERT_APPROVAL = "EXPERT_APPROVAL"


class RequirementWarning(enum.Enum):
    EXPAND_TO_LOW_SCORE_OWNERS = "EXPAND_TO_LOW_SCORE_OWNERS"
    """The people who can approve the files are expanded to low expert score
    owners.

    When a file doesn't have enough experts, FlexReview expands the
    people who can approve to owners with low expert score.
    """

    EXPAND_TO_ALL_OWNERS = "EXPAND_TO_ALL_OWNERS"
    """The people who can approve the files are expanded to all owners.

    Because there are not many owners who touched the files, the people who can
    approve are expanded to all owners.
    """

    UNRESOLVED_OWNERS = "UNRESOLVED_OWNERS"
    """Some owners cannot be identified as a GitHub team / GitHub user."""


class OwnerInfo(StrictBaseModel):
    owners: list[OwnerName]
    "GitHub usernames / team names / emails of the owners"

    github_user_ids: list[int]
    "Resolved owner GithubUsers"

    unresolved_owner_names: list[OwnerName]
    "Owners that cannot be resolved to GithubUser"


class FileReviewRequirement(StrictBaseModel):
    file_path: FilePath
    author_normalized_expert_score: float
    author_expert_level: ExpertLevel
    owner_info: OwnerInfo

    complexity_level: ComplexityLevel
    approval_requirement: ApprovalRequirement

    candidate_github_users: list[UserExpertScore]
    """Users who can approve this file, with expert scores.

    This can be empty in two cases:

    * The file doesn't have an owner specified and ANY_APPROVAL. In this case,
      anybody can approve. In this case OwnerInfo.owners is empty.
    * The file has an owner specified in CODEOWNERS, but it doesn't resolve to
      any user. In this case OwnerInfo.owners is not empty.
    """

    requirement_warnings: list[RequirementWarning]
    """Warnings that are found while calculating the review requirement."""


class SuggestedReviewer(StrictBaseModel):
    github_user_id: int
    combined_expert_score: float
    review_load: int = 0


class SuggestedReviewerSet(StrictBaseModel):
    covered_file_paths: set[FilePath]
    suggested_reviewers: list[SuggestedReviewer]


class SuggestReviewerResult(StrictBaseModel):
    suggested_reviewer_sets: list[SuggestedReviewerSet]
    no_candidate_files: set[FilePath]

    def get_referenced_github_user_ids(self) -> set[int]:
        """Return GithubUser IDs referenced in this result."""
        unresolved: set[int] = set()
        for srs in self.suggested_reviewer_sets:
            for sr in srs.suggested_reviewers:
                unresolved.add(sr.github_user_id)
        return unresolved


class FlexReviewSuggestionResult(StrictBaseModel):
    file_review_requirements: list[FileReviewRequirement]
    suggest_reviewer_result: SuggestReviewerResult
