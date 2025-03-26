"""Mix-in for deprecated columns"""

from __future__ import annotations

import datetime
import typing

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, declarative_mixin, declared_attr, deferred

from basemodel import BaseModel, IDModel

type JSONType = (
    str
    | float
    | bool
    | typing.Mapping[typing.Any, typing.Any]
    | list[typing.Any]
    | None
)

# See
# https://docs.sqlalchemy.org/en/14/orm/declarative_mixins.html#mixing-in-deferred-column-property-and-other-mapperproperty-classes
# for deferred mixins.


@declarative_mixin
class DeprecatedChangeSetRun:
    @declared_attr
    def DEPRECATED_status(self) -> Mapped[str | None]:
        return deferred(
            sa.Column(
                "status",
                sa.Text,
                nullable=True,
            )
        )

    @declared_attr
    def DEPRECATED_name(self) -> Mapped[str | None]:
        return deferred(
            sa.Column(
                "name",
                sa.Text,
                nullable=True,
            )
        )

    @declared_attr
    def DEPRECATED_target_url(self) -> Mapped[str | None]:
        return deferred(
            sa.Column(
                "target_url",
                sa.Text,
                nullable=True,
            )
        )

    @declared_attr
    def DEPRECATED_message(self) -> Mapped[str | None]:
        return deferred(
            sa.Column(
                "message",
                sa.Text,
                nullable=True,
            )
        )


@declarative_mixin
class DeprecatedChangeSetRunCommit:
    @declared_attr
    def DEPRECATED_check_run_id(self) -> Mapped[int | None]:
        return deferred(
            sa.Column(
                "check_run_id",
                sa.BigInteger,
                nullable=True,
            )
        )


