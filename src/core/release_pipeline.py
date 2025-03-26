from dataclasses import field

from schema import BaseModel


class ReleasePipelineTask(BaseModel):
    environment_id: int
    auto_triggered: bool = False
    dependency_env_ids: list[int] = field(default_factory=list)
    preconditions: dict[str, bool] = field(default_factory=dict)
    on_start: dict[str, bool] = field(default_factory=dict)
    on_complete: dict[str, bool] = field(default_factory=dict)


class ReleasePipelineConfig(BaseModel):
    tasks: list[ReleasePipelineTask] = field(default_factory=list)
    default_conditions: dict[str, bool] = field(default_factory=dict)
