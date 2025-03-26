from __future__ import annotations

from enum import Enum
from typing import Literal

from core.release_pipeline import ReleasePipelineConfig
from schema import BaseModel


class ReleaseVersionSchema(Enum):
    CALVER = "CALVER"
    SEMVER = "SEMVER"


class ReleaseProjectConfig(BaseModel):
    version: Literal["v0"] = "v0"

    release_version_schema: ReleaseVersionSchema | None = None
    require_verification: bool = False
    require_verification_for_labeled_pr: bool = False
    read_only_dashboard: bool = False
    release_pipeline_config: ReleasePipelineConfig | None = None