@declarative_mixin
class DeprecatedGithubRepo:
    @declared_attr
    def DEPRECATED_verify_all_tests(self) -> Mapped[bool]:
        return deferred(
            sa.Column(
                "verify_all_tests",
                sa.Boolean,
                nullable=False,
                server_default=sa.false(),
            )
        )

    @declared_attr
    def DEPRECATED_approvals(self) -> Mapped[int]:
        return deferred(
            sa.Column(
                "approvals",
                sa.Integer,
                nullable=False,
                server_default="1",
            )
        )

    @declared_attr
    def DEPRECATED_delete_branch(self) -> Mapped[bool]:
        return deferred(
            sa.Column(
                "delete_branch",
                sa.Boolean,
                nullable=False,
                server_default=sa.false(),
            )
        )

    @declared_attr
    def DEPRECATED_merge_latest(self) -> Mapped[bool]:
        return deferred(
            sa.Column(
                "merge_latest",
                sa.Boolean,
                nullable=False,
                server_default=sa.true(),
            )
        )

    @declared_attr
    def DEPRECATED_merge_strategy(self) -> Mapped[str]:
        return deferred(
            sa.Column(
                "merge_strategy",
                sa.Text,
                nullable=False,
                server_default="squash",
                doc=(
                    "The merge strategy to use when merging pull requests for this "
                    "repository. Can be overridden on a per-pull-request basis using "
                    "override labels (e.g., GithubLabel with purpose 'rebase')."
                ),
            )
        )

    @declared_attr
    def DEPRECATED_auto_sync(self) -> Mapped[bool]:
        return deferred(
            sa.Column(
                "auto_sync",
                sa.Boolean,
                nullable=False,
                server_default=sa.false(),
            )
        )

    @declared_attr
    def DEPRECATED_auto_sync_cap(self) -> Mapped[int]:
        return deferred(
            sa.Column(
                "auto_sync_cap",
                sa.Integer,
                nullable=False,
                server_default="0",
            )
        )

    # HISTORICAL NOTE
    # Way back in the before times, the feature that is now known as parallel
    # mode was called "parallel mode" (and briefly "batch mode"). The parallel mode
    # terminology has since been removed, and "batch mode" refers to something
    # else (combining multiple PRs and testing them together in a single BotPR).
    @declared_attr
    def DEPRECATED_parallel_mode(self) -> Mapped[bool]:
        return deferred(
            sa.Column(
                "beast_mode",
                sa.Boolean,
                nullable=False,
                server_default=sa.false(),
                doc=(
                    "If enabled, MergeQueue will create a new BotPR for each queued "
                    "pull request. Each BotPR contains the changes from the original "
                    "PR as well as all the changes from the previous BotPRs in the "
                    "queue. This mode enables higher throughput because PRs are tested "
                    "in parallel (rather than sequentially)."
                ),
            )
        )

    @declared_attr
    def DEPRECATED_parallel_mode_cap(self) -> Mapped[int]:
        return deferred(
            sa.Column(
                "beast_mode_cap",
                sa.Integer,
                nullable=False,
                server_default="0",
                doc=(
                    "The maximum number of parallel BotPRs for the repository. If more "
                    "than this number of PRs are queued, BotPRs for the most recently "
                    "queued PRs won't be created until the number of BotPRs is below "
                    "this number. A value of 0 means no limit."
                ),
            )
        )

    @declared_attr
    def DEPRECATED_check_mergeability_to_queue(self) -> Mapped[bool]:
        return deferred(
            sa.Column(
                "beast_check_mergeability",
                sa.Boolean,
                nullable=False,
                server_default=sa.false(),
                doc=(
                    "If enabled, MergeQueue will wait for the original PR to be "
                    "considered mergeable before adding it to the queue."
                ),
            )
        )

    @declared_attr
    def DEPRECATED_parallel_mode_optimistic_check(self) -> Mapped[bool]:
        return deferred(
            sa.Column(
                "beast_optimistic_check",
                sa.Boolean,
                nullable=False,
                server_default=sa.true(),
                doc=(
                    "With optimistic check, we also consider the CI status of collective batch vs the first one"
                    "So if there is Batch 1 and 2 corresponding to PR 1 and 2, and if the CI for Batch 2 passes"
                    " before CI for Batch 1, then we merge PR 1 and 2 given that we know at least they are "
                    "eventually consistent. This setting is also used to skip first draft PR creation."
                ),
            )
        )

    @declared_attr
    def DEPRECATED_optimistic_validation_failure_depth(self) -> Mapped[int]:
        return deferred(
            sa.Column(
                "optimistic_validation_failure_depth",
                sa.Integer,
                nullable=False,
                server_default="1",
                doc=(
                    "This is the number of PRs we will consider for optimistic validation failure."
                    "For example, if this is set to 2, then we will consider the CI status of the first"
                    "PR and the second PR. If the CI status of the first PR is failure, then we will"
                    "not immediately reset the queue and wait for second PR to fail as well."
                    "If the CI status of the second PR is failure as well, then we will reset the queue."
                ),
            )
        )

    @declared_attr
    def DEPRECATED_skip_draft_when_up_to_date(self) -> Mapped[bool]:
        return deferred(
            sa.Column(
                "skip_draft_when_up_to_date",
                sa.Boolean,
                nullable=False,
                server_default=sa.true(),
                doc=(
                    "This enables teams using parallel mode to skip creating the Draft PR for first PR"
                    "if the PR is already up-to-date with the base branch."
                ),
            )
        )

    @declared_attr
    def DEPRECATED_fast_forward(self) -> Mapped[bool]:
        return deferred(
            sa.Column(
                "fast_forward",
                sa.Boolean,
                nullable=False,
                server_default=sa.false(),
                doc=(
                    "This enables teams using parallel mode to fast-forward merge the Draft PR"
                    "while keeping the history linear. In this approach, the CI is not re-triggered"
                    "as the commit SHA is just copied over to master / main branch."
                ),
            )
        )

    @declared_attr
    def DEPRECATED_rebase_pull(self) -> Mapped[bool]:
        return deferred(
            sa.Column(
                "rebase_pull",
                sa.Boolean,
                nullable=False,
                server_default=sa.false(),
            )
        )

    @declared_attr
    def DEPRECATED_disable_comments(self) -> Mapped[bool]:
        return deferred(
            sa.Column(
                "disable_comments",
                sa.Boolean,
                nullable=False,
                server_default=sa.false(),
            )
        )

    @declared_attr
    def DEPRECATED_commit_pr_body(self) -> Mapped[bool]:
        return deferred(
            sa.Column(
                "commit_pr_body",
                sa.Boolean,
                nullable=False,
                server_default=sa.false(),
            )
        )

    @declared_attr
    def DEPRECATED_comment_on_latest(self) -> Mapped[bool]:
        return deferred(
            sa.Column(
                "comment_on_latest",
                sa.Boolean,
                nullable=False,
                server_default=sa.false(),
                doc=(
                    "Some users have this value set, keep this as read-only. There is no way for users to update this value."
                    "Used to post a comment in default sequential mode if the PR is already up to date."
                ),
            )
        )

    @declared_attr
    def DEPRECATED_ci_timeout(self) -> Mapped[int]:
        return deferred(
            sa.Column(
                "ci_timeout",
                sa.Integer,
                nullable=False,
                server_default="0",
            )
        )

    @declared_attr
    def DEPRECATED_stuck_timeout(self) -> Mapped[int]:
        return deferred(
            sa.Column(
                "stuck_timeout",
                sa.Integer,
                nullable=False,
                server_default="0",
            )
        )

    @declared_attr
    def DEPRECATED_max_requeue_attempts(self) -> Mapped[int]:
        return deferred(
            sa.Column(
                "max_requeue_attempts",
                sa.Integer,
                nullable=False,
                server_default="0",
                doc=(
                    "The maximum number of times a PR will be requeued if it fails CI "
                    "or get stuck. If 0, the PR will not be requeued. This config is only "
                    "used when parallel mode is enabled."
                ),
            )
        )

    @declared_attr
    def DEPRECATED_update_before_requeue(self) -> Mapped[bool]:
        return deferred(
            sa.Column(
                "update_before_requeue",
                sa.Boolean,
                nullable=False,
                server_default=sa.false(),
                doc=(
                    "If true, Aviator will update the PR's head branch before requeueing."
                    "This is only applicable if max_requeue_attempts is greater than 0."
                ),
            )
        )

    @declared_attr
    def DEPRECATED_process_unordered(self) -> Mapped[bool]:
        return deferred(
            sa.Column(
                "process_unordered",
                sa.Boolean,
                nullable=False,
                server_default=sa.false(),
            )
        )

    @declared_attr
    def DEPRECATED_conv_resolution(self) -> Mapped[bool]:
        return deferred(
            sa.Column(
                "conv_resolution",
                sa.Boolean,
                nullable=False,
                server_default=sa.false(),
            )
        )

    @declared_attr
    def DEPRECATED_cut_body_before(self) -> Mapped[str | None]:
        return deferred(
            sa.Column(
                "cut_body_before",
                sa.Text,
                nullable=True,
            )
        )

    @declared_attr
    def DEPRECATED_cut_body_after(self) -> Mapped[str | None]:
        return deferred(
            sa.Column(
                "cut_body_after",
                sa.Text,
                nullable=True,
            )
        )

    @declared_attr
    def DEPRECATED_use_github_mergeability(self) -> Mapped[bool]:
        return deferred(
            sa.Column(
                "use_github_mergeability",
                sa.Boolean,
                nullable=False,
                server_default=sa.true(),
            )
        )

    @declared_attr
    def DEPRECATED_target_mode(self) -> Mapped[bool]:
        return deferred(
            sa.Column(
                "target_mode",
                sa.Boolean,
                nullable=False,
                server_default=sa.false(),
            )
        )

    @declared_attr
    def DEPRECATED_batch_size(self) -> Mapped[int]:
        return deferred(
            sa.Column(
                "batch_size",
                sa.Integer,
                nullable=False,
                server_default="1",
                doc="The max number of queued PRs to batch into a BotPR.",
            )
        )

    @declared_attr
    def DEPRECATED_batch_max_wait_minutes(self) -> Mapped[int]:
        return deferred(
            sa.Column(
                "batch_max_wait_minutes",
                sa.Integer,
                nullable=False,
                server_default="0",
                doc="The max time to wait (in minutes) before creating the next batch of PRs.",
            )
        )

    @declared_attr
    def DEPRECATED_post_gh_status(self) -> Mapped[bool]:
        return deferred(
            sa.Column(
                "post_gh_status",
                sa.Boolean,
                nullable=False,
                server_default=sa.false(),
                doc=(
                    "This field is deprecated. Use the config directly that supports a string value: "
                    "repo.current_config.merge_rules.publish_status_check"
                ),
            )
        )

    @declared_attr
    def DEPRECATED_require_all_checks_pass(self) -> Mapped[bool]:
        return deferred(
            sa.Column(
                "require_all_checks_pass",
                sa.Boolean,
                nullable=False,
                server_default=sa.false(),
                doc="If true, then a PR is marked as failed if any test has failed, even if the test if not required.",
            )
        )

    @declared_attr
    def DEPRECATED_require_all_botpr_checks_pass(self) -> Mapped[bool]:
        return deferred(
            sa.Column(
                "require_all_botpr_checks_pass",
                sa.Boolean,
                nullable=False,
                server_default=sa.false(),
                doc="If true, then a Bot PR is marked as failed if any test has failed, even if the test if not required.",
            )
        )

    @declared_attr
    def DEPRECATED_strip_html_comments(self) -> Mapped[bool]:
        return deferred(
            sa.Column(
                "strip_html_comments",
                sa.Boolean,
                nullable=False,
                server_default=sa.false(),
                doc="If true, then Aviator will strip HTML comments from the PR body.",
            )
        )

    @declared_attr
    def DEPRECATED_include_coauthors(self) -> Mapped[bool]:
        return deferred(
            sa.Column(
                "include_coauthors",
                sa.Boolean,
                nullable=False,
                server_default=sa.true(),
                doc="If true, then Aviator will include co-authors in the commit message.",
            )
        )


