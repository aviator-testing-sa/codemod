from __future__ import annotations

from string import Formatter
from types import SimpleNamespace

from core import models


class AtcWorkflowParamsFormatter(Formatter):
    def __init__(
        self,
        release_cut: models.ReleaseCut,
        deployment: models.Deployment | None = None,
        custom_input: dict | None = None,
    ):
        Formatter.__init__(self)

        # Sanity check
        if deployment:
            assert (
                deployment.release_candidate_version
                == release_cut.release_candidate_version
            )

        release = release_cut.release
        release_project = release.release_project

        self.aviator_values = SimpleNamespace(
            release_project_name=release_project.name,
            release_candidate_version=(
                f"{release.version}-rc{release_cut.release_candidate_version}"
            ),
            repository=release_cut.github_repo.repo_name,
            branch_name=(
                release.head_branch_name
                if release.head_branch_name
                else release_cut.github_repo.head_branch
            ),
            commit_sha=release_cut.commit_hash,
            environment=deployment.environment.name if deployment else "",
        )

        if custom_input is None:
            self.custom_input = SimpleNamespace()
        else:
            self.custom_input = SimpleNamespace(**custom_input)

    def get_value(self, key, args, kwargs):  # type: ignore[no-untyped-def]
        if isinstance(key, str):
            if key == "aviator":
                return self.aviator_values
            elif key == "input":
                return self.custom_input
            else:
                return Formatter.get_value(self, key, args, kwargs)
        else:
            return Formatter.get_value(self, key, args, kwargs)
