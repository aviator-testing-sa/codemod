from __future__ import annotations

import enum
from textwrap import dedent
from typing import Literal

from pydantic import Field

from schema import StrictBaseModel


class AssignmentMethod(enum.Enum):
    EXPERT = "EXPERT"
    EXPERT_LOAD_BALANCE = "EXPERT_LOAD_BALANCE"
    PAGER_DUTY_SCHEDULE = "PAGER_DUTY_SCHEDULE"
    LOAD_BALANCE = "LOAD_BALANCE"
    NO_ASSIGNMENT = "NO_ASSIGNMENT"


class FlexReviewTeamConfig(StrictBaseModel):
    version: Literal["v0"] = "v0"

    reviewer_assignment_method: AssignmentMethod | None = Field(
        default=None,
        description=dedent(
            """
            How to choose a reviewer GithubUser from this team. If unset,
            defaults to NO_ASSIGNMENT.
            """
        ),
    )

    pager_duty_escalation_policy_id: str | None = Field(
        default=None,
        description=dedent(
            """
            PagerDuty escalation policy ID (e.g. "PANZZEQ").
            """
        ),
    )