class DeprecatedSlackDMLabelConfig(BaseModel):
    __tablename__ = "slack_dm_label_config"

    user_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("user_info.id", ondelete="CASCADE"),
        nullable=False,
    )
    opt_out_labels: Mapped[list[str]] = sa.Column(
        ARRAY(sa.Text),
        nullable=False,
    )
    opt_in_labels: Mapped[list[str]] = sa.Column(
        ARRAY(sa.Text),
        nullable=False,
    )


@declarative_mixin
class DeprecatedPullRequestTeamMetadata:
    @declared_attr
    def DEPRECATED_bot_require_approval(self) -> Mapped[bool]:
        return deferred(
            sa.Column(
                "bot_require_approval",
                sa.Boolean,
                nullable=False,
                server_default=sa.false(),
                doc="""
                If true, an automation bot sets this pull request needs an approval from
                this team.
                """,
            )
        )

    @declared_attr
    def DEPRECATED_automation_rule_active(self) -> Mapped[bool]:
        return deferred(
            sa.Column(
                "automation_rule_active",
                sa.Boolean,
                nullable=False,
                server_default=sa.false(),
                doc="""
                If true, the automation rules configured for the team should be applied
                to this pull request.
                """,
            )
        )

    @declared_attr
    def DEPRECATED_last_review_requested_at(self) -> Mapped[datetime.datetime | None]:
        return deferred(
            sa.Column(
                "last_review_requested_at",
                sa.TIMESTAMP(timezone=True),
                nullable=True,
                doc="""
                Last timestamp that this team is requested for review. This can be None if a
                GitHub hasn't assigned a team yet (PR is just created).
                """,
            )
        )

    @declared_attr
    def DEPRECATED_notified_slack_channel(self) -> Mapped[str | None]:
        return deferred(
            sa.Column(
                "notified_slack_channel",
                sa.Text,
                nullable=True,
                doc="The Slack channel that has been notified.",
            ),
        )

    @declared_attr
    def DEPRECATED_notified_slack_message_ts(self) -> Mapped[str | None]:
        return deferred(
            sa.Column(
                "notified_slack_message_ts",
                sa.Text,
                nullable=True,
                doc="The Slack message timestamp that has been notified.",
            ),
        )

    @declared_attr
    def DEPRECATED_channel_notification_id(self) -> Mapped[int | None]:
        return deferred(
            sa.Column(
                "channel_notification_id",
                sa.Integer,
                nullable=True,
            ),
        )


