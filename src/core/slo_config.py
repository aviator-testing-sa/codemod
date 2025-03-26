from __future__ import annotations

import datetime
from textwrap import dedent
from typing import Literal

from pydantic import Field

from schema import StrictBaseModel


class SloConfig(StrictBaseModel):
    version: Literal["v0"] = "v0"

    first_response_time_slo_internal: datetime.timedelta | None = Field(
        default=None,
        description=dedent(
            """
            Target time to respond to PRs for the team internal PRs.
            """
        ),
    )

    first_response_time_slo_external: datetime.timedelta | None = Field(
        default=None,
        description=dedent(
            """
            Target time to respond to PRs for the cross-team PRs.
            """
        ),
    )

    max_modified_line: int | None = Field(
        default=None,
        description=dedent(
            """
            Target number of modified lines for the SLO eligible PRs.

            PRs that have modified more than this config are not subjected to the SLO.
            """
        ),
    )

    @property
    def is_complete(self) -> bool:
        return (
            self.first_response_time_slo_internal is not None
            and self.first_response_time_slo_external is not None
            and self.max_modified_line is not None
        )

    def merge(self, m_config: SloConfig) -> SloConfig:
        cfg = SloConfig(
            max_modified_line=self.max_modified_line or m_config.max_modified_line,
            first_response_time_slo_internal=self.first_response_time_slo_internal
            or m_config.first_response_time_slo_internal,
            first_response_time_slo_external=self.first_response_time_slo_external
            or m_config.first_response_time_slo_external,
        )

        return cfg
