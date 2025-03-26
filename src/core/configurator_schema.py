from __future__ import annotations

import enum

import typing_extensions as TE
from pydantic import Field, field_validator

from flaky.spec import TestDeck
from pilot.spec.scenario import Scenario
from schema import StrictBaseModel


class Labels(StrictBaseModel):
    trigger: str = Field(
        default="mergequeue",
        description="This label is used to identify that a pull request is ready to be processed by Aviator. "
        "Once labeled, Aviator will verify that the PR has passed all the required conditions and "
        "then merge the PR.",
    )
    skip_line: str | None = Field(
        default="mergequeue-priority",
        description="When tagged with this label, Aviator will move the PR to the front of the queue.",
    )
    merge_failed: str | None = Field(
        default="blocked",
        description="If the pull request fails to merge, Aviator will add this label.",
    )
    skip_delete_branch: str | None = Field(
        default="",
        description="If `delete_branch` is enabled, Aviator will skip deleting a branch with this label after merging.",
    )


class PreconditionMatchType(str, enum.Enum):
    TITLE = "title"
    BODY = "body"


class PreconditionMatch(StrictBaseModel):
    type: PreconditionMatchType = Field(
        description="Which field to match the regex against."
    )
    regex: str | list[str] = Field(
        description="Either a single or list of regexes to match."
    )


class Validations(StrictBaseModel):
    name: str = Field(
        default="",
        description="Name of the custom validation rule.",
    )
    match: PreconditionMatch = Field(
        description="Contains type and regex details for the validation rule."
    )


class CustomRequiredChecks(StrictBaseModel):
    name: str = Field(description="Name of the required check.")
    acceptable_statuses: list[str] = Field(
        description="An optional list of acceptable statuses for the required check."
    )


class Preconditions(StrictBaseModel):
    validations: list[Validations] | None = Field(
        default=[],
        description="Custom validation rules using regexes for the PR body or title.",
    )
    number_of_approvals: int | None = Field(
        default=1,
        description="Minimum number of reviewers that should approve the PR before it can qualify to be merged.",
    )
    required_checks: list[str | CustomRequiredChecks] | None = Field(
        default=[],
        description="Checks that need to pass before Aviator will merge the PR. "
        "Supports shell wildcard (glob) patterns. "
        "Also supports other acceptable CI statuses if your repo uses conditional status checks.",
    )
    use_github_mergeability: bool | None = Field(
        default=True,
        description="Determines whether to use the default required checks specified in branch protection rules "
        "on GitHub. When this setting is enabled, Aviator will ignore `required_checks`.",
    )
    conversation_resolution_required: bool | None = Field(
        default=False,
        description="Determines whether Aviator will queue the PR only after all conversations are resolved.",
    )