class DeprecatedFlexReviewConfigHistory(BaseModel):
    __tablename__ = "flexreview_config_history"

    repo_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_repo.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    account_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )
    aviator_user_id: Mapped[int | None] = sa.Column(
        sa.Integer,
        # No cascading deletion. We want to keep the history even if a user is
        # deleted.
        sa.ForeignKey("user_info.id"),
        # This can be empty. Currently in MVP, we only take config from the web UI, but
        # in the future we might want to get this from the repository. In that case, we
        # would want to have either User or GithubUser to track who modified the file.
        nullable=True,
    )
    # config_text and config_data are unparsed and parsed pair with the same
    # content. The schema is not exposed to public yet, and the config is
    # created in our code.
    config_text: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )
    config_data: Mapped[typing.Any] = sa.Column(
        sa.dialects.postgresql.JSONB,
        nullable=False,
    )


class DeprecatedCodeOwnersFileHistory(BaseModel):
    __tablename__ = "code_owners_file_history"

    repo_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_repo.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    account_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )
    # NOTE: Currently, FlexReview only supports the default branch. In order to
    # support multiple branches in the future, branch_name is set to the config
    # cache so that we can have different CODEOWNERS files in different
    # branches.
    branch_name: Mapped[str] = sa.Column(
        sa.Text, nullable=False, doc="""The branch name (e.g. 'main')"""
    )
    commit_sha: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
        doc="""
        The commit hash from which we read the CODEOWNERS file.
        """,
    )
    file_path: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
        doc="""
        The file content.

        When CODEOWNERS file is deleted or doesn't exist, this can be an empty
        string. This is necessary to indicate there's no CODEOWNERS file.
        """,
    )
    codeowners_text: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
        doc="""
        The file content.

        When CODEOWNERS file is deleted or doesn't exist, this can be an empty
        string. This is necessary to indicate there's no CODEOWNERS file.
        """,
    )


