from __future__ import annotations

from textwrap import dedent
from typing import Literal

from pydantic import Field

from schema import BaseModel


class ReleaseEnvironmentConfig(BaseModel):
    version: Literal["v0"] = "v0"

    github_workflow_id: int | None = Field(
        default=None,
        description=dedent(
            """
            Id of the GitHub workflow.
            """
        ),
    )

    buildkite_organization: str | None = Field(
        default=None,
        description="Organization slug in Buildkite.",
    )
    buildkite_pipeline: str | None = Field(
        default=None,
        description="Pipeline slug in Buildkite.",
    )

    notify_pr_owners_after_deployment_success: bool = Field(
        default=False,
        description="Whether to notify PR owners after deployment succeeds.",
    )

    webhook_url: str | None = Field(
        default=None, description="URL to call to trigger the pipeline"
    )

    custom_workflow_params_template: dict = Field(
        default={},
        description="Template for custom workflow variables.",
    )