class ParallelMode(StrictBaseModel):
    use_affected_targets: bool | None = Field(
        default=False,
        description="If enabled, allows using affected targets in your repository.",
    )
    use_fast_forwarding: bool | None = Field(
        default=False,
        description="If enabled, uses fast forwarding to merge PRs.",
    )
    max_parallel_builds: int | None = Field(
        default=0,
        description="The maximum number of builds that Aviator will run at any time. Defaults to no limit.",
    )
    #: The max depth for any paused base branches. It must be less than max_parallel_builds.
    #: - if None, there is no change in behavior - we count all PRs in calculating queue depth
    #: - if 0, no PRs should be queued/processed on a paused base branch
    #: - otherwise, this value should represent the max queue depth for all paused base branches combined.
    max_parallel_paused_builds: int | None = Field(
        default=None,
        description="Must be less than `max_parallel_builds`. Defaults to null. "
        "The maximum number of PRs in a paused state that Aviator will create draft PRs for. "
        "If set to 0, Aviator will not create any draft PRs on paused base branches. "
        "If set to null there will be no specific limit for paused PRs. "
        "The number of paused draft PRs always counts towards the cap set by `max_parallel_builds`.",
    )
    max_requeue_attempts: int | None = Field(
        default=0,
        description="The maximum number of times Aviator will requeue a CI run after failing. "
        "Note that PRs will only be requeued if the original PR CI is passing but the draft PR CI fails. "
        "Defaults to no requeuing.",
    )
    update_before_requeue: bool | None = Field(
        default=False,
        description="Whether to update the PR with the base branch when doing an auto-requeue. "
        "This is only applicable if `max_requeue_attempts` is set. ",
    )
    stuck_pr_label: str | None = Field(
        default="",
        description="The label that Aviator will add if it determines a PR to be stuck.",
    )
    stuck_pr_timeout_mins: int | None = Field(
        default=0,
        description="Aviator will determine the PR to be stuck after the specified timeout and dequeue it. "
        "A stuck state in parallel mode is when the draft PR has passed CI but the original PR's CI "
        "is still pending. Defaults to 0, which means that Aviator will dequeue the PR immediately.",
    )
    block_parallel_builds_label: str | None = Field(
        default="",
        description="Once added to a PR, no further draft PRs will be built on top of it "
        "until that PR is merged or dequeued.",
    )
    check_mergeability_to_queue: bool | None = Field(
        default=False,
        description="If enabled, Aviator will only queue the PR if it passes all mergeability checks.",
    )
    override_required_checks: None | (list[str | CustomRequiredChecks]) = Field(
        default=[],
        description="Use this attribute if you would like different checks for your original PRs and draft PRs. "
        "The checks defined here will be used for the draft PRs created in Parallel mode. "
        "Supports shell wildcard (glob) patterns. "
        "Also supports other acceptable CI statuses if your repo uses conditional status checks.",
    )
    batch_size: int | None = Field(
        default=1,
        description="The number of queued PRs batched together for a draft PR CI run.",
    )
    batch_max_wait_minutes: int | None = Field(
        default=0,
        description="The time to wait before creating the next batch of PRs if "
        "there are not enough queued PRs to create a full batch.",
    )
    require_all_draft_checks_pass: bool | None = Field(
        default=False,
        description="Determines if Aviator will enforce all checks to pass for the constructed draft PRs. "
        "If true, any single failing test will cause the draft PR to fail. "
        "These checks include the ones we receive status updates for via GitHub. "
        "This may work well if your repo has conditional checks. "
        "Requires at least one check to be present.",
    )
    skip_draft_when_up_to_date: bool | None = Field(
        default=True,
        description="Skips creation of the draft PR to validate the CI if the original PR is already up to date "
        "and no other PR is currently queued. This is usually a good optimization to avoid "
        "running extra CI cycles. ",
    )
    use_active_reviewable_pr: bool | None = Field(
        default=False,
        description="Determines whether Aviator will use reviewable PRs instead of draft PRs to validate the "
        "CI in the batch codemix. ",
    )
    use_optimistic_validation: bool | None = Field(
        default=True,
        description="If the CI of the top draft PR is still running but a subsequent draft PR passes, "
        "then optimistically use that success result to validate the top PR as passing.",
    )
    optimistic_validation_failure_depth: int | None = Field(
        default=1,
        description="Requires `use_optimistic_validation` to be true. "
        "If the CI of the top draft PR has failed, wait for subsequent draft PR CIs to also fail "
        "before dequeuing the PR. The number represents how many draft PRs do we wait to fail "
        "before dequeuing a PR. For example, if set to 1, it will dequeue immediately after the top "
        "draft PR fails, but if set to 2, it will wait for one more subsequent draft PR to fail "
        "before dequeuing the top PR. Value should always be 1 or larger.",
    )
    use_parallel_bisection: bool | None = Field(
        default=None,
        description="If this config is set, the bisected batches will be run in parallel "
        "to identify the the culprit PR and requeue all the clean PRs.",
    )
    max_parallel_bisected_builds: int | None = Field(
        default=None,
        description="Whether we should have a separate quota for bisected builds. If "
        "this value is set, the bisected builds will run in parallel and not cannibalize "
        "the quota for regular builds",
    )
    max_bisection_attempts: int | None = Field(
        default=None,
        description="If set, the PR is marked as failed after getting bisected this many "
        "number of times. Otherwise the PR will only be marked as failed when it's run on "
        "a bisected batch size of 1.",
    )
    max_bisected_batch_size: int | None = Field(
        default=None,
        description="Max number of PRs in a bisected batch. If not set, the batches will be "
        "split into two batches.",
    )


class MergeMode(StrictBaseModel):
    type: TE.Literal["default", "parallel", "no-queue"] = Field(
        default="default",
        description="Determines the mode. Options are: default, parallel, no-queue.",
    )
    parallel_mode: ParallelMode | None = Field(
        default=None, description="Parallel mode configuration options."
    )