class DeprecatedCodeOwnersReviewRequirement(IDModel):
    """Mapping of modified files and owners for a pull request."""

    __tablename__ = "code_owners_review_requirement"

    pull_request_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("pull_request.id"),
        nullable=False,
        index=True,
        unique=True,
    )
    mapping: Mapped[JSONType] = sa.Column(
        sa.dialects.postgresql.JSONB,
        nullable=False,
    )


class FlexReviewReviewSuggestion(BaseModel):
    __tablename__ = "flexreview_review_suggestion"

    pull_request_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("pull_request.id"),
        nullable=False,
    )
    suggestion_result: Mapped[JSONType] = sa.Column(
        sa.dialects.postgresql.JSONB,
        nullable=False,
    )


sa.Index(
    "flexreview_review_suggestion_prid_created",
    FlexReviewReviewSuggestion.pull_request_id,
    FlexReviewReviewSuggestion.created.desc(),
)


class FlexReviewTeamReviewerSelectionHistory(BaseModel):
    __tablename__ = "flexreview_team_reviewer_selection_history"

    pull_request_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("pull_request.id"),
        nullable=False,
    )
    selection_result: Mapped[JSONType] = sa.Column(
        sa.dialects.postgresql.JSONB,
        nullable=False,
    )


sa.Index(
    "flexreview_team_reviewer_selection_history_prid_created",
    FlexReviewTeamReviewerSelectionHistory.pull_request_id,
    FlexReviewTeamReviewerSelectionHistory.created.desc(),
)
