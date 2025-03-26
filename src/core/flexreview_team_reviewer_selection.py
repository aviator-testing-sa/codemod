from __future__ import annotations

import enum

from lib.pagerduty import PagerDutyUser
from schema import StrictBaseModel

from .flexreview_config import AssignmentMethod


class UsedAssignmentMethod(enum.Enum):
    """Used assignment method for the team.

    SIDE_EFFECT means that the reviewer picked up by other team coincidentally satisfies
    this team's reviewer requirement. For example, let's say team A uses the PagerDuty
    based assignment, and we pick up a reviewer based on that. That reviewer also
    belongs to team B. In this case, we use that reviewer for team B as well, and the
    team B's assignment method becomes PAGER_DUTY_SCHEDULE_SIDE_EFFECT.
    """

    ALREADY_ASSIGNED = "ALREADY_ASSIGNED"
    """The team already has an assigned reviewer.

    This is the case where PullRequestTeamMetadata.assigned_github_user_id is already
    set.

    Let's say we already picked a reviewer for team A, and that team A is added to the
    reviewer list again. In this case, since we already have a reviewer, we do not
    assign a new reviewer.
    """
    EXISTING_REVIEWER = "EXISTING_REVIEWER"
    """The team reviewer is chosen from the existing reviewers.

    This is the case where PullRequestTeamMetadata.assigned_github_user_id is not set,
    but picked up from the existing reviewers.
    """
    PAGER_DUTY_SCHEDULE = "PAGER_DUTY_SCHEDULE"
    PAGER_DUTY_SCHEDULE_SIDE_EFFECT = "PAGER_DUTY_SCHEDULE_SIDE_EFFECT"
    LOAD_BALANCE = "LOAD_BALANCE"
    LOAD_BALANCE_SIDE_EFFECT = "LOAD_BALANCE_SIDE_EFFECT"
    EXPERT = "EXPERT"
    EXPERT_SIDE_EFFECT = "EXPERT_SIDE_EFFECT"
    NO_CANDIDATE = "NO_CANDIDATE"


class CheckAssignedReviewerResult(StrictBaseModel):
    """Result on checking PullRequestTeamMetadata.assigned_github_user_id."""

    already_assigned_team_and_assignee: dict[int, int | None]
    """GithubTeam.id to GithubUser.id mappings that are already assigned.

    If not exist, the value becomes None.
    """


class AssociateExistingReviewerResult(StrictBaseModel):
    """Result on reusing existing reviewer as a team representative."""

    existing_reviewer_github_user: dict[int, list[int]]
    """Reviewer GithubUser.id that teams have."""

    oncall_assignment_teams: list[int]
    """GithubTeam.ids that are using oncall assignment.

    These teams cannot use the existing reviewers as-is since they need to check the
    oncall calendar.
    """


class OncallChoiceResult(StrictBaseModel):
    """Result for the oncall based assignment."""

    # TODO(masaya): Better to add a PagerDuty error message, error code, and request ID.
    # In order to do this, we need to modify the PD client lib...

    pager_duty_oncallers: dict[str, list[list[PagerDutyUser]]]
    """PagerDuty escalation policy IDs to oncalls."""

    unrecognized_user_emails: list[str]
    """The list of email addresses that are not recognized as an Aviator user."""

    users_without_github_user: list[int]
    """User.id that do not have an associated GithubUser."""

    chosen_github_users: dict[int, int]
    """GithubTeam.id to GithubUser.id mappings that are chosen as a reviewer."""

    side_effect_reviewer_choice: dict[int, int]
    """GithubTeam.id to GithubUser.id mappings that are chosen as a reviewer as a side
    effect.

    Even if the team is not using the oncall assignment, other team's oncall based
    assignment can satisfy the reviewer requirement (e.g. the reviewer is in the both
    teams). In this case, that reviewer is chosen as a reviewer for other teams as well.
    """


class LoadBalancingChoiceResult(StrictBaseModel):
    """Result for the load-balancing based assignment."""

    team_to_candidates: dict[int, list[int]]
    """GithubTeam.id to GithubUser.id mappings that are candidates for the assignment."""

    review_loads: dict[int, int]
    """GithubUser.id to the number of reviews that are currently assigned."""

    chosen_github_users: dict[int, int]
    """GithubTeam.id to GithubUser.id mappings that are chosen as a reviewer."""

    side_effect_reviewer_choice: dict[int, int]
    """GithubTeam.id to GithubUser.id mappings that are chosen as a reviewer as a side
    effect.

    Even if the team is not using the load-balancing assignment, other team's assignment
    can satisfy the reviewer requirement (e.g. the reviewer is in the both teams). In
    this case, that reviewer is chosen as a reviewer for other teams as well.
    """


class ExpertScoreChoiceResult(StrictBaseModel):
    """Result for the expert score based assignment."""

    chosen_github_users: dict[int, int]
    """GithubTeam.id to GithubUser.id mappings that are chosen as a reviewer."""

    chosen_github_user_scores: dict[int, float]
    """GithubUser.id to expert score for chosen users."""

    side_effect_reviewer_choice: dict[int, int]
    """GithubTeam.id to GithubUser.id mappings that are chosen as a reviewer as a side
    effect.
    """


class TeamReviewerSelectionResult(StrictBaseModel):
    """Per-team reviewer selection result."""

    github_team_id: int
    configured_assignment_method: AssignmentMethod
    used_assignment_method: UsedAssignmentMethod
    final_github_user_id: int | None
    """The picked reviewer GithubUser.id.

    This can be None if the assignment logic cannot pick up a reviewer.
    """


class ReviewerSelectionResult(StrictBaseModel):
    """Reviewer selection result."""

    per_team_selection_results: list[TeamReviewerSelectionResult]

    check_assigned_reviewer_result: CheckAssignedReviewerResult
    associate_existing_reviewer_result: AssociateExistingReviewerResult
    oncall_choice_result: OncallChoiceResult
    load_balancing_choice_result: LoadBalancingChoiceResult
    expert_score_choice_result: ExpertScoreChoiceResult