class AutoUpdate(StrictBaseModel):
    enabled: bool | None = Field(
        default=False,
        description="If enabled, Aviator will keep your branches up to date with the main branch.",
    )
    label: str | None = Field(
        default="",
        description="Aviator will only keep branches with this label up to date with the main branch. "
        "Leave empty to auto merge without a label. If no label is provided and auto_update is enabled, "
        "by default Aviator will update all PRs.",
    )
    max_runs_for_update: int | None = Field(
        default=0,
        description="The maximum number of times Aviator will update your branch. Default of 0 means there is no limit.",
    )


class TitleRegex(StrictBaseModel):
    pattern: str = Field(description="The pattern to replace in the PR title.")
    replace: str = Field(
        description="The string to replace the pattern with in the PR title."
    )


class MergeCommit(StrictBaseModel):
    use_title_and_body: bool | None = Field(
        default=True,
        description="Determines whether Aviator will replace default commit messages offered by GitHub.",
    )
    cut_body_before: str | None = Field(
        default="",
        description="A marker string to cut the PR body description. "
        "The commit message will contain the PR body after this marker. Leave empty for no cropping.",
    )
    cut_body_after: str | None = Field(
        default="",
        description="A marker string to cut the PR body description. "
        "The commit message will contain the PR body before this marker. Leave empty for no cropping.",
    )
    apply_title_regexes: list[TitleRegex] | None = Field(
        default=None,
        description="Contains the strings pattern and replace to indicate what pattern to replace in the PR title. "
        "This will be applied to the merge commit. In parallel mode, "
        "this also applies to the draft PR title.",
    )
    strip_html_comments: bool | None = Field(
        default=False,
        description="Strips out the hidden HTML comments from the commit message when merging the PR.",
    )
    include_coauthors: bool | None = Field(
        default=False,
        description="Include coauthors (if any) in the commit message when merging the PR.",
    )


class OverrideLabels(StrictBaseModel):
    squash: str | None = Field(
        default=None,
        description="If marked with this label, the PR will be squashed and merged.",
    )
    merge: str | None = Field(
        default=None,
        description="If marked with this label, the PR will be merged using a merge commit.",
    )
    rebase: str | None = Field(
        default=None,
        description="If marked with this label, the PR will be rebased and merged.",
    )


class MergeStrategyNameEnum(str, enum.Enum):
    SQUASH = "squash"
    MERGE = "merge"
    REBASE = "rebase"


class MergeStrategy(StrictBaseModel):
    name: MergeStrategyNameEnum = Field(
        default=MergeStrategyNameEnum.MERGE,
        description="Defines the merge strategy to use.",
    )
    override_labels: OverrideLabels | None = Field(
        default=None,
        description="When a PR is flagged with these labels along with the trigger label, "
        "Aviator will merge the PR using this strategy instead of the default strategy."
        "Leave empty for no overrides.",
    )
    use_separate_commits_for_stack: bool | None = Field(
        default=False,
        description="If enabled, uses independent commits for stacked PRs. This requires setting up "
        "Rulesets in GitHub and allow Aviator to bypass branch protection rules. Otherwise GitHub "
        "blocks commits from merging without approval and CI completion",
    )


PublishCadence = TE.Literal["always", "ready", "queued", "never"]


class StatusComment(StrictBaseModel):
    #: When to publish the sticky status comment.
    #: - always: Publish the status comment when the pull request is opened.
    #: - ready: Publish the status comment when the pull request is ready for
    #:   review.
    #: - queued: Only publish the status comment when the pull request is queued.
    #: - never: Never publish the status comment.
    #: Once the status comment is published, it will be updated in-place.
    publish: PublishCadence = Field(
        default="always",
        description="When to publish the sticky status comment. Must be one of always, ready, queued, or never.",
    )

    #: A custom message to include in the status comment whenever the pull
    #: request is open (not queued).
    open_message: str | None = Field(
        default="",
        description="An optional message to include in the Aviator status comment when "
        "the pull request is open (not queued).",
    )

    #: A custom message to include in the status comment whenever the pull
    #: request is queued.
    queued_message: str | None = Field(
        default="",
        description="An optional message to include in the Aviator status comment when "
        "the pull request is queued.",
    )

    #: A custom message to include in the status comment whenever the pull
    #: request is blocked.
    blocked_message: str | None = Field(
        default="",
        description="An optional message to include in the Aviator status comment when "
        "the pull request is blocked.",
    )


class MergeRules(StrictBaseModel):
    labels: Labels = Field(description="Configure labels for your merge queue.")
    update_latest: bool | None = Field(
        default=True,
        description="Determines whether Aviator will merge the latest base branch into the current branch of "
        "your PR before verifying the CI statuses. This is only compatible with the default and "
        "no-queue merge modes.",
    )
    delete_branch: bool | None = Field(
        default=False,
        description="Determines whether Aviator will delete the branch after merging.",
    )
    use_rebase: bool | None = Field(
        default=False,
        description="Determines if Aviator will use rebase to update PRs. "
        "Please note that you also need to enable `update_latest` for rebase to work.",
    )
    publish_status_check: PublishCadence = Field(
        default="ready",
        description="Determines if Aviator will publish a status check back on the PR. "
        "This status check represents the current state of PR in the Aviator queue.\n\n"
        "Possible values: \n"
        "always: Post the status check whenever the pull request is opened.\n"
        "ready: Publish the status check when the pull request is ready for review.\n"
        "queued: Post the status comment when the pull request is queued.\n"
        "never: Disable the status comment.\n\n"
        "For backwards compatibility this value also supports a boolean.\n"
        "True is the same as ready.\n"
        "False is the same as never.",
    )
    status_comment: StatusComment = Field(
        default=StatusComment(),
        description="Aviator posts a status comment on every PR by default.",
    )
    enable_comments: bool | None = Field(
        default=True,
        description="Determines if Aviator can add comments on the PR to describe the actions performed "
        "on the PR. Aviator comments include information such as failure reasons and the "
        "position of the PR in the queue.",
    )
    ci_timeout_mins: int | None = Field(
        default=0,
        description="The time before we determine that the CI has timed out. Defaults to no time out.",
    )
    require_all_checks_pass: bool | None = Field(
        default=False,
        description="Determines if Aviator will enforce all checks to pass. "
        "If true, any single failing test will cause the PR to fail. "
        "These checks include the ones we receive status updates for via GitHub. "
        "This may work well if your repo has conditional checks. "
        "Requires at least one check to be present.",
    )
    require_skip_line_reason: bool | None = Field(
        default=False,
        description="If enabled, a reason is required when marking a PR as skip line. "
        "Provide a reason via Aviator slash command as a GitHub comment: "
        "`/aviator merge --skip-line=<insert reason for skipping>`.",
    )
    preconditions: Preconditions | None = Field(
        default=Preconditions(),
        description="Configuration settings to customize preconditions.",
    )
    merge_mode: MergeMode | None = Field(
        default=MergeMode(),
        description="Configuration settings to customize the merge mode.",
    )
    auto_update: AutoUpdate | None = Field(
        default=None,
        description="Configuration settings for auto-updating PRs.",
    )
    merge_commit: MergeCommit | None = Field(
        default=MergeCommit(),
        description="Configuration settings to customize merge commits.",
    )
    merge_strategy: MergeStrategy | None = Field(
        default=MergeStrategy(),
        description="Configuration settings to customize merge strategy.",
    )
    base_branches: list[str] | None = Field(
        default=[],
        description="These branches are the ones that Aviator will monitor as valid branches to merge into. "
        "Defaults to your repository default branch as configured on GitHub. Regexes are allowed.",
    )

    @field_validator("publish_status_check", mode="before")
    @classmethod
    def validate_publish_status_check(cls, value: object) -> str:
        if not value:
            return "never"  # For backward compatibility
        if isinstance(value, bool):
            return "ready" if value else "never"
        # We let the pydantic validator handle the rest of the validation
        return value  # type: ignore[return-value]


class Configuration(StrictBaseModel):
    merge_rules: MergeRules
    scenarios: list[Scenario] = []
    testdeck: TestDeck | None = None

    # This is deprecated but we are keeping this as the old configs
    # have this field as a null value.
    flakybot: TestDeck | None = None


class ConfiguratorSchema(StrictBaseModel):
    merge_rules: MergeRules = Field(
        description="Set up customized merge rules.",
    )
    scenarios: list[Scenario] = Field(
        default=[],
        description="Set up customized scenarios via Pilot.",
    )
    testdeck: TestDeck | None = Field(
        default=None,
        description="Set up customized rules for TestDeck.",
    )

    # This is deprecated but we are keeping this as the old configs
    # have this field as a null value.
    flakybot: TestDeck | None = None

    # We never ended up using the version field and it was removed from
    # the pydantic-yaml library that we were using.
    version: object | None = None
