from __future__ import annotations

import contextlib
import dataclasses
import datetime
import enum
import fnmatch
import functools
import typing
from collections.abc import Generator
from decimal import Decimal
from typing import TYPE_CHECKING, Union

import slack_sdk.models.blocks as slack_blocks
import sqlalchemy as sa
import structlog
import typing_extensions as TE
import yaml
from sqlalchemy import CheckConstraint, LargeBinary, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, relationship

import schema
from basemodel import BaseModel, DBModel, IDModel, MutableModel
from core import deprecated_models
from core.configurator_schema import (
    AutoUpdate,
    Configuration,
    MergeCommit,
    MergeMode,
    MergeStrategy,
    ParallelMode,
    Preconditions,
)
from core.flexreview_config import FlexReviewTeamConfig
from core.release_environment_config import ReleaseEnvironmentConfig
from core.release_project_config import ReleaseProjectConfig
from core.slo_config import SloConfig
from core.status_codes import StatusCode
from core.tech_debt import (
    MetricSeverity,
    TechDebtCategory,
    TechDebtPerDirData,
    TechDebtPerFileData,
)
from flaky.models import TestCase, TestReport
from main import app, db
from signals import bug_heuristics
from util import dbutil, time_util

if TYPE_CHECKING:
    from auth.models import Account, User


GITHUB_BASE_URL: str = app.config["GITHUB_BASE_URL"]
DEFAULT_QUEUE_LABEL: str = app.config["DEFAULT_READY_LABEL"]
DEFAULT_BLOCKED_LABEL: str = app.config["DEFAULT_FAILED_LABEL"]

target_pr_mapping = sa.Table(
    "target_pr_mapping",
    BaseModel.metadata,
    sa.Column(
        "affected_target_id",
        sa.Integer,
        sa.ForeignKey("affected_target.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    sa.Column(
        "pull_request_id",
        sa.Integer,
        sa.ForeignKey("pull_request.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    # Create a composite index to query all affected targets for a PR with an index
    # scan.
    sa.Index("target_pr_mapping_pr_id_at_id", "pull_request_id", "affected_target_id"),
)

logger = structlog.stdlib.get_logger()


class PrDependency(DBModel):
    __tablename__ = "pr_dependency"
    pull_request_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("pull_request.id"),
        nullable=False,
        primary_key=True,
    )
    depends_on_pr_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("pull_request.id"),
        nullable=False,
        primary_key=True,
    )


class BranchCommitMapping(DBModel):
    __tablename__ = "branch_commit_mapping"
    pull_request_id: Mapped[int | None] = sa.Column(
        sa.Integer,
        sa.ForeignKey("pull_request.id"),
        nullable=True,
    )
    git_commit_id: Mapped[int] = sa.Column(
        sa.BigInteger,
        sa.ForeignKey("git_commit.id"),
        primary_key=True,
        nullable=False,
    )
    branch_name: Mapped[str] = sa.Column(
        sa.Text(),
        primary_key=True,
        nullable=False,
        doc="""
        Although the branch name is also stored in pull_request, we are storing it here
        for the cases where the pull request many not be created.""",
    )


class GitCommit(BaseModel):
    __tablename__ = "git_commit"
    __table_args__ = (
        sa.Index("git_commit_unique_repo_sha", "repo_id", "sha", unique=True),
    )

    id: Mapped[int] = sa.Column(
        sa.BigInteger,
        primary_key=True,
        nullable=False,
    )
    sha: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
        index=True,
    )
    repo_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_repo.id"),
        nullable=False,
    )
    check_run_id: Mapped[int | None] = sa.Column(
        sa.BigInteger,
        nullable=True,
    )

    pull_requests: Mapped[list[PullRequest]] = relationship(
        "PullRequest",
        secondary=BranchCommitMapping.__tablename__,
        uselist=True,
        back_populates="commits",
    )
    repo: Mapped[GithubRepo] = relationship("GithubRepo", uselist=False)

    @property
    def short_sha(self) -> str:
        return self.sha[:6]

    @property
    def url(self) -> str:
        return self.repo.html_url + "/commit/" + self.sha


class ChangeSetMapping(DBModel):
    __tablename__ = "change_set_pr_mapping"
    change_set_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("change_set.id"),
        primary_key=True,
        nullable=False,
    )
    pull_request_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("pull_request.id"),
        primary_key=True,
        nullable=False,
    )
    change_set: Mapped[ChangeSet] = relationship(
        "ChangeSet", uselist=False, viewonly=True
    )


PullRequestStatus = TE.Literal[
    # Aviator is tracking the pull request, and it is open on GitHub, but it is
    # not (yet) queued for merge.
    "open",
    # The pull request has been marked for queueing, but it is not yet ready to
    # enter the merge queue because it does not (yet) meet all requirements to
    # be queued. For example, a pull request might be waiting for tests to pass
    # if the repository is configured with the `check_mergeability_to_queue`
    # setting enabled.
    "pending",
    # The pull request has been added to the queue and will merge if tests pass.
    # In parallel mode, the pull request isn't considered "fully queued" until
    # it enters the "tagged" state.
    "queued",
    # In parallel mode only, the pull request has had a MergeQueue bot PR
    # created. This state isn't valid for sequential or no-queue modes.
    "tagged",
    # The pull request could not be merged for some reason.
    "blocked",
    # The pull request was closed or merged.
    # IMPORTANT: Previously the "merged" state represented
    # both pull requests that are either merged or closed without merging.
    # To differentiate, a pull request was actually merged if the status is
    # `merged` AND the `merged_at` field is not null; if the status is `merged`
    # but `merged_at` is null, then the pull request was actually closed without
    # merging.
    "merged",
    # The closed state is used to represent pull requests that were closed.
    # This state was recently introduced and many of the old closed PRs may
    # still be marked as merged.
    "closed",
]


class CoverageRawData(IDModel):
    __tablename__ = "coverage_raw_data"

    payload: Mapped[bytes] = sa.Column(LargeBinary, nullable=False)

    repo_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_repo.id", ondelete="CASCADE"),
        nullable=False,
    )

    commit_sha: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )


class PullRequest(BaseModel):
    __tablename__ = "pull_request"

    # Need to define id column locally so that alembic can work properly when
    # we create a foreign key on this id column within the same table
    # (needed for dependent_prs below)
    id = BaseModel.id

    account_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )
    repo_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_repo.id", ondelete="CASCADE"),
        nullable=False,
    )
    creator_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_user.id"),
        nullable=False,
    )
    number: Mapped[int] = sa.Column(
        sa.Integer,
        nullable=False,
    )
    title: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )
    body: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
        server_default="",
    )

    is_draft: Mapped[bool] = sa.Column(
        sa.Boolean,
        nullable=False,
        server_default="false",
    )
    status: Mapped[PullRequestStatus] = sa.Column(
        sa.Text,
        nullable=False,
        server_default="open",
        doc=(
            "The current status of the pull request. Valid options include:"
            "open, queued, blocked, merged, tagged."
        ),
    )
    status_code: Mapped[StatusCode] = sa.Column(
        dbutil.IntEnum(StatusCode),
        nullable=False,
        server_default=str(StatusCode.UNKNOWN.value),
    )
    is_codeowner_approved: Mapped[bool] = sa.Column(
        sa.Boolean, nullable=False, server_default="false"
    )

    status_reason: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
        doc=(
            "A (possibly multi-line) description of why the PR is in the current status. "
            "This should only be set when status is blocked or pending. "
            "The text should be in Markdown format."
        ),
    )
    gh_review_decision: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
        doc=(
            "The GitHub GraphQL API value for a PullRequest's ReviewDecision."
            "This is a PullRequestReviewDecision object. If not null one of:"
            "APPROVED, CHANGES_REQUESTED, REVIEW_REQUIRED"
        ),
    )

    queued_at: Mapped[datetime.datetime] = sa.Column(
        sa.TIMESTAMP(timezone=True),
        # IMPORTANT:
        # We currently set queued_at regardless of whether the PR is queued or
        # not. This means this value is only accurate for PRs that have been
        # queued (otherwise it's junk).
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    )
    last_processed: Mapped[datetime.datetime] = sa.Column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
        onupdate=time_util.now,
    )
    creation_time: Mapped[datetime.datetime] = sa.Column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    )
    merged_at: Mapped[datetime.datetime | None] = sa.Column(
        sa.TIMESTAMP(timezone=True),
        nullable=True,
    )
    approved_at: Mapped[datetime.datetime | None] = sa.Column(
        sa.TIMESTAMP(timezone=True),
        nullable=True,
    )
    first_reviewed_at: Mapped[datetime.datetime | None] = sa.Column(
        sa.TIMESTAMP(timezone=True),
        nullable=True,
    )
    ready_for_review_at: Mapped[datetime.datetime | None] = sa.Column(
        sa.TIMESTAMP(timezone=True),
        nullable=True,
    )
    merge_base_count: Mapped[int] = sa.Column(
        sa.Integer,
        nullable=False,
        server_default="0",
    )
    skip_line: Mapped[bool] = sa.Column(
        sa.Boolean,
        nullable=False,
        server_default=sa.false(),
    )
    base_commit_sha: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
        server_default="''",
        doc="""
        The commit SHA of the base branch of the pull request.

        This may be empty for old pull requests that were processed before
        this field was added.
        """,
    )
    head_commit_sha: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
        doc="""
        The commit SHA of the head branch of the pull request.

        This may be empty for very old pull requests that were processed before
        this field was added.
        """,
    )
    merge_commit_sha: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
        doc="""
        The SHA of the merge commit of this PR. This is only set if the PR has
        been merged.
        """,
    )
    branch_name: Mapped[str] = sa.Column(
        sa.Text(),
        nullable=False,
        doc="""
        The HEAD branch name of the pull request (i.e., the topic branch).
        """,
    )
    base_branch_name: Mapped[str] = sa.Column(
        "base_branch_name",
        sa.Text(),
        nullable=False,
        doc="""
        The base branch of the PR (i.e., what is seen by GitHub).
        This might be different from target_branch_name (see below).

        This may be empty for very old pull requests that were processed before
        this field was added.
        """,
    )
    target_branch_name: Mapped[str] = sa.Column(
        "target_branch_name",
        sa.Text(),
        nullable=False,
        doc="""
        The branch that the PR is ultimately targeting for merge. This is only
        different from base_branch_name if the PR depends on another PR as a
        part of a stack. In that case, if we have an stack like
            main <- PR1 <- PR2
        then:
            PR1.base_branch_name == PR1.target_branch_name == "main"
        but for PR2, we have:
            PR2.base_branch_name == PR1.branch_name
            and PR2.target_branch_name == "main"

        We use target_branch_name when deciding how to construct PR's in
        parallel mode.

        This may be empty for very old pull requests that were processed before
        this field was added.
        """,
    )
    flexreview_comment_id: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
        doc="""
        The GraphQL ID of the GitHub comment that we posted containing the
        suggested reviewers, and their related score.
        """,
    )
    gh_sticky_comment_id: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
        doc="""
        The GraphQL ID of the GitHub comment that we update (in order to show
        the user the current state of the PR). Since this is an ID in GitHub's
        system, there's no guarantee that the comment still exists even if it's
        not-null here.
        """,
    )

    gh_node_id: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
        doc="""
        The GraphQL ID of the GitHub pull request. This is used to fetch the
        pull request from GitHub's API in a batch.
        """,
    )
    stack_parent_id: Mapped[int | None] = sa.Column(
        sa.Integer,
        sa.ForeignKey("pull_request.id", name="pull_request_stack_parent_id_fkey"),
        nullable=True,
        doc=(
            "The id of the pull request that this PR depends on in a stack. "
            "Stacks are essentially directed trees. "
            "The root of the tree will be the PR that will be merged into the "
            "repository's base branch (it will have a NULL stack_parent_id)."
        ),
    )
    num_modified_lines: Mapped[int | None] = sa.Column(
        sa.Integer,
        nullable=True,
        doc="""Number of modified lines (additions + deletions).

        This can be None as this field was added later.
        """,
    )
    verified: Mapped[bool | None] = sa.Column(
        sa.Boolean,
        nullable=True,
        doc="""
        Pull request has been verified and is good to be deployed.

        True: Good to deploy.
        False: Bad change. Should not be deployed.
        None: Change has not been verified yet.
        """,
    )
    require_verification: Mapped[bool] = sa.Column(
        sa.Boolean,
        nullable=False,
        server_default=sa.false(),
        doc="""
        Verification is required for this pull request. The requirement will be an OR with the setting
        at release project level
        """,
    )
    signal_action_on_open_executed_at: Mapped[datetime.datetime | None] = sa.Column(
        sa.TIMESTAMP(timezone=True),
        nullable=True,
        doc="""
        The timestamp when the signal action on open was executed.
        """,
    )

    category: Mapped[bug_heuristics.PRType] = sa.Column(
        dbutil.TextEnum(bug_heuristics.PRType),
        nullable=False,
        server_default=bug_heuristics.PRType.UNCLASSIFIED.value,
        doc="Whether the PR is a bugfix, feature, or uncertain.",
    )

    account: Mapped[Account] = relationship(
        "Account",
        uselist=False,
    )
    activities: Mapped[list[Activity]] = relationship(
        "Activity",
        uselist=True,
        back_populates="pull_request",
    )
    creator: Mapped[GithubUser] = relationship(
        "GithubUser",
        uselist=False,
    )
    commits: Mapped[list[GitCommit]] = relationship(
        "GitCommit",
        secondary=BranchCommitMapping.__tablename__,
        back_populates="pull_requests",
    )
    repo: Mapped[GithubRepo] = relationship(
        "GithubRepo",
        uselist=False,
    )

    blocking_prs: Mapped[list[PullRequest]] = relationship(
        "PullRequest",
        uselist=True,
        secondary=PrDependency.__tablename__,
        primaryjoin="PullRequest.id == PrDependency.pull_request_id",
        secondaryjoin="PullRequest.id == PrDependency.depends_on_pr_id",
        doc="""
        All the PRs queued before this PR that will need to be cleared before this can
        be merged.
        """,
    )
    change_sets: Mapped[list[ChangeSet]] = relationship(
        "ChangeSet",
        secondary=ChangeSetMapping.__tablename__,
        uselist=True,
        back_populates="pr_list",
    )

    stack_parent: PullRequest | None
    stack_dependents: Mapped[list[PullRequest]] = relationship(
        "PullRequest",
        backref=sa.orm.backref("stack_parent", remote_side=[id]),
        doc=(
            "The pull requests that depend on this PR in a stack. "
            "These PRs have this PR's branch as their merge target (though "
            "they won't actually be merged into this PR -- they'll be merged "
            "into the main branch in dependency order)."
        ),
        uselist=True,
    )
    signals: Mapped[list[PullRequestFileSignal]] = relationship(
        "PullRequestFileSignal",
        back_populates="pull_request",
        uselist=True,
    )
    user_subscriptions: Mapped[list[PullRequestUserSubscription]] = relationship(
        "PullRequestUserSubscription",
        back_populates="pull_request",
        uselist=True,
    )

    files: Mapped[list[PullRequestModifiedFile]] = relationship(
        "PullRequestModifiedFile",
        uselist=True,
        back_populates="pull_request",
        doc=(
            "Files that were modified in this pull request, currently only gets updated on merge. "
            "On unmerged PRs this will be empty."
        ),
    )

    channel_notifications: Mapped[list[PullRequestChannelNotification]] = relationship(
        "PullRequestChannelNotification",
        back_populates="pull_request",
        uselist=True,
    )

    # Needs to be after the definition of columns to reference columns here.
    __table_args__ = (
        UniqueConstraint("repo_id", "number", name="pull_request_repo_id_number"),
        sa.Index("pull_request_account_id_branch_name", "account_id", "branch_name"),
        sa.Index("pull_request_repo_id_branch_name", "repo_id", "branch_name"),
        sa.Index("pull_request_repo_id_status", "repo_id", "status"),
        sa.Index(
            "pull_request_stack_parent_id",
            "stack_parent_id",
            postgresql_where=(stack_parent_id.isnot(None)),
        ),
        sa.Index("pull_request_repo_id_creation_time", "repo_id", "creation_time"),
    )

    def __repr__(self) -> str:
        return "<PullRequest %s #%d, id=%d, status=%s/%s>" % (
            self.repo.name,
            self.number,
            self.id,
            self.status,
            StatusCode(self.status_code).name,
        )

    def as_json(self) -> dict:
        return dict(
            number=self.number,
            title=self.title,
            author=self.creator.username,
            head_branch=self.branch_name,
            base_branch=self.base_branch_name,
            repository=self.repo.as_json(),
            status=self.json_status,
            queued=self.status in ("queued", "tagged"),
            skip_line=self.skip_line,
            queued_at=self.queued_at.isoformat() if self.queued_at else None,
            affected_targets=[t.name for t in self.affected_targets],
            merge_commit_sha=self.merge_commit_sha,
        )

    @property
    def json_status(self) -> str:
        # abstract tagged status
        return "queued" if self.status == "tagged" else self.status

    @property
    def merge_operations(self) -> MergeOperation | None:
        return (
            MergeOperation.query.filter_by(pull_request_id=self.id)
            .order_by(MergeOperation.id.desc())
            .first()
        )

    @property
    def emergency_merge(self) -> bool:
        mo = self.merge_operations
        return mo.emergency_merge if mo else False

    def set_emergency_merge(self, val: bool) -> None:
        mo = self.merge_operations
        if not mo:
            mo = MergeOperation(pull_request_id=self.id)
            db.session.add(mo)
        mo.emergency_merge = val

    @property
    def blocked_reason(self) -> str:
        if self.status != "blocked":
            return ""
        return StatusCode(self.status_code).name

    @property
    def status_name(self) -> str:
        return StatusCode(self.status_code).name

    @property
    def status_description(self) -> str:
        return StatusCode(self.status_code).message()

    def set_status(
        self,
        status: PullRequestStatus,
        status_code: StatusCode,
        status_reason: str | None = None,
    ) -> None:
        with self.bound_contextvars():
            logger.info(
                "Setting PR status",
                status=status,
                status_code=status_code.name,
                status_reason=status_reason,
                prev_status=self.status,
                prev_status_code=self.status_code.name,
            )
            self.status = status
            self.status_code = status_code
            if self.status in ("pending", "blocked"):
                self.status_reason = status_reason
            else:
                self.status_reason = None

    @property
    def status_reason_str(self) -> str:
        return self.status_reason or self.status_code.message()

    @property
    def latest_bot_pr(self) -> BotPr | None:
        """
        If a PR earlier in the queue fails and a new bot PR is created),
        we enforce that the latest BotPR is always the only active one.

        Ensures the latest BotPR is valid: it was created after the last time the PR was queued.
        """
        bot_pr: BotPr | None = (
            BotPr.query.filter(BotPr.batch_pr_list.any(PullRequest.id == self.id))
            .order_by(BotPr.id.desc())
            .first()
        )
        if not bot_pr or self.queued_at > bot_pr.created:
            return None

        return bot_pr

    @property
    def url(self) -> str:
        return self.repo.html_url + "/pull/" + str(self.number)

    @property
    def app_url(self) -> str:
        return f"{app.config['BASE_URL']}/repo/{self.repo.name}/pull/{self.number}"

    @property
    def elapsed_time(self) -> datetime.timedelta | None:
        """
        Represents the time the PR has been in queue before it's merged.
        """
        if not self.queued_at:
            return None
        if (
            self.status_code
            not in (
                StatusCode.MERGED_BY_MQ,
                StatusCode.MERGED_MANUALLY,
                StatusCode.PR_CLOSED_MANUALLY,
            )
            or not self.merged_at
        ):
            return time_util.now() - self.queued_at
        return self.merged_at - self.queued_at

    @property
    def markup_text(self) -> str:
        return f"[(# {self.number})]({self.number})"

    @functools.cached_property
    def affected_targets(self) -> list[AffectedTarget]:
        # NOTE: Initially, we used relationship() to query these, but PostgreSQL kept
        # using seq scan, and causing the CPU spikes. The latest effort is to split the
        # query hoping that each query uses index scan.
        affected_target_ids = list(
            db.session.scalars(
                sa.select(target_pr_mapping.c.affected_target_id)
                .where(target_pr_mapping.c.pull_request_id == self.id)
                .distinct(),
            ).all(),
        )
        if not affected_target_ids:
            return []
        return db.session.scalars(
            sa.select(AffectedTarget).where(
                AffectedTarget.id == sa.func.any(affected_target_ids),
            ),
        ).all()

    def is_bisected(self) -> bool:
        return bool(self.merge_operations and self.merge_operations.bisected_batch_id)

    def bind_contextvars(self) -> None:
        structlog.contextvars.bind_contextvars(**self.__contextvars())

    @contextlib.contextmanager
    def bound_contextvars(self) -> Generator[None]:
        with structlog.contextvars.bound_contextvars(**self.__contextvars()):
            yield

    def __contextvars(self) -> dict[str, object]:
        return {
            "pr_id": self.id,
            "pr_number": self.number,
        }


class PatchStatus(enum.Enum):
    """
    This matches the GitHub GraphQL PatchStatus enum.

    The comments below are derived from both the GitHUb GraphQL documentation
    and the Git man page for `git-diff`.
    """

    #: Git status letter "A": addition of a file
    ADDED = "ADDED"
    #: Git status letter "T": change in the type of the file
    #: (regular file, symbolic link, or submodule)
    CHANGED = "CHANGED"
    #: Git status letter "C": copy of a file into a new one
    COPIED = "COPIED"
    #: Git status letter "D": deletion of a file
    DELETED = "DELETED"
    #: Git status letter "M": modification of the contents or mode of a file
    MODIFIED = "MODIFIED"
    #: Git status letter "R": renaming of a file
    RENAMED = "RENAMED"


# We use DBModel instead of BaseModel here because this will be a massive table,
# and we don't need the extra baggage of the id, deleted, created, and modified
# columns.
class PullRequestModifiedFile(DBModel):
    __tablename__ = "pull_request_modified_files"
    __table_args__ = (sa.Index("pr_modified_files_file_path", "file_path"),)

    pull_request_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("pull_request.id", ondelete="CASCADE"),
        nullable=False,
        primary_key=True,
        doc="""
        The pull request where this file was modified.
        """,
    )
    file_path: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
        primary_key=True,
        doc="""
        The path of the modified file.
        """,
    )
    change_type: Mapped[PatchStatus] = sa.Column(
        dbutil.TextEnum(PatchStatus),
        nullable=False,
        primary_key=True,
        doc="""
        The type of change made to the file.
        """,
    )
    additions: Mapped[int] = sa.Column(
        sa.Integer,
        nullable=False,
        doc="""
        The number of lines added to the file.
        """,
    )
    deletions: Mapped[int] = sa.Column(
        sa.Integer,
        nullable=False,
        doc="""
        The number of lines deleted from the file.
        """,
    )
    owner_github_team_id: Mapped[int | None] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_team.id", ondelete="SET NULL"),
        nullable=True,
        doc="""The direct owner team of the file.""",
    )
    github_codeowners_approval_required: Mapped[bool] = sa.Column(
        sa.Boolean,
        server_default=sa.false(),
        nullable=False,
        doc="""True if the file requires CODEOWNERS approval by GitHub.""",
    )

    pull_request: Mapped[PullRequest] = relationship(
        "PullRequest",
        back_populates="files",
        uselist=False,
    )


pr_team_slack_channel_notification_mapping = sa.Table(
    "pr_team_slack_channel_notification_mapping",
    BaseModel.metadata,
    sa.Column(
        "pull_request_team_metadata_id",
        sa.Integer,
        sa.ForeignKey("pull_request_team_metadata.id"),
        primary_key=True,
        nullable=False,
    ),
    sa.Column(
        "pull_request_channel_notification_id",
        sa.Integer,
        sa.ForeignKey("pull_request_channel_notification.id"),
        primary_key=True,
        nullable=False,
    ),
)


class PullRequestTeamMetadata(
    IDModel, deprecated_models.DeprecatedPullRequestTeamMetadata
):
    """Metadata that belongs to a pull request, indexed by a GitHub team."""

    __tablename__ = "pull_request_team_metadata"
    __table_args__ = (
        sa.Index(
            "pull_request_team_metadata_pull_request_id_github_team_id",
            "pull_request_id",
            "github_team_id",
            unique=True,
        ),
        sa.Index(
            "pull_request_team_metadata_github_team_id",
            "github_team_id",
        ),
    )

    pull_request_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("pull_request.id"),
        nullable=False,
    )
    github_team_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_team.id", ondelete="CASCADE"),
        nullable=False,
    )
    is_direct_owner: Mapped[bool] = sa.Column(
        sa.Boolean,
        server_default=sa.false(),
        nullable=False,
        doc="""
        True if this team directly owns some of the modified files in the PR.
        """,
    )
    is_common_owner: Mapped[bool] = sa.Column(
        sa.Boolean,
        server_default=sa.false(),
        nullable=False,
        doc="""
        True if this team directly or indirectly owns all of the modified files in the
        PR.
        """,
    )
    is_team_internal_review: Mapped[bool] = sa.Column(
        sa.Boolean,
        server_default=sa.false(),
        nullable=False,
        doc="""
        True if the author is in this team.
        """,
    )
    first_reviewed_at: Mapped[datetime.datetime | None] = sa.Column(
        sa.TIMESTAMP(timezone=True),
        nullable=True,
        doc="""When a reviewer replied for the first time.""",
    )
    approved_at: Mapped[datetime.datetime | None] = sa.Column(
        sa.TIMESTAMP(timezone=True),
        nullable=True,
        doc="""When a reviewer approved.""",
    )
    time_to_first_review: Mapped[datetime.timedelta | None] = sa.Column(
        sa.Interval,
        nullable=True,
        doc="Time taken to get the first reply.",
    )
    first_reviewer_assigned_at: Mapped[datetime.datetime | None] = sa.Column(
        sa.TIMESTAMP(timezone=True),
        nullable=True,
        doc="""
            First timestamp that a reviewer is assigned from this team. This can be None
            if Aviator hasn't assigned a reviewer yet.
        """,
    )
    last_reviewer_assigned_at: Mapped[datetime.datetime | None] = sa.Column(
        sa.TIMESTAMP(timezone=True),
        nullable=True,
        doc="""
            Last timestamp that a reviewer is assigned from this team. This can be None
            if Aviator hasn't assigned a reviewer yet (e.g. it's retrying a PagerDuty
            API call).
        """,
    )
    assigned_github_user_id: Mapped[int | None] = sa.Column(
        sa.Integer(),
        nullable=True,
        default=None,
        doc="""
            If set, this GithubUser is assigned to review this pull request on behalf of
            the team.
        """,
    )

    last_new_reviewer_notification_sent_at: Mapped[datetime.datetime | None] = (
        sa.Column(
            sa.TIMESTAMP(timezone=True),
            nullable=True,
            default=None,
            doc="The timestamp when a new reviewer was last notified.",
        )
    )
    last_team_automation_processed_at: Mapped[datetime.datetime | None] = sa.Column(
        sa.TIMESTAMP(timezone=True),
        nullable=True,
    )

    notify_slack_channel: Mapped[bool] = sa.Column(
        sa.Boolean,
        nullable=False,
        server_default=sa.false(),
        doc="If true, notify the team default Slack channel",
    )

    channel_notifications: Mapped[list[PullRequestChannelNotification]] = relationship(
        "PullRequestChannelNotification",
        secondary=pr_team_slack_channel_notification_mapping,
        back_populates="pull_request_team_metas",
        uselist=True,
    )

    team: Mapped[GithubTeam] = relationship("GithubTeam", uselist=False)


class AttentionReason(enum.Enum):
    # A reviewer approved the PR.
    APPROVED = "approved"
    # All reviewers do not have an attention.
    FALLBACK = "fallback"
    # Specify only manual for now. As more transitions are implemented
    # this enum will be expanded to support them.
    MANUAL = "manual"
    # Pull request could not be merged
    PULL_REQUEST_BLOCKED = "pull_request_blocked"
    # A comment on a review on this PullRequest lead this attention state.
    REVIEW_COMMENT = "review_comment"
    # A review was dismissed.
    REVIEW_DISMISSED = "review_dismissed"
    # REVIEW_REQUESTED reason attention is for users that have been added as
    # a reviewer.
    REVIEW_REQUESTED = "review_requested"
    # A test failure was reported.
    TEST_FAILURE = "test_failure"

    ##### DEPRECATED REASONS #####
    REVIEW_SUBMITTED = "review_submitted"
    PULL_REQUEST_CLOSED = "pull_request_closed"


class PullRequestUserSubscription(BaseModel):
    __tablename__ = "pull_request_user_subscription"
    # There should only be a single {PullRequest} x {GithubUser} entry at most.
    __table_args__ = (
        UniqueConstraint(
            "pull_request_id",
            "github_user_id",
            name="pull_request_user_subscription_pull_request_id_github_user_id",
        ),
        sa.Index(
            # Can't use the full column names here because it exceeds the
            # maximum allowed identifier length of 63 characters.
            "pull_request_user_subscription_user_id_attention_modified",
            "github_user_id",
            "has_attention",
            "modified",
        ),
    )
    # Index by both GithubUser and PullRequest, so that a user or a pull's
    # attention set is easily accessible.
    pull_request_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("pull_request.id"),
        nullable=False,
        index=True,
    )
    github_user_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_user.id"),
        nullable=False,
        index=True,
    )
    has_attention: Mapped[bool] = sa.Column(
        sa.Boolean,
        nullable=False,
        doc=(
            "This boolean represents the GitHubUser's attention state for the "
            "given PullRequest. True suggests that the user has been asked to "
            "take action next. False suggests that user has recently fulfilled "
            "a requested action. "
        ),
    )
    is_reviewer: Mapped[bool] = sa.Column(
        sa.Boolean,
        nullable=False,
        server_default=sa.false(),
        doc=(
            "This boolean represents if the GitHubUser is assigned as a "
            "reviewer for the given PullRequest."
        ),
    )
    attention_reason: Mapped[AttentionReason] = sa.Column(
        dbutil.TextEnum(AttentionReason),
        nullable=False,
        doc=("The most recent action that has set the current attention_status."),
    )
    approved_at: Mapped[datetime.datetime | None] = sa.Column(
        sa.TIMESTAMP(timezone=True),
        nullable=True,
        doc="Timestamp that the user approved the PR.",
    )
    approved_head_commit_sha: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
        doc="If a user approved a PR, the commit SHA of the approved code.",
    )
    attention_bit_flipped_at: Mapped[datetime.datetime] = sa.Column(
        sa.TIMESTAMP(timezone=True),
        # Since this is a column added later, we need to set a reasonable server default
        # for existing data. CURRENT_TIMESTAMP is likely the best as this is used for
        # showing SLO limit and team action. Do not worry much about this for existing
        # data as this is one time thing when running a data migration.
        server_default=sa.text("CURRENT_TIMESTAMP"),
        nullable=False,
        doc="Last time the has_attention bit is flipped at.",
    )
    last_interaction_at: Mapped[datetime.datetime] = sa.Column(
        sa.TIMESTAMP(timezone=True),
        server_default=sa.text("CURRENT_TIMESTAMP"),
        nullable=False,
        doc="Last time the user interacts with this PR.",
    )
    notify_slack_dm: Mapped[bool] = sa.Column(
        sa.Boolean,
        nullable=False,
        server_default=sa.false(),
        doc="If true, notify the user via Slack DM.",
    )
    notified_slack_message_ts: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
        doc="""
        The ts of the Slack message that has been notified.

        See the Slack API document for chat.postMessage.
        """,
    )

    pull_request: Mapped[PullRequest] = relationship(
        "PullRequest", back_populates="user_subscriptions", uselist=False
    )
    github_user: Mapped[GithubUser] = relationship("GithubUser", uselist=False)


class PullRequestChannelNotification(IDModel):
    __tablename__ = "pull_request_channel_notification"
    pull_request_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("pull_request.id"),
        nullable=False,
        doc="The pull request that we've notified for.",
    )

    notified_slack_channel: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
        doc="The Slack channel that has been notified.",
    )

    external_slack_channel_id: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
        doc="""
        `notified_slack_channel` can be either a name or a channel ID.
        We capture the channel ID from our postMessage response,
        so that we can add reactions to the message.
        To add reactions we need the channel ID, ex: `C1234567890`.
        """,
    )

    notified_slack_message_ts: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
        doc="""
        The ts of the Slack message that has been notified.

        See the Slack API document for chat.postMessage.
        """,
    )

    pull_request: Mapped[PullRequest] = relationship(
        "PullRequest",
        back_populates="channel_notifications",
        uselist=False,
    )

    pull_request_team_metas: Mapped[list[PullRequestTeamMetadata]] = relationship(
        "PullRequestTeamMetadata",
        secondary=pr_team_slack_channel_notification_mapping,
        back_populates="channel_notifications",
        uselist=True,
    )


class FlexReviewSloPullRequestData(BaseModel):
    """SLO tracking data per-PR per team.

    An entry is created per-(PR, team) uniquely.
    """

    __tablename__ = "flexreview_slo_pull_request_data"
    __table_args__ = (
        sa.Index(
            "flexreview_slo_pull_request_data_prid_reviewer_team_id",
            "pull_request_id",
            "reviewer_team_id",
            unique=True,
        ),
    )

    pull_request_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("pull_request.id"),
        nullable=False,
    )
    reviewer_team_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_team.id", ondelete="CASCADE"),
        nullable=False,
        doc="""
        GitHub team that is required for reviewing a PR. This is an owner per
        CODEOWNERS.
        """,
    )
    is_creator_team_member: Mapped[bool] = sa.Column(
        sa.Boolean,
        nullable=False,
        server_default=sa.false(),
        doc="""True if the PR creator is a member of the team.""",
    )
    review_started_at: Mapped[datetime.datetime] = sa.Column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        doc="""
        When the review started at. The review starts when (1) the team is assigned
        as a reviewer or (2) a team member is assigned as a reviewer.
        """,
    )
    first_replied_at: Mapped[datetime.datetime] = sa.Column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        doc="""When the reviewer replied at for the first time.""",
    )
    time_to_first_review: Mapped[datetime.timedelta] = sa.Column(
        sa.Interval,
        nullable=False,
        server_default="0s",
        doc="Time taken to get the first review.",
    )


class PullRequestFileSignal(IDModel):
    __tablename__ = "pull_request_file_signal"

    pull_request_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("pull_request.id"),
        nullable=False,
    )
    signal_rule_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("signal_rule.id"),
        nullable=False,
    )
    file_path: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )
    regexp_match_count: Mapped[int] = sa.Column(
        sa.Integer,
        nullable=False,
        server_default="0",
        doc="""
        Number of regexp pattern occurrences in the file. If the regexp is not
        specified, this is set to 0.
        """,
    )
    regexp_match_count_delta: Mapped[int] = sa.Column(
        sa.Integer,
        nullable=False,
        server_default="0",
        doc="""
        Difference of the regexp_match_count made by the PR. For example, if the
        file has 5 occurrences before the PR, and with PR it becomes 6, this is
        set to 1.
        """,
    )

    pull_request: Mapped[PullRequest] = relationship(
        "PullRequest",
        back_populates="signals",
        uselist=False,
    )


required_repo_user = sa.Table(
    "required_repo_user",
    BaseModel.metadata,
    sa.Column(
        "github_repo_id",
        sa.Integer,
        sa.ForeignKey("github_repo.id"),
        nullable=False,
        index=True,
    ),
    sa.Column(
        "github_user_id",
        sa.Integer,
        sa.ForeignKey("github_user.id"),
        nullable=False,
        index=True,
    ),
)

bot_pr_mapping = sa.Table(
    "bot_pr_mapping",
    BaseModel.metadata,
    sa.Column(
        "bot_pr_id",
        sa.Integer,
        sa.ForeignKey("bot_pr.id"),
        nullable=False,
        index=True,
    ),
    sa.Column(
        "pull_request_id",
        sa.Integer,
        sa.ForeignKey("pull_request.id"),
        nullable=False,
        index=True,
    ),
)

batch_bot_pr_mapping = sa.Table(
    "batch_bot_pr_mapping",
    BaseModel.metadata,
    sa.Column(
        "bot_pr_id",
        sa.Integer,
        sa.ForeignKey("bot_pr.id"),
        nullable=False,
        index=True,
    ),
    sa.Column(
        "pull_request_id",
        sa.Integer,
        sa.ForeignKey("pull_request.id"),
        nullable=False,
        index=True,
    ),
)


class AffectedTarget(BaseModel):
    __tablename__ = "affected_target"
    repo_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_repo.id"),
        nullable=False,
    )
    name: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )

    pr_list: Mapped[list[PullRequest]] = relationship(
        "PullRequest",
        secondary=target_pr_mapping,
        uselist=True,
    )

    __table_args__ = (
        UniqueConstraint("repo_id", "name", name="affected_target_repo_id_idx"),
    )


class CommitFileCoverage(IDModel):
    """Coverage data for a given commit."""

    __tablename__ = "commit_file_coverage"

    repo_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_repo.id", ondelete="CASCADE"),
        nullable=False,
    )

    commit_sha: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )

    lines_found: Mapped[int] = sa.Column(
        sa.Float,
        nullable=False,
    )

    lines_hit: Mapped[int] = sa.Column(
        sa.Float,
        nullable=False,
    )

    file_path: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )

    owner_github_team_id: Mapped[int | None] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_team.id", ondelete="CASCADE"),
        nullable=True,
    )

    created: Mapped[datetime.datetime] = sa.Column(
        sa.TIMESTAMP(timezone=True), default=time_util.now
    )


class CommitFileMetric(IDModel):
    """Metric data for a given commit."""

    __tablename__ = "commit_file_metric"

    repo_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_repo.id", ondelete="CASCADE"),
        nullable=False,
    )

    commit_sha: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )

    count: Mapped[int] = sa.Column(
        sa.Integer,
        nullable=False,
    )

    file_path: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )

    type_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("metric_type.id", ondelete="CASCADE"),
        nullable=False,
    )

    owner_github_team_id: Mapped[int | None] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_team.id", ondelete="CASCADE"),
        nullable=True,
    )

    created: Mapped[datetime.datetime] = sa.Column(
        sa.TIMESTAMP(timezone=True), default=time_util.now
    )


class TechDebtWeeklyPerDirData(MutableModel):
    __tablename__ = "tech_debt_weekly_per_dir_data"
    __table_args__ = (
        sa.Index(
            "tech_debt_weekly_per_dir_idx_repo_dir_year_week",
            "repo_id",
            "dir_path",
            "year_week",
            unique=True,
        ),
    )

    repo_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_repo.id", ondelete="CASCADE"),
        nullable=False,
    )

    dir_path: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )

    year_week: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )

    commit_sha: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
        server_default="",
    )

    completed: Mapped[bool] = sa.Column(
        sa.Boolean,
        server_default=sa.false(),
    )

    data: Mapped[TechDebtPerDirData] = sa.Column(
        dbutil.PydanticType(TechDebtPerDirData),
        nullable=False,
    )

    repo: Mapped[GithubRepo] = relationship("GithubRepo", uselist=False)


class MetricType(BaseModel):
    """Data on metric configuration."""

    __tablename__ = "metric_type"
    __table_args__ = (
        sa.Index(
            "metric_type_tool_name_type_name",
            "account_id",
            "repo_id",
            "tool_name",
            "type_name",
            unique=True,
        ),
    )

    repo_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_repo.id", ondelete="CASCADE"),
        nullable=False,
    )

    account_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )

    category: Mapped[TechDebtCategory] = sa.Column(
        dbutil.TextEnum(TechDebtCategory),
        nullable=False,
        server_default=TechDebtCategory.UNCATEGORIZED.value,
    )

    name: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )

    tool_name: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )

    type_name: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )

    severity: Mapped[MetricSeverity] = sa.Column(
        dbutil.TextEnum(MetricSeverity),
        nullable=False,
        server_default=MetricSeverity.IGNORE.value,
    )


class TechDebtGoalState(enum.Enum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    PAST_DUE = "past_due"


class TechDebtGoal(MutableModel):
    """Organizational tech debt goals."""

    __tablename__ = "tech_debt_goal"

    account_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )

    repo_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_repo.id", ondelete="CASCADE"),
        nullable=False,
    )

    name: Mapped[str] | None = sa.Column(
        sa.Text,
        nullable=True,
    )

    metric_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("metric_type.id"),
        nullable=False,
    )

    creator_user_id: Mapped[int] | None = sa.Column(
        sa.Integer,
        sa.ForeignKey("user_info.id"),
        nullable=True,
    )

    state: Mapped[TechDebtGoalState] = sa.Column(
        dbutil.TextEnum(TechDebtGoalState),
        nullable=False,
        server_default=TechDebtGoalState.IN_PROGRESS.value,
    )

    file_path_patterns: Mapped[list[str]] = sa.Column(
        ARRAY(sa.Text, dimensions=1),
        nullable=False,
        server_default="{}",
        doc="""
        File path patterns to match for this goal.
        For example:
        * ['src/core', 'src/gitlab', 'src/auth']
        * ['frontend/src/app/gitlab', 'frontend/src/app/github']
        * [] - empty will match all files in the repository
        """,
    )

    target_completion_date: Mapped[datetime.date | None] = sa.Column(
        sa.Date,
        nullable=True,
    )

    metric: Mapped[MetricType] = relationship("MetricType", uselist=False)

    tasks: Mapped[list[TechDebtTask]] = relationship(
        "TechDebtTask",
        back_populates="goal",
        uselist=True,
    )

    creator: Mapped[User] = relationship("User", uselist=False)


class TechDebtTaskCount(MutableModel):
    __tablename__ = "tech_debt_task_count"

    task_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("tech_debt_task.id", ondelete="CASCADE"),
        nullable=False,
    )

    task: Mapped[TechDebtTask] = relationship(
        "TechDebtTask",
        uselist=False,
    )

    count: Mapped[int] = sa.Column(
        sa.Integer,
        nullable=False,
        server_default="0",
    )

    count_52w: Mapped[list[int]] = sa.Column(
        ARRAY(sa.Integer, dimensions=1), nullable=False, server_default="{}"
    )

    year_week: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )


class TechDebtTask(MutableModel):
    """Track individual tasks for a given tech debt goal"""

    __tablename__ = "tech_debt_task"

    goal_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("tech_debt_goal.id", ondelete="CASCADE"),
        nullable=False,
    )

    github_team_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_team.id", ondelete="CASCADE"),
        nullable=False,
    )

    github_team: Mapped[GithubTeam] = relationship(
        "GithubTeam",
        back_populates="tasks",
        uselist=False,
    )

    goal: Mapped[TechDebtGoal] = relationship(
        "TechDebtGoal",
        back_populates="tasks",
        uselist=False,
    )


class TechDebtWeeklyPerFileData(MutableModel):
    __tablename__ = "tech_debt_weekly_per_file_data"
    __table_args__ = (
        sa.Index(
            "tech_debt_weekly_per_file_idx_repo_file_year_week",
            "repo_id",
            "file_path",
            "year_week",
            unique=True,
        ),
    )

    repo_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_repo.id", ondelete="CASCADE"),
        nullable=False,
    )

    file_path: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )

    year_week: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )

    commit_sha: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
        server_default="",
    )

    completed: Mapped[bool] = sa.Column(
        sa.Boolean,
        server_default=sa.false(),
    )

    tech_debt_score: Mapped[float] = sa.Column(
        sa.Float,
        nullable=False,
    )

    importance_score: Mapped[float] = sa.Column(
        sa.Float, nullable=False, server_default="0.0"
    )

    data: Mapped[TechDebtPerFileData] = sa.Column(
        dbutil.PydanticType(TechDebtPerFileData),
        nullable=False,
    )

    repo: Mapped[GithubRepo] = relationship("GithubRepo", uselist=False)


class TechDebtScannerJobStatus(enum.Enum):
    PENDING = "pending"
    RECEIVED = "received"
    COMPLETED = "completed"
    FAILED = "failed"


class TechDebtScannerJob(MutableModel):
    __tablename__ = "tech_debt_scanner_job"
    user_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("user_info.id", ondelete="CASCADE"),
        nullable=False,
    )
    message: Mapped[str] = sa.Column(sa.Text, nullable=False)
    status: Mapped[TechDebtScannerJobStatus] = sa.Column(
        dbutil.TextEnum(TechDebtScannerJobStatus),
        nullable=False,
        default=TechDebtScannerJobStatus.PENDING,
    )
    raw_result: Mapped[str] = sa.Column(sa.Text, nullable=True)
    config_gen_list: Mapped[list[TechDebtScannerJobConfigGen]] = relationship(
        "TechDebtScannerJobConfigGen",
        uselist=True,
    )


class TechDebtScannerJobConfigGen(IDModel):
    __tablename__ = "tech_debt_scanner_job_config_gen"
    job_id = sa.Column(
        sa.Integer,
        sa.ForeignKey("tech_debt_scanner_job.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    parsed_text: Mapped[str] = sa.Column(sa.Text, nullable=False)
    description: Mapped[str] = sa.Column(sa.Text, nullable=False)

    @property
    def pattern_yaml(self) -> str:
        try:
            return yaml.safe_dump(yaml.safe_load(self.parsed_text))
        except yaml.YAMLError:
            return ""


class TechDebtAstGrepPattern(MutableModel):
    __tablename__ = "tech_debt_ast_grep_patterns"
    repo_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_repo.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    description: Mapped[str] = sa.Column(sa.Text, nullable=False)
    pattern: Mapped[str] = sa.Column(sa.Text, nullable=False)
    creator_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("user_info.id", ondelete="CASCADE"),
        nullable=False,
    )


class ReadyHookState(str, enum.Enum):
    # The ready hook has not yet been executed.
    PENDING = "pending"
    # The ready hook executed successfully.
    # This also includes the case where there is no ready hook configured.
    SUCCEEDED = "succeeded"
    # An error occurred while executing the ready hook.
    FAILED = "failed"


class ReadySource(str, enum.Enum):
    """
    Represents how the PR got into a ready state.
    """

    UNKNOWN = "unknown"
    LABEL = "label"
    CHROME_EXTENSION = "chrome_extension"
    GRAPHQL_API = "graphql_api"
    REST_API = "rest_api"
    PILOT = "pilot"
    SLASH_COMMAND = "slash_command"


class MergeOperation(BaseModel):
    """
    A MergeOperation represents a request/attempt to merge a pull request as
    part of MergeQueue.

    Historically, pull requests were considered queued whenever the `status`
    field was set to `"queued"`, but we're moving towards a conceptual model
    where a pull request is considered queued if and only if it has an
    associated MergeOperation.

    Longer term, the goal is to unify merging single pull requests with merging
    stacks and merging changesets all under the umbrella of a single
    MergeOperation.
    """

    MAX_TRANSIENT_ERROR_RETRY_COUNT = 3

    __tablename__ = "merge_operations"

    pull_request_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("pull_request.id"),
        nullable=False,
        index=True,
    )
    commit_message: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
    )
    commit_title: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
    )
    ready: Mapped[bool] = sa.Column(
        sa.Boolean,
        nullable=False,
        server_default=sa.true(),
    )
    emergency_merge: Mapped[bool] = sa.Column(
        sa.Boolean,
        nullable=False,
        server_default=sa.false(),
    )
    skip_line_reason: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
    )
    skip_validation: Mapped[bool] = sa.Column(
        sa.Boolean,
        nullable=False,
        server_default=sa.false(),
        doc=(
            "This is a special configuration originally designed for Doordash "
            "to support a special skip line workflow where we ignore the CI checks "
            "on original PR but create the draft PR and validate the checks on it."
            "Reference: "
            "https://www.notion.so/aviator-co/Doordash-instant-merge-skip-line-skip-testing-8f90577183484cea9fb2a9e520795b85"
        ),
    )
    transient_error_retry_count: Mapped[int] = sa.Column(
        sa.Integer,
        nullable=False,
        server_default="0",
        doc=(
            "A merge operation can fail for a transient error. When it happens, "
            "we want to retry merging a PR with some upper bound. This is a "
            "counter for this retry. Do not confuse with the requeue retry."
        ),
    )
    is_bisected: Mapped[bool | None] = sa.Column(
        sa.Boolean,
        nullable=True,
        doc="""
            Represents that the PR is requeued due to bisection. This
            property should be set when the previous batch has failed
            and before the new batch is created. As soon as the new batch
            is created, the property should be unset.
            """,
    )
    bisected_batch_id: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
        doc="""
            Represents the batch id (uuid) of the bisected batch to be created.
            This is used to identify what PRs will be together during a requeue
            after a bisection.
            """,
    )
    times_bisected: Mapped[int] = sa.Column(
        sa.Integer,
        nullable=False,
        server_default="0",
        doc="Represents the number of times a PR has been bisected.",
    )

    ready_hook_state: Mapped[ReadyHookState] = sa.Column(
        dbutil.TextEnum(ReadyHookState),
        nullable=False,
        # TODO: change this to ReadyHookState.PENDING once we're ready to launch
        #       the ready hook
        server_default=ReadyHookState.SUCCEEDED,
        doc="The current state of the ready hook.",
    )

    ready_source: Mapped[ReadySource | None] = sa.Column(
        dbutil.TextEnum(ReadySource),
        nullable=True,
        server_default=ReadySource.LABEL,
        doc="Represents how the PR was queued / marked ready",
    )
    ready_actor_github_user_id: Mapped[int | None] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_user.id"),
        nullable=True,
        doc="Represents a GH user who queued the PR",
    )
    ready_actor_user_id: Mapped[int | None] = sa.Column(
        sa.Integer,
        sa.ForeignKey("user_info.id"),
        nullable=True,
        doc="Represents an Aviator user who queued the PR",
    )

    @contextlib.contextmanager
    def bound_contextvars(self) -> Generator[None]:
        with structlog.contextvars.bound_contextvars(merge_operation_id=self.id):
            yield


class PullRequestQueuePrecondition(IDModel):
    __tablename__ = "pull_request_queue_precondition"

    created: Mapped[datetime.datetime] = sa.Column(
        sa.TIMESTAMP(timezone=True), default=time_util.now
    )

    pull_request_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("pull_request.id"),
        nullable=False,
        index=True,
    )
    blocking_pull_request_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("pull_request.id"),
        nullable=False,
        index=True,
        doc="""
        The blocking pull request that needs to be merged before this pull request
        can be queued. Note that, this is different from stacked PRs DAG.
        """,
    )

    blocking_pull_request: Mapped[PullRequest] = relationship(
        "PullRequest",
        uselist=False,
        foreign_keys=[blocking_pull_request_id],
    )


class BotPr(BaseModel):
    __tablename__ = "bot_pr"
    # Lie to mypy and tell it that number is non-nullable.
    # TODO: mark this as non-nullable in database
    number: Mapped[int] = sa.Column(
        sa.Integer,
        nullable=False,
    )
    # options: queued, closed
    status: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
        server_default="queued",
    )
    title: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )
    account_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )
    repo_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_repo.id", ondelete="CASCADE"),
        nullable=False,
    )

    status_code: Mapped[StatusCode] = sa.Column(
        dbutil.IntEnum(StatusCode),
        nullable=False,
        server_default=str(StatusCode.UNKNOWN.value),
    )
    last_processed: Mapped[datetime.datetime] = sa.Column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
        onupdate=time_util.now,
    )
    sequence_no: Mapped[int | None] = sa.Column(
        sa.Integer,
        nullable=True,
    )
    pull_request_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("pull_request.id"),
        nullable=False,
    )
    is_blocking: Mapped[bool] = sa.Column(
        sa.Boolean,
        nullable=False,
        server_default=sa.false(),
    )
    head_commit_sha: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
    )
    branch_name: Mapped[str] = sa.Column(
        sa.Text(),
        nullable=False,
    )
    target_branch_name: Mapped[str] = sa.Column(
        sa.Text(),
        nullable=True,
    )
    failing_test_count: Mapped[int] = sa.Column(
        sa.Integer,
        nullable=False,
        server_default="0",
        doc="Cached count of failing required tests",
    )
    passing_test_count: Mapped[int] = sa.Column(
        sa.Integer,
        nullable=False,
        server_default="0",
        doc="Cached count of passing required tests",
    )
    pending_test_count: Mapped[int] = sa.Column(
        sa.Integer,
        nullable=False,
        server_default="0",
        doc="Cached count of pending required tests",
    )
    is_bisected_batch: Mapped[bool] = sa.Column(
        sa.Boolean,
        nullable=True,
        doc="""
        Whether the current batch a bisected batch. If it is, then
        the batch may not merge the PR once it passes, rather it will
        only mark the PRs in the batch as clean, and requeue them.
        """,
    )

    # The list of PRs that have been tagged already - these PRs' commits are
    # included in the BotPR branch.
    #
    # For example, if there are 6 PRs and this batch includes [4 5 6]. This
    # pr_list gives [1 2 3 4 5 6].
    pr_list: Mapped[list[PullRequest]] = relationship(
        "PullRequest", secondary=bot_pr_mapping, uselist=True
    )
    # The list of PRs that are in the current batch. These are the PRs that get
    # merged if the BotPR CI passes.
    #
    # For example, if there are 6 PRs and this batch includes [4 5 6]. This
    # batch_pr_list gives [4 5 6].
    batch_pr_list: Mapped[list[PullRequest]] = relationship(
        "PullRequest",
        secondary=batch_bot_pr_mapping,
        uselist=True,
    )

    account: Mapped[Account] = relationship("Account", uselist=False)
    repo: Mapped[GithubRepo] = relationship("GithubRepo", uselist=False)
    pull_request: Mapped[PullRequest] = relationship(
        "PullRequest", uselist=False, backref="bot_pr"
    )

    __table_args__ = (
        UniqueConstraint("repo_id", "number", name="bot_pr_number_repo_id"),
        sa.Index(
            "botpr_repo_id_status",
            "repo_id",
            "status",
        ),
    )

    def __repr__(self) -> str:
        return "<BotPr %r>" % self.number

    def set_status(self, status: str, status_code: StatusCode) -> None:
        logger.info(
            "Setting BotPR status",
            bot_pr_number=self.number,
            status=status,
            status_code=status_code.name,
            prev_status=self.status,
            prev_status_code=self.status_code.name,
        )
        self.status = status
        self.status_code = status_code

    @property
    def blocked_reason(self) -> str:
        if self.status != "blocked":
            return ""
        return StatusCode(self.status_code).name

    @property
    def markup_text(self) -> str:
        return f"[(# {self.number})]({self.number})"

    @property
    def url(self) -> str:
        return self.repo.html_url + "/pull/" + str(self.number)

    @staticmethod
    def get_many_latest_bot_pr(
        pr_ids: list[int],
        *,
        account_id: int | None,
    ) -> dict[int, BotPr]:
        """
        Get the latest BotPR for each input pull request ID.

        Only pull requests matching the given account ID will be returned
        (unless `account_id` is `None`, in which case no account filtering will
        be applied).
        """
        # This overall uses a LATERAL JOIN query which is a fancy Postgres thing
        # that kind of acts like a "foreach loop". This works well for this kind of
        # query because we can phrase it as "for each PR ID, get the latest BotPR".
        # Ultimately, this query looks something like:
        # SELECT * FROM pull_request WHERE id IN (?, ?, ?)
        # LATERAL JOIN (
        #     SELECT * FROM bot_pr
        #     JOIN batch_bot_pr_mapping ON ...
        #     WHERE pr_id = pull_request.id AND ...
        #     ORDER BY id DESC LIMIT 1
        # )

        subq_pull_request = sa.orm.aliased(
            db.session.query(
                PullRequest.id.label("id"),
                PullRequest.queued_at.label("queued_at"),
            )
            .join(GithubRepo)
            .filter(
                PullRequest.id.in_(pr_ids),
                (
                    # If account_id filtering is disabled, just use "True" here
                    # since "AND TRUE" is a no-on in the WHERE clause.
                    True if account_id is None else GithubRepo.account_id == account_id
                ),
            )
            .subquery()
            .lateral(),
        )

        sub_latest_bot = sa.orm.aliased(
            BotPr,
            db.session.query(BotPr)
            .join(batch_bot_pr_mapping)
            .filter(
                batch_bot_pr_mapping.columns["pull_request_id"]
                == subq_pull_request.c.id,
                BotPr.created > subq_pull_request.c.queued_at,
            )
            .order_by(BotPr.id.desc())
            .limit(1)
            .subquery()
            .lateral(),
        )

        result: list[tuple[int, datetime.datetime, BotPr]] = (
            db.session.query(subq_pull_request, sub_latest_bot)
            # select_from(subq_pr_ids) here tells SQLAlchemy to use the PR query as
            # the left side of the join (so that we use a lateral join to select the
            # most recent BotPR for every PR ID).
            .select_from(subq_pull_request)
            .all()
        )

        bot_pr_map = {}
        for pr_id, _, bot_pr in result:
            bot_pr_map[pr_id] = bot_pr
        return bot_pr_map

    def bind_contextvars(self) -> None:
        structlog.contextvars.bind_contextvars(**self.__contextvars())

    @contextlib.contextmanager
    def bound_contextvars(self) -> Generator[None]:
        with structlog.contextvars.bound_contextvars(**self.__contextvars()):
            yield

    def __contextvars(self) -> dict[str, object]:
        return {
            "bot_pr_id": self.id,
            "bot_pr_number": self.number,
        }


class ChangeSetConfig(BaseModel):
    @staticmethod
    def for_account(account_id: int) -> ChangeSetConfig | None:
        return ChangeSetConfig.query.filter_by(account_id=account_id).first()

    __tablename__ = "change_set_config"
    account_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )
    enable_comments: Mapped[bool] = sa.Column(
        sa.Boolean,
        nullable=False,
        server_default=sa.false(),
    )
    require_global_ci: Mapped[bool] = sa.Column(
        sa.Boolean,
        nullable=False,
        server_default=sa.false(),
    )
    enable_auto_creation: Mapped[bool] = sa.Column(
        sa.Boolean,
        nullable=False,
        server_default=sa.false(),
    )


class ChangeSet(BaseModel):
    __tablename__ = "change_set"
    account_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )
    number: Mapped[int] = sa.Column(
        sa.Integer,
        nullable=False,
    )  # unique to each account
    status: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
        server_default="pending",
    )  # pending, queued, merged, failure
    owner_id: Mapped[int | None] = sa.Column(
        sa.Integer,
        sa.ForeignKey("user_info.id"),
        nullable=True,
    )

    pr_list: Mapped[list[PullRequest]] = relationship(
        "PullRequest",
        secondary=ChangeSetMapping.__tablename__,
        uselist=True,
        back_populates="change_sets",
    )
    owner: Mapped[User] = relationship(
        "User",
        uselist=False,
    )

    def bind_contextvars(self) -> None:
        structlog.contextvars.bind_contextvars(**self.__contextvars())

    @contextlib.contextmanager
    def bound_contextvars(self) -> Generator[None]:
        with structlog.contextvars.bound_contextvars(**self.__contextvars()):
            yield

    def __contextvars(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "changeset_id": self.id,
        }

    @property
    def status_runs(self) -> list[ChangeSetRun]:
        runs: list[ChangeSetRun] = (
            ChangeSetRun.query.filter_by(change_set_id=self.id)
            .order_by(ChangeSetRun.id.desc())
            .all()
        )
        return runs

    @property
    def latest_status_run(self) -> ChangeSetRun | None:
        run: ChangeSetRun | None = (
            ChangeSetRun.query.filter_by(change_set_id=self.id)
            .order_by(ChangeSetRun.id.desc())
            .first()
        )
        return run

    __table_args__ = (
        UniqueConstraint("account_id", "number", name="change_set_number"),
    )

    def get_webhook_params(self) -> dict[str, str]:
        """
        Returns a dictionary of parameters to be used in a webhook payload. We offer the default ones
        and then override with custom ones specific to the change set.
        """
        webhook_params = {}
        default_params: list[ChangeSetWebhookParam] = (
            ChangeSetWebhookParam.query.filter_by(
                account_id=self.account_id, change_set_id=None
            ).all()
        )
        for param in default_params:
            webhook_params[param.name] = param.value
        custom_params: list[ChangeSetWebhookParam] = (
            ChangeSetWebhookParam.query.filter_by(
                account_id=self.account_id, change_set_id=self.id
            ).all()
        )
        for param in custom_params:
            webhook_params[param.name] = param.value
        return webhook_params


class ChangeSetRun(BaseModel, deprecated_models.DeprecatedChangeSetRun):
    """
    Represents a request for running checks on a change set. This is typically triggered from
    the UI and it internally triggers a webhook. The user is then supposed to run a bunch of checks
    and send back a ChangeSetRunCheck per check with a unique name.
    """

    __tablename__ = "change_set_run"

    account_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )
    change_set_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("change_set.id"),
        nullable=False,
        index=True,
    )
    change_set: Mapped[ChangeSet] = relationship(
        "ChangeSet",
        uselist=False,
    )
    commits: Mapped[list[ChangeSetRunCommit]] = relationship(
        "ChangeSetRunCommit",
        uselist=True,
        back_populates="change_set_run",
    )
    checks: Mapped[list[ChangeSetRunCheck]] = relationship(
        "ChangeSetRunCheck",
        uselist=True,
        back_populates="change_set_run",
    )

    @property
    def status(self) -> str:
        """
        Possible values: error, failure, pending, success
        """
        if not self.checks:
            s: str | None = self.DEPRECATED_status
            return s or "pending"

        status = "success"
        for check in self.checks:
            if check.status in ["failure", "error"]:
                return check.status
            if check.status in ["pending", "queued"]:
                status = "pending"
        return status


class ChangeSetWebhookParam(BaseModel):
    __tablename__ = "change_set_webhook_param"
    account_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )
    change_set_id: Mapped[int | None] = sa.Column(
        sa.Integer,
        sa.ForeignKey("change_set.id"),
        nullable=True,
    )
    name: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )
    value: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )


class ChangeSetCommitCheck(DBModel):
    """
    Represents mapping of each commit to a specific check in a ChangeSet. This uniquely identifies a
    check on a PR at a given commit SHA. Since the checks for a change set are not tied to a specific
    PR but a Changeset, we store that as ChangeSetRunCheck.
    """

    __tablename__ = "change_set_commit_check"
    change_set_run_check_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("change_set_run_check.id"),
        primary_key=True,
        nullable=False,
    )
    change_set_run_commit_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("change_set_run_commit.id"),
        primary_key=True,
        nullable=False,
    )
    check_run_id: Mapped[int | None] = sa.Column(
        sa.BigInteger,
        nullable=True,
    )


class ChangeSetRunCheck(BaseModel):
    """
    Represents a unique check run for a change set that the user has responded with
    in response to a ChangeSetRun request. This is still global to a changeset and does not
    represent an actual status check_run on a specific PR. That is captured in ChangeSetCommitCheck.
    """

    __tablename__ = "change_set_run_check"
    change_set_run_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("change_set_run.id"),
        nullable=False,
    )
    status: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
        server_default="pending",
    )  # error, failure, pending, success
    name: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )
    target_url: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
    )
    message: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
    )

    change_set_run: Mapped[ChangeSetRun] = relationship(
        "ChangeSetRun",
        uselist=False,
        back_populates="checks",
    )

    def to_check_status(self) -> str:
        return "in_progress" if self.status == "pending" else "completed"

    def to_check_conclusion(self) -> str | None:
        if self.status in ["error", "failure"]:
            return "failure"
        elif self.status == "success":
            return "success"
        return None


class ChangeSetRunCommit(BaseModel, deprecated_models.DeprecatedChangeSetRunCommit):
    """
    Every time a ChangeSetRun is triggered, we create a ChangeSetRunCommit for each commit that
    we have sent out in the original request. That way we can map the returned status to each
    specific commit sha.
    """

    __tablename__ = "change_set_run_commit"
    account_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )
    change_set_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("change_set.id"),
        nullable=False,
    )
    change_set_run_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("change_set_run.id"),
        nullable=False,
        index=True,
    )
    sha: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
    )
    pull_request_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("pull_request.id"),
        nullable=False,
    )

    pull_request: Mapped[PullRequest] = relationship(
        "PullRequest",
        uselist=False,
    )
    change_set_run: Mapped[ChangeSetRun] = relationship(
        "ChangeSetRun",
        uselist=False,
        back_populates="commits",
    )

    __table_args__ = (
        UniqueConstraint(
            "change_set_run_id", "pull_request_id", name="change_set_commit"
        ),
    )

    def get_github_check(self, check: ChangeSetRunCheck) -> ChangeSetCommitCheck | None:
        commit_check: None | (
            ChangeSetCommitCheck
        ) = ChangeSetCommitCheck.query.filter_by(
            change_set_run_commit_id=self.id, change_set_run_check_id=check.id
        ).first()
        return commit_check

    def set_github_check_id(
        self, check: ChangeSetRunCheck, check_run_id: int
    ) -> ChangeSetCommitCheck:
        github_check = self.get_github_check(check)
        if not github_check:
            github_check = ChangeSetCommitCheck(
                change_set_run_commit_id=self.id,
                change_set_run_check_id=check.id,
                check_run_id=check_run_id,
            )
            db.session.add(github_check)
        else:
            github_check.check_run_id = check_run_id
        return github_check


class GithubRepo(BaseModel, deprecated_models.DeprecatedGithubRepo):
    @staticmethod
    def get(name: str, *, account_id: int) -> GithubRepo | None:
        """
        Get a GithubRepo by name for the matching account.
        """
        repo: GithubRepo | None = GithubRepo.query.filter_by(
            account_id=account_id, name=name
        ).first()
        return repo

    @staticmethod
    def get_by_id_for_account(account_id: int, repo_id: int) -> GithubRepo | None:
        """
        Get a GithubRepo by id for the matching account.
        """
        repo: GithubRepo | None = GithubRepo.query.filter_by(
            account_id=account_id, id=repo_id
        ).one_or_none()
        return repo

    @staticmethod
    def get_all_for_account(account_id: int) -> list[GithubRepo]:
        """
        Get all GithubRepos for the matching account.
        """
        repos: list[GithubRepo] = GithubRepo.query.filter_by(
            account_id=account_id
        ).all()
        return repos

    __tablename__ = "github_repo"
    __table_args__ = (sa.Index("github_repo_name", "name"),)

    name: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
        doc="Full repo name (e.g. acmecorp/myrepo)",
    )
    account_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )
    organization: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
    )
    active: Mapped[bool] = sa.Column(
        sa.Boolean,
        nullable=False,
        server_default=sa.false(),
        doc=(
            "If true, the repository is being used with Aviator/MergeQueue. "
            "Does not affect ChangeSets."
        ),
    )
    flexreview_active: Mapped[bool] = sa.Column(
        sa.Boolean,
        nullable=False,
        server_default="false",
        doc="If true, the repository is being used with Aviator/FlexReview.",
    )
    flexreview_merged_pull_backfill_next_node: Mapped[str | None] = sa.Column(
        sa.Text(),
        nullable=True,
        doc="""The GitHub node ID that is used for merged pull request data backfill.

        If this is an empty string, it means that a backfill task starts from the
        beginning. If this is null, it means that the backfill is done or not started.
        """,
    )
    flexreview_backfill_started_at: Mapped[datetime.datetime | None] = sa.Column(
        sa.TIMESTAMP(timezone=True),
        nullable=True,
        doc=("The timestamp when the FlexReview backfill was started. "),
    )
    flexreview_backfill_last_processed_at: Mapped[datetime.datetime | None] = sa.Column(
        sa.TIMESTAMP(timezone=True),
        nullable=True,
        doc=("The timestamp when the FlexReview backfill was processed. "),
    )
    flexreview_backfill_last_pr_created_at: Mapped[datetime.datetime | None] = (
        sa.Column(
            sa.TIMESTAMP(timezone=True),
            nullable=True,
            doc=("The timestamp of the oldest backfilled PR creation time. "),
        )
    )
    enable_flexreview_approvals: Mapped[bool] = sa.Column(
        sa.Boolean,
        nullable=False,
        server_default="false",
        doc="If true, the repository should check FlexReview Approvals.",
    )
    flexreview_custom_status_comment: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
        doc="Custom status comment injected into sticky comment.",
    )

    active_for_insights: Mapped[bool | None] = sa.Column(
        sa.Boolean,
        nullable=True,
        server_default=sa.true(),
        doc=("If true, Aviator will fetch stats and insights for the repository. "),
    )
    enabled: Mapped[bool] = sa.Column(
        sa.Boolean,
        nullable=False,
        server_default=sa.false(),
        doc=(
            "If False, MergeQueue is paused in the repository and will not "
            "merge pull requests. This is generally True only if active is "
            "True."
        ),
    )
    head_branch: Mapped[str] = sa.Column(
        sa.Text(),
        nullable=False,
        server_default="master",
        doc=(
            "The base branch of the repository on GitHub. We use this as the "
            "default in all_base_branches below for determining which branches "
            "we'll merge PRs into."
        ),
    )
    billing_active: Mapped[bool] = sa.Column(
        sa.Boolean,
        nullable=False,
        server_default=sa.true(),
    )  # Deprecated

    access_token_id: Mapped[int | None] = sa.Column(
        sa.Integer,
        nullable=True,
    )
    require_verified_commits: Mapped[bool] = sa.Column(
        sa.Boolean,
        nullable=False,
        server_default=sa.false(),
        doc=(
            "If true, Aviator enforces that all commits in a PR have been "
            "signed and verified by GitHub (i.e., every commit must be signed "
            "with a GPG key that GitHub knows about)."
        ),
    )
    configurations_from_file: Mapped[bool] = sa.Column(
        sa.Boolean,
        nullable=False,
        server_default=sa.false(),
    )
    flexreview_team_author_allowlist: Mapped[list[str]] = sa.Column(
        ARRAY(sa.Text, dimensions=1),
        nullable=False,
        server_default='{"*"}',
        doc="""
        A list of user names and team names for doing assignments by FlexReview.
        If a PR is created by these users, FlexReview will do the assignments based on
        .aviator/OWNERS file. Syntax is `@username` for users and `@org/team` for teams.

        A special marker '*' can be used to allow all users.
        """,
    )
    flexreview_reviewer_exclusion_list: Mapped[list[str]] = sa.Column(
        ARRAY(sa.Text, dimensions=1),
        nullable=False,
        server_default="{}",
        doc="""
        A list of user names and team names for excluding them from the reviewer
        assignment by FlexReview. Syntax is `@username` for users and `@org/team` for
        teams.
        """,
    )
    flexreview_ignore_list: Mapped[list[str]] = sa.Column(
        ARRAY(sa.Text, dimensions=1),
        nullable=False,
        server_default="{}",
        doc="""
        A list of user names that will be completely ignored by FlexReview.
        They will not be assigned as reviewers, and if they are assigned
        in other ways we will also ignore them.
        Syntax is `@username`.
        """,
    )
    flexreview_validation_base_branch_list: Mapped[list[str]] = sa.Column(
        ARRAY(sa.Text, dimensions=1),
        nullable=False,
        server_default="{}",
        doc="""
        A list of target branches that flexreview will enforce validation on.
        """,
    )
    flexreview_breakglass_slack_channel: Mapped[str | None] = sa.Column(
        sa.Text(),
        nullable=True,
        doc="""
        The slack channel to send breakglass notifications to.
        """,
    )
    signals_last_calculated_year_week: Mapped[str | None] = sa.Column(
        sa.Text(),
        nullable=True,
        doc="""
        The ISO year week (e.g. 2025-W01) of the last calculated aggregated signals
        data.
        """,
    )
    signals_last_calculated_commit_hash: Mapped[str | None] = sa.Column(
        sa.Text(),
        nullable=True,
        doc="""The commit hash of the last calculated aggregated signals data.""",
    )
    signals_last_calculated_at: Mapped[datetime.datetime | None] = sa.Column(
        sa.TIMESTAMP(timezone=True),
        nullable=True,
        doc="""The timestamp of the last calculated aggregated signals data.""",
    )

    account: Mapped[Account] = relationship(
        "Account",
        uselist=False,
    )
    required_users: Mapped[list[GithubUser]] = relationship(
        "GithubUser",
        secondary=required_repo_user,
        uselist=True,
    )
    regex_config: Mapped[list[RegexConfig]] = relationship(
        "RegexConfig",
        uselist=True,
    )
    starred: Mapped[bool] = sa.Column(
        sa.Boolean,
        nullable=False,
        server_default=sa.false(),
    )
    enable_testdeck: Mapped[bool] = sa.Column(
        "enable_flakybot",
        sa.Boolean,
        nullable=False,
        server_default=sa.false(),
    )

    release_projects: Mapped[list[ReleaseProject]] = relationship(
        "ReleaseProject",
        secondary="release_project_repo_config",
        uselist=True,
        back_populates="repos",
    )

    # This is a local cache of the Configuration
    _config_history: Configuration | None = None

    @property
    def org_name(self) -> str:
        return self.name.split("/")[0]

    @property
    def repo_name(self) -> str:
        name_splits = self.name.split("/")
        return name_splits[1] if len(name_splits) == 2 else self.name

    @property
    def html_url(self) -> str:
        return GITHUB_BASE_URL + "/" + self.name

    def get_branch_url(self, branch: str) -> str:
        return self.html_url + "/tree/" + branch

    def get_required_tests(self, *, botpr: bool = False) -> list[GithubTest]:
        """
        Returns a list of tests we should verify for a CI run.

        If for_botpr=True, we check if the repo has status checks specifically set for draft PR CI runs. If there
        are no specified BotPR checks, we default to using the checks for original PR CI runs.

        If repo.preconditions.use_github_mergeability=True, we use the tests that are marked as github_required=True. These are the
        tests that have been explicitly marked as required in Github.

        if repo.preconditions.use_github_mergeability=False, we use the tests that are marked as ignore=False. These are the tests
        that have been explicitly marked as required in the Aviator UI.

        :param botpr: True if we should fetch the required checks for a BotPR in the repo.
        """
        if botpr:
            bot_checks: list[GithubTest] = GithubTest.query.filter_by(
                repo_id=self.id,
                is_required_bot_pr_check=True,
            ).all()
            if len(bot_checks) > 0:
                return bot_checks

        if self.preconditions.use_github_mergeability:
            return GithubTest.query.filter_by(
                repo_id=self.id, github_required=True
            ).all()

        return GithubTest.query.filter_by(
            repo_id=self.id,
            ignore=False,
        ).all()

    @property
    def all_valid_tests(self) -> list[GithubTest]:
        return GithubTest.query.filter_by(repo_id=self.id, ignore=False).all()

    @property
    def queue_labels(self) -> list[str]:
        valid_labels: list[GithubLabel] = (
            GithubLabel.query.filter_by(repo_id=self.id)
            .filter(
                GithubLabel.purpose.in_(
                    [
                        GithubLabelPurpose.Queue.value,
                        GithubLabelPurpose.Merge.value,
                        GithubLabelPurpose.Rebase.value,
                        GithubLabelPurpose.Squash.value,
                        GithubLabelPurpose.SkipLine.value,
                    ]
                )
            )
            .all()
        )
        return [label.name for label in valid_labels]

    @property
    def queue_label(self) -> str:
        label: GithubLabel | None = GithubLabel.query.filter_by(
            repo_id=self.id,
            purpose=GithubLabelPurpose.Queue.value,
        ).first()
        if label is None:
            return DEFAULT_QUEUE_LABEL
        return label.name

    @property
    def skip_delete_labels(self) -> list[GithubLabel]:
        return GithubLabel.query.filter_by(repo_id=self.id, purpose="skip_delete").all()

    @property
    def blocked_labels(self) -> list[GithubLabel]:
        return GithubLabel.query.filter_by(repo_id=self.id, purpose="blocked").all()

    @property
    def blocked_label(self) -> str:
        return (
            self.blocked_labels[0].name
            if self.blocked_labels
            else DEFAULT_BLOCKED_LABEL
        )

    @property
    def auto_sync_label(self) -> GithubLabel | None:
        return GithubLabel.query.filter_by(repo_id=self.id, purpose="auto_sync").first()

    @property
    def skip_line_label(self) -> GithubLabel | None:
        return GithubLabel.query.filter_by(repo_id=self.id, purpose="skip_line").first()

    @property
    def merge_labels(self) -> list[GithubLabel]:
        return (
            GithubLabel.query.filter_by(repo_id=self.id)
            .filter(GithubLabel.purpose.in_(["merge", "squash", "rebase"]))
            .order_by(GithubLabel.id)
            .all()
        )

    @property
    def parallel_mode_stuck_label(self) -> GithubLabel | None:
        return GithubLabel.query.filter_by(
            repo_id=self.id,
            purpose=GithubLabelPurpose.ParallelStuck.value,
        ).first()

    @property
    def parallel_mode_barrier_label(self) -> GithubLabel | None:
        return GithubLabel.query.filter_by(
            repo_id=self.id,
            purpose=GithubLabelPurpose.ParallelBarrier.value,
        ).first()

    @property
    def base_branch_patterns(self) -> list[str]:
        """
        Return a list of all the valid base branch patterns for PRs in this repo.

        Use :meth:`is_valid_base_branch` to check if a branch is valid base
        branch (i.e., matches one of these patterns).
        """
        return [bb.name for bb in BaseBranch.query.filter_by(repo_id=self.id).all()]

    @property
    def current_config_history(self) -> ConfigHistory | None:
        config_history_record: ConfigHistory | None = (
            ConfigHistory.query.filter_by(repo_id=self.id, config_type=ConfigType.Main)
            .filter(ConfigHistory.applied_at.isnot(None))
            .order_by(ConfigHistory.applied_at.desc())
            .first()
        )
        return config_history_record

    @property
    def current_config(self) -> Configuration:
        """
        :return: the latest configuration for this repo
        """
        if self._config_history:
            return self._config_history
        config_history_record = self.current_config_history
        if not config_history_record:
            # BAD: find some better way of structuring dependencies
            from core.configurator import Configurator

            configurator = Configurator(self)
            # Not setting the scenarios and flakybot config as those are only
            # stored in the config file and will be null if no config history exists.
            self._config_history = Configuration(
                merge_rules=configurator.generate_merge_rules(),
            )
            return self._config_history
        self._config_history = Configuration.model_validate(
            config_history_record.config_data
        )
        return self._config_history

    @property
    def merge_commit(self) -> MergeCommit:
        if (
            self.current_config.merge_rules
            and self.current_config.merge_rules.merge_commit
        ):
            return self.current_config.merge_rules.merge_commit
        logger.info("MergeCommit config does not exist", repo_id=self.id)
        return MergeCommit()

    @property
    def update_latest(self) -> bool:
        if self.current_config.merge_rules and (
            self.current_config.merge_rules.update_latest is not None
        ):
            # Since we want to set the default value as True, we will only
            # return the property when it exists.
            return self.current_config.merge_rules.update_latest
        return True

    @property
    def delete_branch(self) -> bool:
        if (
            self.current_config.merge_rules
            and self.current_config.merge_rules.delete_branch
        ):
            return self.current_config.merge_rules.delete_branch
        return False

    @property
    def use_rebase(self) -> bool:
        if (
            self.current_config.merge_rules
            and self.current_config.merge_rules.use_rebase
        ):
            return self.current_config.merge_rules.use_rebase
        return False

    @property
    def enable_comments(self) -> bool:
        if (
            self.current_config.merge_rules
            and self.current_config.merge_rules.enable_comments
        ):
            return self.current_config.merge_rules.enable_comments
        return True

    @property
    def ci_timeout_mins(self) -> int:
        if (
            self.current_config.merge_rules
            and self.current_config.merge_rules.ci_timeout_mins
        ):
            return self.current_config.merge_rules.ci_timeout_mins
        return 0

    @property
    def require_all_checks_pass(self) -> bool:
        if (
            self.current_config.merge_rules
            and self.current_config.merge_rules.require_all_checks_pass
        ):
            return self.current_config.merge_rules.require_all_checks_pass
        return False

    @property
    def auto_update(self) -> AutoUpdate:
        if (
            self.current_config.merge_rules
            and self.current_config.merge_rules.auto_update
        ):
            return self.current_config.merge_rules.auto_update
        logger.info("AutoUpdate config does not exist", repo_id=self.id)
        return AutoUpdate()

    @property
    def merge_strategy(self) -> MergeStrategy:
        if (
            self.current_config.merge_rules
            and self.current_config.merge_rules.merge_strategy
        ):
            return self.current_config.merge_rules.merge_strategy
        logger.info("MergeStrategy config does not exist", repo_id=self.id)
        return MergeStrategy()

    @property
    def preconditions(self) -> Preconditions:
        if (
            self.current_config.merge_rules
            and self.current_config.merge_rules.preconditions
        ):
            return self.current_config.merge_rules.preconditions
        logger.info("Preconditions config does not exist", repo_id=self.id)
        return Preconditions()

    @property
    def merge_mode_type(self) -> str:
        if (
            self.current_config.merge_rules
            and self.current_config.merge_rules.merge_mode
        ):
            return self.current_config.merge_rules.merge_mode.type
        logger.info("MergeMode config does not exist", repo_id=self.id)
        return MergeMode().type

    @property
    def is_no_queue_mode(self) -> bool:
        return self.merge_mode_type == "no-queue"

    @property
    def parallel_mode(self) -> bool:
        return self.merge_mode_type == "parallel"

    @property
    def parallel_mode_config(self) -> ParallelMode:
        if (
            self.current_config.merge_rules
            and self.current_config.merge_rules.merge_mode
            and self.current_config.merge_rules.merge_mode.parallel_mode
        ):
            return self.current_config.merge_rules.merge_mode.parallel_mode
        return ParallelMode()

    @property
    def batch_size(self) -> int:
        if self.parallel_mode_config.batch_size:
            return self.parallel_mode_config.batch_size
        return 1

    @property
    def optimistic_validation_failure_depth(self) -> int:
        if self.parallel_mode_config.optimistic_validation_failure_depth:
            return self.parallel_mode_config.optimistic_validation_failure_depth
        return 1

    @functools.cached_property
    def flexreview_ignored_github_users(self) -> set[GithubUser]:
        """Generate set of all GithubUsers that are to be ignored by the
        repo's reviewer pool. This includes: inactive collaborators and users
        in the repo ignore list"""

        ret: set[GithubUser] = set()

        # Process the ignore list in order. Same as in exclusion list, we
        # allow negation so the order matters.
        # Ex: an org may have a team for bot users, but one bot may be used
        # for automated review approvals.
        for entry in self.flexreview_ignore_list:
            negate, user, team = _parse_user_or_team_entry(self.account_id, entry)
            if negate or team:
                logger.info(
                    "Ignoring entry in flexreview_ignore_list, only users are supported",
                    entry=entry,
                    negate=negate,
                    team=team,
                )
            if user:
                ret.add(user)

        return ret

    @functools.cached_property
    def flexreview_excluded_reviewer_github_users(self) -> set[GithubUser]:
        """Generate set of all GithubUsers that are to be excluded from the
        repo's reviewer pool. This includes: inactive collaborators, users in the repo
        exclusion list, and eventually users that are currently on PTO"""

        # start with the ignored users
        ret: set[GithubUser] = self.flexreview_ignored_github_users

        # Process the exclusion list in order. Because there's a negation flag, the
        # order matters.
        for entry in self.flexreview_reviewer_exclusion_list:
            negate, user, team = _parse_user_or_team_entry(self.account_id, entry)
            if user:
                if negate:
                    ret.discard(user)
                else:
                    ret.add(user)
            if team:
                if negate:
                    ret.difference_update(team.members_recursively)
                else:
                    ret.update(team.members_recursively)

        # Add all inactive collaborators and OOO users. Note that in order not to
        # exclude them via the negation rules, we add them after the exclusion list.
        ret.update(
            db.session.scalars(
                sa.select(GithubUser).where(
                    GithubUser.account_id == self.account_id,
                    GithubUser.active_collaborator.is_(False),
                ),
            ),
        )
        ret.update(OutOfOfficeEntry.get_ooo_gh_users(account_id=self.account_id))
        return ret

    def is_valid_base_branch(self, branch_name: str) -> bool:
        """
        Returns True if the given branch name is a base branch for this repo.

        :param branch_name: The branch name to check.
        """
        if not self.base_branch_patterns:
            # If no base branch patterns configured, assume every branch is a base branch.
            return True
        return any(
            # Match globs
            fnmatch.fnmatch(branch_name, pattern)
            for pattern in self.base_branch_patterns
        )

    def is_base_branch_paused(self, branch_name: str) -> bool:
        """
        Returns True if the given branch name is a base branch for this repo and is paused.
        Returns False if the branch is not a base branch or if the branch is not paused.

        :param branch_name: The branch name to check.
        """
        base_branches = BaseBranch.query.filter_by(repo_id=self.id).all()
        if not base_branches:
            # If no base branch patterns configured, assume every branch is a base branch.
            return False
        return any(
            # Match globs
            fnmatch.fnmatch(branch_name, base_branch.name) and base_branch.paused
            for base_branch in base_branches
        )

    def get_collaborators(self) -> list[GithubUser]:
        rc: list[RepoCollaborator] = RepoCollaborator.query.filter_by(
            repo_id=self.id
        ).all()
        return [r.user for r in rc]

    def as_json(self) -> dict:
        return dict(
            name=self.repo_name,
            org=self.org_name,
            paused=not self.enabled,
            active=self.active,
        )

    def rbac_entity(self) -> str:
        return f"repo:{self.name}"

    def bind_contextvars(self) -> None:
        structlog.contextvars.bind_contextvars(**self.__contextvars())

    @contextlib.contextmanager
    def bound_contextvars(self) -> Generator[None]:
        with structlog.contextvars.bound_contextvars(**self.__contextvars()):
            yield

    def __contextvars(self) -> dict[str, object]:
        return {
            "repo_id": self.id,
            "account_id": self.account_id,
        }


class TestStatus(enum.Enum):
    Success = "success"
    Pending = "pending"
    Failure = "failure"
    Cancelled = "cancelled"
    TimedOut = "timed_out"
    Skipped = "skipped"
    Neutral = "neutral"
    ActionRequired = "action_required"
    Stale = "stale"
    Unknown = "unknown"
    Error = "error"
    Blocked = "blocked"
    Missing = "missing"

    def __str__(self) -> str:
        return self.value

    @property
    def description(self) -> str:
        return self.value.replace("_", " ")


class AcceptableTestStatus(DBModel):
    __tablename__ = "acceptable_test_status"

    github_test_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_test.id"),
        primary_key=True,
        nullable=False,
    )
    status: Mapped[TestStatus] = sa.Column(
        dbutil.TextEnum(TestStatus),
        primary_key=True,
        nullable=False,
        server_default=TestStatus.Unknown.value,
    )

    for_override: Mapped[bool] = sa.Column(
        sa.Boolean,
        primary_key=True,
        nullable=False,
        server_default=sa.false(),
    )


class GithubTest(BaseModel):
    @staticmethod
    def get_by_name(repo_id: int, name: str) -> GithubTest | None:
        return GithubTest.query.filter_by(repo_id=repo_id, name=name).first()

    @staticmethod
    def all_for_repo(repo_id: int) -> list[GithubTest]:
        """
        Get all the GithubTests for a repository.
        """
        return GithubTest.query.filter_by(repo_id=repo_id).all()

    __tablename__ = "github_test"
    __table_args__ = (
        sa.Index("github_test_unique_repo_name", "repo_id", "name", unique=True),
    )

    name: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )
    repo_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_repo.id", ondelete="CASCADE"),
        nullable=False,
    )
    enabled: Mapped[bool] = sa.Column(
        sa.Boolean,
        nullable=False,
        server_default=sa.true(),
    )
    ignore: Mapped[bool] = sa.Column(
        sa.Boolean,
        nullable=False,
        server_default=sa.true(),
        doc=(
            "If true, we ignore this test when validating an original PR's CI run. If false, we need to verify"
            "that this test passes in the original PR's CI run."
        ),
    )
    github_required: Mapped[bool] = sa.Column(
        sa.Boolean,
        nullable=False,
        server_default=sa.false(),
    )
    is_required_bot_pr_check: Mapped[bool] = sa.Column(
        sa.Boolean,
        nullable=False,
        server_default=sa.false(),
        doc="If true, we need to verify that this test passes in draft PR CI runs.",
    )
    provider: Mapped[dbutil.JobProviders] = sa.Column(
        dbutil.TextEnum(dbutil.JobProviders),
        nullable=False,
        server_default=dbutil.JobProviders.unknown.value,
    )
    starred: Mapped[bool] = sa.Column(
        sa.Boolean,
        nullable=False,
        server_default=sa.false(),
    )
    publish_report = sa.Column(
        sa.Boolean,
        nullable=False,
        server_default=sa.false(),
        doc="If true, publish a status check back to GitHub for TestDeck.",
    )  # publish a status check back on Github
    connected: Mapped[bool] = sa.Column(
        sa.Boolean,
        nullable=False,
        server_default=sa.false(),
        doc="If true, we are receiving the test reports from the CI provider.",
    )
    provider_params: Mapped[dict] = sa.Column(
        dbutil.JSONDictType(),
        nullable=False,
        server_default=sa.text("'{}'::jsonb"),
        doc=(
            "JSON-encoded metadata for this test. This is used to store provider specific "
            "information about the test."
        ),
    )

    repo: Mapped[GithubRepo] = relationship("GithubRepo", uselist=False)

    def get_recent_runs(self, limit: int = 100) -> list[GithubTestStatus]:
        return (
            GithubTestStatus.query.filter_by(github_test_id=self.id)
            .order_by(GithubTestStatus.created.desc())
            .limit(limit)
        )

    def flaky_test_cases(
        self,
        *,
        look_back_days: int,
        limit: int | None,
        offset: int | None,
    ) -> dict[TestCase, dict]:
        """
        Gets details for flaky tests for the Dashboard. Returns data in the following format:

        data = {
          {TestCase}: {
              "flaky_run": {the latest flaky TestCaseRun},
          }
        }
        """
        start_of_time_window = time_util.now() - datetime.timedelta(days=look_back_days)
        test_cases_query = (
            TestCase.query.filter_by(github_test_id=self.id)
            .filter(TestCase.last_flaky_date > start_of_time_window)
            .order_by(TestCase.last_flaky_date.desc())
        )
        if limit:
            test_cases_query = test_cases_query.limit(limit)
        if offset:
            test_cases_query = test_cases_query.offset(offset)
        test_cases = test_cases_query.all()

        data = {}
        for test_case in test_cases:
            latest_flaky_run = test_case.get_latest_flaky()
            if not latest_flaky_run:
                continue
            data[test_case] = {
                "flaky_run": latest_flaky_run,
            }
        return data

    def get_master_runs(
        self, branches: list[str] | None = None, page_size: int = 100
    ) -> list[TestReport]:
        if not branches:
            branches = ["master", "main"]
        return (
            TestReport.query.filter_by(github_test_id=self.id)
            .join(
                BranchCommitMapping,
                BranchCommitMapping.git_commit_id == GithubTestStatus.commit_id,
            )
            .filter(BranchCommitMapping.branch_name.in_(branches))
            .order_by(GithubTestStatus.created.desc())
            .limit(page_size)
            .all()
        )

    def __repr__(self) -> str:
        return f"<GithubTest {self.id} (repo: {self.repo_id}; name: {self.name})>"


class GithubTestStatus(BaseModel):
    __tablename__ = "github_test_status"
    __table_args__ = (
        sa.Index("repo_commit", "head_commit_sha", "repo_id"),
        sa.Index("repo_commit_id", "commit_id", "repo_id"),
        sa.Index("repo_test_id", "github_test_id", "repo_id"),
    )

    github_test_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_test.id"),
        nullable=False,
    )
    repo_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_repo.id", ondelete="CASCADE"),
        nullable=False,
    )
    commit_id: Mapped[int | None] = sa.Column(
        sa.BigInteger,
        sa.ForeignKey("git_commit.id", ondelete="CASCADE"),
        nullable=True,
    )
    is_complete: Mapped[bool] = sa.Column(
        sa.Boolean,
        nullable=False,
        server_default=sa.false(),
    )
    # TODO:
    #   Will deprecate head_commit_sha from GithubTestStatus and use sha from GitCommit.
    head_commit_sha: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )
    job_url: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )
    external_id: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
        index=True,
    )  # represents the identifier with the provider
    nightly = sa.Column(
        sa.Boolean,
        nullable=False,
        server_default=sa.false(),
    )  # whether this was part of nightly run
    status: Mapped[TestStatus] = sa.Column(
        dbutil.TextEnum(TestStatus),
        nullable=False,
        server_default=TestStatus.Unknown.value,
    )

    status_updated_at: Mapped[datetime.datetime | None] = sa.Column(
        sa.TIMESTAMP(timezone=True),
        nullable=True,
        doc=(
            "The timestamp when the test status was updated. "
            "This is updated_at for GitHub commit status and completed_at for "
            "GitHub check-runs."
        ),
    )
    fetched_at: Mapped[datetime.datetime | None] = sa.Column(
        sa.TIMESTAMP(timezone=True),
        nullable=True,
        doc=(
            "The timestamp when the test status was fetched from GitHub. "
            "This is used to try to avoid overwriting test statuses with stale "
            "data since we don't have a guaranteed order for how we parse "
            "GitHub webhooks. This isn't perfect, but it's probably better "
            "than nothing."
        ),
    )

    github_test: Mapped[GithubTest] = relationship(
        "GithubTest",
        uselist=False,
    )
    commit: Mapped[GitCommit] = relationship(
        "GitCommit",
        uselist=False,
    )
    repo: Mapped[GithubRepo] = relationship(
        "GithubRepo",
        uselist=False,
    )


class GithubLabelPurpose(enum.Enum):
    NoPurpose = "none"
    # This label is added by users to add the pull request to the queue.
    # Historically, this label had many different names: ready label, queue
    # label, trigger label, and/or merge label. We've since settled on the name
    # "queue label." All internal code and external documentation should refer
    # to it as the "queue label" as much as possible while the database value is
    # still "ready".
    Queue = "ready"
    # This label is added by users to indicate that the branch associated
    # with this PR should NOT be deleted after it is merged.
    SkipDelete = "skip_delete"
    # This label is added by MergeQueue to indicate that the PR could not be
    # merged for some reason (such as failing tests).
    Blocked = "blocked"
    # This label can be added to a PR to tell MergeQueue to automatically sync
    # changes from the target branch into this PR whenever new commits are added
    # to the target branch.
    AutoSync = "auto_sync"
    # This label can be added to tell MergeQueue to move this pull request to
    # the front of the queue, skipping everything that was queued before it.
    # This causes a queue reset and should be used sparingly.
    SkipLine = "skip_line"
    # This label can be added to a PR to tell MergeQueue to merge the changes
    # from this PR into the target branch using the merge commit strategy.
    Merge = "merge"
    # This label can be added to a PR to tell MergeQueue to merge the changes
    # from this PR into the target branch using the squash commit strategy.
    Squash = "squash"
    # This label can be added to a PR to tell MergeQueue to merge the changes
    # from this PR into the target branch using the rebase strategy.
    Rebase = "rebase"
    # The stuck label is added by MergeQueue in parallel mode if a BotPR passes
    # all the relevant CI checks, but the original PR is not (yet) passing after
    # a configurable timeout.
    ParallelStuck = "beast_failed"
    # This label can be added by users to tell MergeQueue that no PRs should be
    # enqueued behind the labeled PR. This can be helpful for PRs that change
    # CI configuration.
    # (This was renamed to be more obvious, but the value in the database is
    # still "block_beast").
    ParallelBarrier = "block_beast"


class ValidationType(enum.Enum):
    Body = "body"
    Title = "title"


class Validation(BaseModel):
    __tablename__ = "validation"
    type: Mapped[ValidationType] = sa.Column(
        dbutil.TextEnum(ValidationType),
        nullable=False,
    )
    name: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )
    value: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )
    repo_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_repo.id", ondelete="CASCADE"),
        nullable=False,
    )


class GithubLabel(BaseModel):
    __tablename__ = "github_label"
    name: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )
    purpose: Mapped[GithubLabelPurpose] = sa.Column(
        dbutil.TextEnum(GithubLabelPurpose),
        nullable=False,
        server_default=GithubLabelPurpose.NoPurpose.value,
    )
    repo_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_repo.id", ondelete="CASCADE"),
        nullable=False,
    )

    __table_args__ = (sa.Index("github_label_repo_id", "repo_id"),)


class GithubTeamMembers(DBModel):
    __tablename__ = "github_team_members"
    __table_args__ = (
        # The reverse index to the primary key index.
        sa.Index(
            "idx_github_team_members_user_id_team_id",
            "github_user_id",
            "github_team_id",
            unique=True,
        ),
    )
    github_team_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_team.id", ondelete="CASCADE"),
        primary_key=True,
    )
    github_user_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_user.id", ondelete="CASCADE"),
        primary_key=True,
    )
    added_on = sa.Column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )


class PreviousGithubTeamMember(DBModel):
    __tablename__ = "previous_github_team_member"
    __table_args__ = (
        sa.Index(
            "idx_previous_github_team_member_user_id_team_id",
            "github_user_id",
            "github_team_id",
            unique=True,
        ),
    )
    github_team_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_team.id", ondelete="CASCADE"),
        primary_key=True,
    )
    github_user_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_user.id", ondelete="CASCADE"),
        primary_key=True,
    )
    removed_on = sa.Column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )


class GithubTeamOwnership(DBModel):
    __tablename__ = "github_team_ownership"
    id: Mapped[int] = sa.Column(sa.Integer, primary_key=True)
    path_pattern: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
        doc="""
        The code path patterns that the team owns.
        This is based on the format used by GitHub Codeowners:
        https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners#codeowners-syntax
        """,
    )
    slug: Mapped[str] = sa.Column(sa.Text, nullable=False, doc="The team or user slug")
    repo_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_repo.id", ondelete="CASCADE"),
        nullable=False,
    )
    github_team_id: Mapped[int | None] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_team.id", ondelete="CASCADE"),
        nullable=True,
        doc="""
            The team that owns the code path. This property can be null to represent
            an invalid team name or when a user is directly assigned instead of the team.
            """,
    )
    github_user_id: Mapped[int | None] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_user.id", ondelete="CASCADE"),
        nullable=True,
        doc="""
        The user that owns the code path. This property will be when when a team owns
        the code path.
        """,
    )
    prs_authored_count: Mapped[int] = sa.Column(
        sa.Integer,
        nullable=False,
        server_default="0",
        doc="The number of PRs authored by the team or user associated with the path.",
    )
    prs_reviewed_count: Mapped[int] = sa.Column(
        sa.Integer,
        nullable=False,
        server_default="0",
        doc="The number of PRs approved by the team or user associated with the path.",
    )
    total_prs_count: Mapped[int] = sa.Column(
        sa.Integer,
        nullable=False,
        server_default="0",
        doc="The total number of PRs that touch the given path",
    )

    github_team: Mapped[GithubTeam | None] = relationship(
        "GithubTeam",
        back_populates="ownership_list",
        uselist=False,
    )
    github_user: Mapped[GithubUser | None] = relationship(
        "GithubUser",
        back_populates="ownership_list",
        uselist=False,
    )
    repo: Mapped[GithubRepo] = relationship(
        "GithubRepo",
        uselist=False,
    )


class SuggestionsReason(enum.Enum):
    # The team has authored PRs that touch the given path
    Author = "author"
    # The team has reviewed PRs that touch the given path
    Reviewer = "reviewer"
    # The team owns adjacent paths to the given path
    Adjacency = "adjacency"


class SuggestedOwner(IDModel):
    __tablename__ = "suggested_owner"
    path_pattern: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
        doc="""
        The code path patterns that the team owns.
        This is based on the format used by GitHub Codeowners:
        https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners#codeowners-syntax
        """,
    )
    slug: Mapped[str] = sa.Column(sa.Text, nullable=False, doc="The team slug")
    repo_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_repo.id", ondelete="CASCADE"),
        nullable=False,
    )
    github_team_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_team.id", ondelete="CASCADE"),
        nullable=False,
        doc="""
            The team that owns the code path.
            """,
    )
    prs_authored_count: Mapped[int] = sa.Column(
        sa.Integer,
        nullable=False,
        server_default="0",
        doc="The number of PRs authored by the team associated with the path.",
    )
    prs_reviewed_count: Mapped[int] = sa.Column(
        sa.Integer,
        nullable=False,
        server_default="0",
        doc="The number of PRs approved by the team associated with the path.",
    )

    github_team: Mapped[GithubTeam] = relationship(
        "GithubTeam",
        uselist=False,
    )
    suggestion_reason: Mapped[SuggestionsReason] = sa.Column(
        dbutil.TextEnum(SuggestionsReason),
        nullable=False,
    )


class SyncStatus(enum.Enum):
    # NEW: Organization is new and currently being fetched. Existing cache doesn't exist
    NEW = "new"

    # LOADING: A sync process is currently running. There is an existing cache available
    LOADING = "loading"

    # SUCCESS: No process is currently running. Cache was fetched at synced_at
    SUCCESS = "success"

    # FAILED: The most recent attempt to sync failed. Cache exists but might be out of date
    FAILED = "failed"

    # INVALID: A special flag to work around "pseudo-orgs" which are actually users.
    # Context: Team only exists in organizations. As of today (2/2/2024) we don't have an
    # appropriate data model for organizations. Instead the organization is parsed from the
    # full repo name. As a result, what we perceive as "repo name" might actually be a user name.
    INVALID = "invalid"


class GithubTeamSyncStatus(DBModel):
    __tablename__ = "github_team_sync_status"
    account_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("account.id", ondelete="CASCADE"),
        primary_key=True,
    )
    organization: Mapped[str] = sa.Column(
        sa.Text,
        primary_key=True,
    )
    status: Mapped[SyncStatus] = sa.Column(
        dbutil.TextEnum(SyncStatus),
        nullable=False,
        server_default=SyncStatus.NEW.value,
    )
    synced_at: Mapped[datetime.datetime] = sa.Column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
    )


@dataclasses.dataclass(frozen=True)
class GithubTeamFlexReviewInclusionExclusionListEntry:
    negate: bool
    user: GithubUser | None
    team: GithubTeam | None


class GithubTeam(BaseModel):
    __tablename__ = "github_team"
    __table_args__ = (
        sa.Index("github_team_account_id", "account_id"),
        sa.Index("github_team_account_id_organization", "account_id", "organization"),
        sa.Index(
            "github_team_account_combined_slug",
            "account_id",
            "organization",
            "slug",
            unique=True,
        ),
        sa.Index("github_team_database_id", "github_database_id", unique=True),
    )

    name: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )
    # NOTE: `slug` is generated from `name` and should be used as the human-friendly id
    # However this can be changed and is unreliable for syncing with Github
    slug: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )
    organization: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )
    github_node_id: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )
    # NOTE: This is the best available cardinality that we have for syncing with Github
    github_database_id: Mapped[int] = sa.Column(
        sa.Integer,
        nullable=False,
    )
    account_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )
    # NOTE: Deleting a parent team will also delete its descendants
    parent_id: Mapped[int | None] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_team.id", ondelete="CASCADE"),
        nullable=True,
    )
    slack_channel_id: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
    )
    enable_flexreview_team: Mapped[bool] = sa.Column(
        sa.Boolean,
        nullable=False,
        server_default=sa.false(),
        doc="""
        Enable FlexReview Team for this team.

        * When a PR modifies a file that is owned by this team, a sticky comment is
          posted.
        * If review assignment method is configured and a auto-assignment is enabled,
          assign a reviewer user.
        * If an automation is configured, trigger the automation.
        """,
    )
    enable_flexreview_team_auto_assignment: Mapped[bool] = sa.Column(
        sa.Boolean,
        nullable=False,
        server_default=sa.true(),
        doc="""
        Enable reviewer auto-assignment for this team.

        If this is enabled, a reviewer is automatically assigned to a PR based on the
        configured review assignment method.

        If this is disabled, a reviewer is not automatically assigned, but a slash
        command will be available to manually assign a reviewer.
        """,
    )
    team_size: Mapped[int] = sa.Column(
        sa.Integer,
        nullable=False,
        server_default="0",
        doc="The number of members in the team (including members of subteams).",
    )
    flexreview_reviewer_inclusion_exclusion_list: Mapped[list[str]] = sa.Column(
        ARRAY(sa.Text, dimensions=1),
        nullable=False,
        server_default="{}",
        doc="""
        A list of GitHub user / team names for the reviewer inclusions and exclusions.

        When we assign a reviewer from this team, we will take members of this team.
        Then, use this list to include and exclude users from first to last. Exclusion
        should be specified with a leading `!`.

        For example, if this list is
        ["@acme-corp/ios-sre-team", "!@acme-corp/eng-managers"], we will add the members
        of the SRE team to the reviewer candidates, and then removes the eng managers.
        """,
    )

    account: Mapped[Account] = relationship(
        "Account",
        foreign_keys=account_id,
        uselist=False,
    )
    parent_team: Mapped[GithubTeam | None] = relationship(
        "GithubTeam",
        foreign_keys=parent_id,
        remote_side="GithubTeam.id",
        uselist=False,
    )
    children_teams: Mapped[list[GithubTeam]] = relationship(
        "GithubTeam",
        back_populates="parent_team",
        uselist=True,
    )

    members: Mapped[list[GithubUser]] = relationship(
        "GithubUser",
        secondary=GithubTeamMembers.__tablename__,
        back_populates="teams",
        uselist=True,
    )

    tasks: Mapped[list[TechDebtTask]] = relationship(
        "TechDebtTask",
        back_populates="github_team",
        uselist=True,
    )

    ownership_list: Mapped[list[GithubTeamOwnership]] = relationship(
        "GithubTeamOwnership",
        uselist=True,
    )

    @property
    def full_name(self) -> str:
        return f"{self.organization}/{self.slug}"

    @property
    def markdown_quoted_name(self) -> str:
        return f"`{self.full_name}`"

    @property
    def codeowners_name(self) -> str:
        """Return a str that is used in CODEOWNERS file."""
        return f"@{self.organization}/{self.slug}"

    @functools.cached_property
    def members_recursively(self) -> list[GithubUser]:
        subteams_cte = (
            sa.select(GithubTeam)
            .where(GithubTeam.id == self.id)
            .cte("subteams", recursive=True)
        )
        subteams_cte = subteams_cte.union_all(
            sa.select(GithubTeam).join(
                subteams_cte, GithubTeam.parent_id == subteams_cte.c.id
            )
        )
        q = (
            sa.select(GithubUser)
            .join(
                GithubTeamMembers,
                GithubTeamMembers.github_user_id == GithubUser.id,
            )
            .where(GithubTeamMembers.github_team_id.in_(sa.select(subteams_cte.c.id)))
            .distinct()
        )
        return db.session.scalars(q).all()

    @functools.cached_property
    def flexreview_reviewer_inclusion_exclusion_list_parsed(
        self,
    ) -> list[GithubTeamFlexReviewInclusionExclusionListEntry]:
        ret: list[GithubTeamFlexReviewInclusionExclusionListEntry] = []
        for entry in self.flexreview_reviewer_inclusion_exclusion_list:
            negate, user, team = _parse_user_or_team_entry(self.account_id, entry)
            ret.append(
                GithubTeamFlexReviewInclusionExclusionListEntry(
                    negate=negate,
                    user=user,
                    team=team,
                ),
            )
        return ret

    @property
    def current_slo_config_history(self) -> GithubTeamSloConfigHistory | None:
        ret: GithubTeamSloConfigHistory | None = (
            GithubTeamSloConfigHistory.query.filter(
                GithubTeamSloConfigHistory.github_team_id == self.id
            )
            .order_by(GithubTeamSloConfigHistory.created.desc())
            .first()
        )
        return ret

    @property
    def current_slo_config(self) -> SloConfig:
        history = self.current_slo_config_history
        if history is None:
            return SloConfig()
        return history.config_data

    @property
    def current_resolved_slo_config(self) -> SloConfig:
        config = self.current_slo_config
        if config.is_complete:
            return config
        if self.parent_team is None:
            return config.merge(self.account.current_slo_config)
        return config.merge(self.parent_team.current_resolved_slo_config)

    @property
    def current_flexreview_config_history(
        self,
    ) -> GithubTeamFlexReviewConfigHistory | None:
        ret: GithubTeamFlexReviewConfigHistory | None = (
            GithubTeamFlexReviewConfigHistory.query.filter(
                GithubTeamFlexReviewConfigHistory.github_team_id == self.id
            )
            .order_by(GithubTeamFlexReviewConfigHistory.created.desc())
            .first()
        )
        return ret

    @property
    def current_flexreview_config(self) -> FlexReviewTeamConfig:
        history = self.current_flexreview_config_history
        if history is None:
            return FlexReviewTeamConfig()
        return history.config_data

    def bind_contextvars(self) -> None:
        structlog.contextvars.bind_contextvars(**self.__contextvars())

    @contextlib.contextmanager
    def bound_contextvars(self) -> Generator[None]:
        with structlog.contextvars.bound_contextvars(**self.__contextvars()):
            yield

    def __contextvars(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "github_team_id": self.id,
        }


class GithubTeamFlexReviewConfigHistory(BaseModel):
    __tablename__ = "github_team_flexreview_config_history"

    github_team_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_team.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
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
    config_data: Mapped[FlexReviewTeamConfig] = sa.Column(
        dbutil.PydanticType(FlexReviewTeamConfig, exclude_none=False),
        nullable=False,
    )


class GithubTeamSloConfigHistory(BaseModel):
    __tablename__ = "github_team_slo_config_history"

    github_team_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_team.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
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
    config_data: Mapped[SloConfig] = sa.Column(
        dbutil.PydanticType(SloConfig, exclude_none=False),
        nullable=False,
    )


class TriggerType(enum.Enum):
    ON_NEW_REVIEWER = "ON_NEW_REVIEWER"
    AFTER_INACTIVITY = "AFTER_INACTIVITY"


class ActionType(enum.Enum):
    SEND_SLACK_CHANNEL_MESSAGE = "SEND_SLACK_CHANNEL_MESSAGE"
    SEND_SLACK_DIRECT_MESSAGE_TO_REVIEWER = "SEND_SLACK_DIRECT_MESSAGE_TO_REVIEWER"
    REASSIGN_REVIEWER = "REASSIGN_REVIEWER"


class TeamAutomationRule(BaseModel):
    __tablename__ = "team_automation_rule"
    github_team_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_team.id", ondelete="CASCADE"),
        nullable=False,
        doc="""The team that defines this automation rule.""",
    )

    inheritable: Mapped[bool] = sa.Column(
        sa.Boolean,
        nullable=False,
        server_default=sa.false(),
        doc="""If true, this rule will be inherited by child teams.""",
    )
    trigger_type: Mapped[TriggerType] = sa.Column(
        dbutil.TextEnum(TriggerType),
        nullable=False,
    )
    inactive_duration: Mapped[datetime.timedelta | None] = sa.Column(
        sa.Interval,
        nullable=True,
    )

    action_type: Mapped[ActionType] = sa.Column(
        dbutil.TextEnum(ActionType),
        nullable=False,
    )
    notification_slack_channel_id: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
        doc="Only used when the action type is SEND_SLACK_CHANNEL_MESSAGE.",
    )

    github_team: Mapped[GithubTeam] = relationship(
        "GithubTeam",
        uselist=False,
    )


class TeamAutomationRuleMapping(DBModel):
    """Model that maps a team automation rule to teams.

    An automation rule can be inherited by child teams. This mapping represents which
    team is inheriting which rule.
    """

    __tablename__ = "team_automation_rule_mapping"
    __table_args__ = (
        # The reverse index to the primary key.
        sa.Index(
            "idx_team_automation_rule_mapping_ghteam_rule",
            "github_team_id",
            "team_automation_rule_id",
            unique=True,
        ),
    )

    team_automation_rule_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("team_automation_rule.id", ondelete="CASCADE"),
        nullable=False,
        primary_key=True,
    )
    github_team_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_team.id", ondelete="CASCADE"),
        nullable=False,
        primary_key=True,
    )

    team_automation_rule: Mapped[TeamAutomationRule] = relationship(
        "TeamAutomationRule",
        uselist=False,
    )
    github_team: Mapped[GithubTeam] = relationship(
        "GithubTeam",
        uselist=False,
    )


class GithubUserType(str, enum.Enum):
    BOT = "Bot"
    ORGANIZATION = "Organization"
    USER = "User"


class GithubUser(BaseModel):
    __tablename__ = "github_user"
    __table_args__ = (
        # IMPORTANT:
        # Alembic is not able to detect changes to this index so any changes
        # to this index must be manually reflected in a new Alembic migration.
        sa.Index(
            "github_user_username_lower",
            sa.text("LOWER(username)"),
        ),
        sa.Index(
            "github_user_account_id_username",
            "account_id",
            "username",
        ),
        sa.UniqueConstraint(
            "account_id",
            "gh_type",
            "gh_database_id",
            name="github_user_account_type_database_id",
        ),
    )

    account_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )
    gh_type: Mapped[GithubUserType | None] = sa.Column(
        dbutil.TextEnum(GithubUserType),
        nullable=True,
        doc="The type of GitHub user. This is either 'User' or 'Bot'.",
    )
    gh_node_id: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
        doc="The GitHub global node ID of the user. "
        "This is stable even if the user's login (username) changes.",
    )
    gh_database_id: Mapped[int | None] = sa.Column(
        sa.Integer,
        nullable=True,
        doc="The GitHub database ID of the user.",
    )
    username: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
        doc="The GitHub login (username) of the user. "
        "GitHub refers to this as `login` in their API, so we try to use that "
        "terminology wherever possible, though this column was added before "
        "that standard was created.",
    )
    DEPRECATED_profile_name: Mapped[str | None] = sa.Column(
        "profile_name",
        sa.Text,
        nullable=True,
        doc="The user's public profile name. "
        "This is a part of a GitHub user's public profile and is optional."
        "DEPRECATED: We do not set this property to reduce extra API calls",
    )

    avatar_url: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
    )
    # We define `billed` and `active` separately because we need to know
    #   1. How many users we need to charge
    #   2. Which users are allowed to merge their PRs using Aviator
    # All active=True users MUST also have billed=True. This should always
    #   be the case for all users on accounts on the Pro or Enterprise plans.
    #
    # If a user has billed=True but active=False, this means that the
    #  account has exceeded its 15 seats, and only the first 15 users
    #  will have active=True. This can happen if a Pro trial has expired,
    #  or if the account does not have a payment method.
    billed: Mapped[bool] = sa.Column(
        sa.Boolean,
        nullable=False,
        server_default=sa.false(),
        doc="If true, this users PR has been labeled to be merged so we need to bill them.",
    )
    active: Mapped[bool] = sa.Column(
        sa.Boolean,
        nullable=False,
        server_default=sa.false(),
        doc="If true, we should process the PRs that this user opened.",
    )
    active_collaborator: Mapped[bool] = sa.Column(
        sa.Boolean,
        nullable=False,
        server_default=sa.true(),
        doc=(
            "If false, this user is no longer an active collaborator of the account. "
            "This can be used to label past employees"
        ),
    )
    oauth_token: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
    )
    oauth_refresh_token: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
    )
    slack_username: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
    )
    slack_user_id: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
        doc="""
        Historical Slack user ID.

        When we initially introduced a Slack integration, we made a connection between
        GithubUser and Slack user ID directly. Later we introduced a separate table and
        have User, GithubUser, and SlackUser associated based on the User.id.

        This field is kept for historical reasons and will not be filled for new users.
        They will have self.aviator_user.slack_user.external_user_id instead.

        See slackhook/notify.py code as well. It prefers the new association if
        available, but falls back to this column to support old users.
        """,
    )

    account: Mapped[Account] = relationship(
        "Account",
        uselist=False,
    )
    aviator_user: Mapped[User | None] = relationship(
        "User",
        back_populates="github_user",
        uselist=False,
    )
    slack_user: Mapped[SlackUser | None] = relationship(
        "SlackUser",
        back_populates="github_user",
        uselist=False,
    )
    teams: Mapped[list[GithubTeam]] = relationship(
        "GithubTeam",
        secondary=GithubTeamMembers.__tablename__,
        back_populates="members",
        uselist=True,
    )

    ownership_list: Mapped[list[GithubTeamOwnership]] = relationship(
        "GithubTeamOwnership",
        uselist=True,
    )

    out_of_office_entries: Mapped[list[OutOfOfficeEntry]] = relationship(
        "OutOfOfficeEntry",
        back_populates="github_user",
        uselist=True,
    )

    def __repr__(self) -> str:
        return f"<GithubUser {self.id} {self.username} {self.gh_node_id}>"

    @property
    def codeowners_name(self) -> str:
        """Return a str that is used in CODEOWNERS file."""
        return f"@{self.username}"

    @property
    def url(self) -> str:
        base_url: str = app.config["GITHUB_BASE_URL"]
        return f"{base_url}/{self.username}"

    @property
    def qualified_avatar(self) -> str:
        # Disable avatars for some on-prem installations.
        # See comment above this setting in config/base.py.
        if app.config["DISABLE_GITHUB_AVATARS"]:
            return ""
        return self.gh_avatar

    @property
    def gh_avatar(self) -> str:
        # We don't actively update user's Avatar URLs in the database right now
        # (after the first time we see their user info) but this works to fetch
        # their latest avatar.
        base_url: str = app.config["GITHUB_BASE_URL"]
        # Removing the [bot] suffix because the Avatar URL doesn't seem to work
        # with it. For example, https://github.com/dependabot.png works, but
        # https://github.com/dependabot[bot].png won't.
        username_without_bot = self.username.replace("[bot]", "")
        full_url = base_url + "/" + username_without_bot + ".png"
        return full_url

    @functools.cached_property
    def teams_recursively(self) -> list[GithubTeam]:
        """Get all teams that a user belongs to, including parents."""
        team_table1 = sa.orm.aliased(GithubTeam)
        team_table2 = sa.orm.aliased(GithubTeam)
        # Let's say we have "employee" team and "eng" team. "eng" is a child of
        # "employee", and we are going to get teams that a member of "eng" is in. Both
        # "employee" and "eng" should be included.
        #
        # In the top-level, we query the user's direct membership, which is "eng".
        top_level = (
            sa.select(team_table1.id, team_table1.parent_id)
            .join(GithubTeamMembers, GithubTeamMembers.github_team_id == team_table1.id)
            .where(GithubTeamMembers.github_user_id == self.id)
            .cte("top_level", recursive=True)
        )
        # In the second-level, we query the teams that are parents of "eng".
        sub_level = sa.select(team_table2.id, team_table2.parent_id).join(
            top_level, team_table2.id == top_level.c.parent_id
        )
        # With the recursive CTE, we get recursively run the top_level and sub_level
        # queries.
        query = sa.select(GithubTeam).where(
            GithubTeam.id.in_(sa.select(top_level.union(sub_level).c.id))
        )
        return db.session.execute(query).scalars().all()

    @property
    def is_out_of_office(self) -> bool:
        result = db.session.scalar(
            sa.select(OutOfOfficeEntry).where(
                OutOfOfficeEntry.github_user_id == self.id,
                OutOfOfficeEntry.start_date <= sa.func.now(),
                OutOfOfficeEntry.end_date >= sa.func.now(),
            ),
        )
        return result is not None

    def to_slack_rich_text_element(
        self,
        *,
        at_mention: bool = True,
    ) -> slack_blocks.RichTextElement:
        """Return a dict that is used in Slack rich text message.

        Depending on the user's Slack user association, it returns an at-mention or a
        plain text username.
        """
        out: slack_blocks.RichTextElement
        if at_mention and self.aviator_user and self.aviator_user.slack_user:
            out = slack_blocks.RichTextElementParts.User(
                user_id=self.aviator_user.slack_user.external_user_id
            )
        else:
            out = slack_blocks.RichTextElementParts.Text(
                text=f"@{self.username}",
                style=slack_blocks.RichTextElementParts.TextStyle(code=True),
            )
        return out


class SlackUser(BaseModel):
    __tablename__ = "slack_user"
    username: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )
    external_user_id: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )
    team_id: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
        doc=(
            "This is associated with the user's workspace. For our purposes,"
            "each Slack User should only have one team id."
        ),
    )
    github_user_id: Mapped[int | None] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_user.id"),
        nullable=True,
        unique=True,
    )

    github_user: Mapped[GithubUser | None] = relationship(
        "GithubUser",
        back_populates="slack_user",
    )


class RepoCollaborator(BaseModel):
    __tablename__ = "repo_collaborator"
    repo_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_repo.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_user.id", ondelete="CASCADE"),
        nullable=False,
    )

    repo: Mapped[GithubRepo] = relationship(
        "GithubRepo",
        uselist=False,
    )
    user: Mapped[GithubUser] = relationship(
        "GithubUser",
        uselist=False,
        lazy="joined",
    )


class BaseBranch(BaseModel):
    __tablename__ = "base_branch"
    __table_args__ = (
        sa.Index("idx_base_branch_repo_id_deleted", "repo_id", "deleted", unique=False),
    )
    repo_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_repo.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )
    paused: Mapped[bool] = sa.Column(
        sa.Boolean,
        nullable=False,
        server_default=sa.false(),
    )
    paused_message: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
    )

    repo: Mapped[GithubRepo] = relationship(
        "GithubRepo",
        uselist=False,
    )


class ActivityType(str, enum.Enum):
    """
    The type of an Activity.

    This class is a subclass of ``str`` in order to tell Pydantic that we can
    coalesce the value from a string into the actual enum value when using a
    ``Literal`` type (which is useful for the discriminated union thing that we
    do below). The doctest below tests this.

    >>> import typing_extensions as TE
    >>> import pydantic
    >>> assert (
    ...     pydantic.TypeAdapter(TE.Literal[ActivityType.OPENED]).validate_python("opened")
    ...     is ActivityType.OPENED
    ... )
    """

    OPENED = "opened"
    LABELED = "labeled"
    UNLABELED = "unlabeled"
    APPROVED = "approved"
    QUEUED = "queued"
    BOT_PR_CREATED = "bot_pr_created"  # Cascading effect of internal "tagged" event
    TOP = "top"
    MERGED = "merged"
    BLOCKED = "blocked"
    PAUSED = "paused"
    UNPAUSED = "unpaused"
    DELAYED = "delayed"
    STUCK = "stuck"
    CHANGE_SET = "change_set"
    RESET = "reset"
    RESYNC = "resync"
    UNDO_BISECTION = "undo_bisection"

    #: The HEAD commit of the pull request was updated.
    #: This might be a push of a single or multiple commits, or a force push.
    SYNCHRONIZE = "synchronize"

    # These haven't been created since 2022-04-20, should we delete them?
    # I think they were created accidentally by calling post_pull_comment.
    PENDING_MERGEABILITY = "pending_mergeability"
    PENDING_APPROVAL = "pending_approval"
    CHANGES_REQUESTED = "changes_requested"
    PENDING_CONV = "pending_conv"
    PENDING_OWNER = "pending_owner"


class ActivityLabeledPayload(schema.BaseModel):
    type: TE.Literal[ActivityType.LABELED] = ActivityType.LABELED
    label: str | None = None


class ActivityApprovedPayload(schema.BaseModel):
    type: TE.Literal[ActivityType.APPROVED] = ActivityType.APPROVED
    approver_github_login: str = schema.Field(
        alias="approverGitHubLogin",
        description="""
        The GitHub login (aka username) of the user who approved the
        pull request.
        """,
    )


class ActivityMergedPayload(schema.BaseModel):
    type: TE.Literal[ActivityType.MERGED] = ActivityType.MERGED
    optimistic_merge_botpr_id: int | None = schema.Field(
        None,
        alias="optimisticMergeBotPRID",
        description="""
        If specified, this is the database ID of the BotPR that caused
        MergeQueue to optimistically merge the pull request associated with this
        merged activity.
        """,
    )
    emergency_merge: bool | None = schema.Field(
        None,
        alias="emergencyMerge",
        description="If set to True, this merged activity represents an emergency merge.",
    )


class ActivityOpenedPayload(schema.BaseModel):
    type: TE.Literal[ActivityType.OPENED] = ActivityType.OPENED
    head_commit_oid: str = schema.Field(alias="headCommitOID")


class ActivitySynchronizePayload(schema.BaseModel):
    type: TE.Literal[ActivityType.SYNCHRONIZE] = ActivityType.SYNCHRONIZE
    old_head_commit_oid: str = schema.Field(
        alias="oldHeadCommitOID",
        description="""
        The old HEAD commit OID (aka "commit SHA") of the pull request before
        the synchronize event.
        """,
    )
    new_head_commit_oid: str = schema.Field(
        alias="newHeadCommitOID",
        description="""
        The new HEAD commit OID (aka "commit SHA") of the pull request after the
        synchronize event.
        """,
    )
    sender_github_login: str = schema.Field(
        alias="senderGitHubLogin",
        description="""
        The GitHub login (aka username) of the user who triggered the
        synchronize event.
        """,
    )


class ActivityBotPullRequestCreatedPayload(schema.BaseModel):
    type: TE.Literal[ActivityType.BOT_PR_CREATED] = ActivityType.BOT_PR_CREATED
    bot_pr_id: int = schema.Field(
        alias="BotPullRequestID",
        description="The database ID of the bot pull request created.",
    )
    bot_pr_head_oid: str | None = schema.Field(
        None,
        alias="botPullRequestOID",
        description="The HEAD commit OID (aka SHA) of the bot pull request when it's created",
    )


class ActivityResyncPayload(schema.BaseModel):
    type: TE.Literal[ActivityType.RESYNC] = ActivityType.RESYNC
    triggering_pr_id: int | None = schema.Field(
        None,
        alias="TriggeringPullRequestID",
        description="Database ID of the GitHub Pull Request that triggered the resync",
    )
    triggering_bot_pr_id: int | None = schema.Field(
        None,
        alias="TriggeringBotPullRequestID",
        description="Database ID of the BotPR that triggered the resync",
    )
    new_bot_pr_head_commit_oid: str | None = schema.Field(
        None,
        alias="newBotPullRequestHeadCommitOID",
        description="""
        The new HEAD commit OID (aka "commit SHA") of the pull request after the
        resync event.
        """,
    )


ActivityPayload = TE.Annotated[
    Union[
        ActivityApprovedPayload,
        ActivityLabeledPayload,
        ActivityMergedPayload,
        ActivityOpenedPayload,
        ActivitySynchronizePayload,
        ActivityBotPullRequestCreatedPayload,
        ActivityResyncPayload,
    ],
    # This tells Pydantic to choose which model to use based on the `type`
    # field.
    schema.Field(discriminator="type"),
]


class Activity(BaseModel):
    __tablename__ = "activity"
    __table_args__ = (
        sa.Index("idx_activity_repo_id_created", "repo_id", "created", unique=False),
    )
    repo_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_repo.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    pull_request_id: Mapped[int | None] = sa.Column(
        sa.Integer,
        sa.ForeignKey("pull_request.id"),
        nullable=True,
        index=True,
    )
    bot_pr_id: Mapped[int | None] = sa.Column(
        sa.Integer, sa.ForeignKey("bot_pr.id"), nullable=True, index=True
    )
    name: Mapped[ActivityType] = sa.Column(
        dbutil.TextEnum(ActivityType),
        nullable=False,
        doc="""
        The type of the activity. See the ActivityType enum for the possible
        values this may take.
        """,
    )
    status_code: Mapped[StatusCode] = sa.Column(
        dbutil.IntEnum(StatusCode),
        nullable=False,
        server_default=str(StatusCode.UNKNOWN.value),
    )
    reset_pr_count: Mapped[int | None] = sa.Column(
        sa.Integer,
        nullable=True,
        doc="""Number of PRs affected by the reset""",
    )
    base_branch: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
        doc="Value should be present only if a specific base branch was affected.",
    )

    payload: Mapped[ActivityPayload | None] = sa.Column(
        dbutil.PydanticType(ActivityPayload, exclude_none=True),
        nullable=True,
        doc="""
        An optional JSON payload that contains additional information about the
        activity. The payload is a JSON object that is specific to the activity
        type.
        """,
    )

    repo: Mapped[GithubRepo] = relationship(
        "GithubRepo",
        uselist=False,
    )
    pull_request: Mapped[PullRequest] = relationship(
        "PullRequest",
        uselist=False,
        back_populates="activities",
    )
    bot_pr: Mapped[BotPr] = relationship(
        "BotPr",
        uselist=False,
    )

    @property
    def status_description(self) -> str:
        return StatusCode(self.status_code).message()

    def __repr__(self) -> str:
        return f"<Activity {self.id} (repo_id={self.repo_id}, name={self.name})>"


class ReleaseProject(BaseModel):
    """ReleaseProject represents a series of releases of a product."""

    __tablename__ = "release_project"
    __table_args__ = (
        sa.Index(
            "idx_release_project_account_id_name", "account_id", "name", unique=True
        ),
    )

    account_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
        doc="The name of the release project (e.g. 'av-cli').",
    )
    release_git_tag_patterns: Mapped[list[str]] = sa.Column(
        ARRAY(sa.Text, dimensions=1),
        nullable=False,
        server_default="{}",
        doc="""
        Git tag patterns to match for detecting releases.

        A pattern can contain '%d' to match digits.
        """,
    )
    config: Mapped[ReleaseProjectConfig] = sa.Column(
        dbutil.PydanticType(ReleaseProjectConfig, exclude_none=False),
        nullable=False,
        doc=("JSON-encoded metadata for this release project."),
    )
    enable_scheduled_release_cut: Mapped[bool] = sa.Column(
        sa.Boolean,
        nullable=False,
        server_default=sa.false(),
        doc="Enable the scheduled release cut for this release project.",
    )
    scheduled_release_cut_cron: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
        doc="Cron expression for scheduled release cuts.",
    )
    scheduled_release_cut_last_execution: Mapped[datetime.datetime | None] = sa.Column(
        sa.TIMESTAMP(timezone=True),
        nullable=True,
        doc="Last time the scheduled release cut was executed.",
    )
    default_automatic_deployment_after_build_env_id: Mapped[int | None] = sa.Column(
        sa.Integer,
        sa.ForeignKey("release_environment.id"),
        nullable=True,
        doc="Default environment ID to deploy automatically after build",
    )

    account: Mapped[Account] = relationship(
        "Account",
        uselist=False,
    )
    repos: Mapped[list[GithubRepo]] = relationship(
        "GithubRepo",
        secondary="release_project_repo_config",
        uselist=True,
        back_populates="release_projects",
    )
    environments: Mapped[list[ReleaseEnvironment]] = relationship(
        "ReleaseEnvironment",
        uselist=True,
        back_populates="release_project",
        foreign_keys="ReleaseEnvironment.release_project_id",
    )
    default_automatic_deployment_after_build_environment: Mapped[
        ReleaseEnvironment | None
    ] = relationship(
        "ReleaseEnvironment",
        uselist=False,
        foreign_keys=[default_automatic_deployment_after_build_env_id],
    )

    @property
    def rbac_entity(self) -> str:
        return f"release_project:{self.name}"

    def bind_contextvars(self) -> None:
        structlog.contextvars.bind_contextvars(**self.__contextvars())

    @contextlib.contextmanager
    def bound_contextvars(self) -> Generator[None]:
        with structlog.contextvars.bound_contextvars(**self.__contextvars()):
            yield

    def __contextvars(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "release_project_id": self.id,
        }


class BuildMethod(enum.Enum):
    NOT_CONFIGURED = "not_configured"
    GITHUB_ACTION = "github_action"
    BUILDKITE = "buildkite"
    WEBHOOK = "webhook"


class ReleaseProjectRepoConfig(BaseModel):
    """Repository configuration for a ReleaseProject."""

    __tablename__ = "release_project_repo_config"
    __table_args__ = (
        sa.Index(
            "idx_release_project_repo_config_rp_id_repo_id",
            "release_project_id",
            "repo_id",
            unique=True,
        ),
    )

    release_project_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("release_project.id", ondelete="CASCADE"),
        nullable=False,
    )
    repo_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_repo.id", ondelete="CASCADE"),
        nullable=False,
    )
    pull_request_file_patterns: Mapped[list[str]] = sa.Column(
        ARRAY(sa.Text, dimensions=1),
        nullable=False,
        server_default="{}",
        doc="""
        File patterns to match to detect relevant pull requests.

        Use the same pattern matching syntax as GitHub actions.

        https://docs.github.com/en/actions/using-workflows/workflow-syntax-for-github-actions#patterns-to-match-file-paths
        """,
    )

    build_image_method: Mapped[BuildMethod] = sa.Column(
        dbutil.TextEnum(BuildMethod),
        nullable=False,
        server_default=BuildMethod.NOT_CONFIGURED.value,
        doc="""
        Configures how the image should be built.
        """,
    )

    build_image_github_workflow_id: Mapped[int | None] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_workflow.id"),
        nullable=True,
        doc="""
        Database ID of the Github workflow for building the release image.
        """,
    )
    buildkite_organization: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
        doc="Organization slug in Buildkite.",
    )
    buildkite_pipeline: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
        doc="Pipeline slug in Buildkite.",
    )

    webhook_url: Mapped[str | None] = sa.Column(
        sa.Text, nullable=True, doc="Webhook url when using webhook build method"
    )

    custom_workflow_params_template: Mapped[dict] = sa.Column(
        dbutil.JSONDictType(),
        nullable=False,
        server_default=sa.text("'{}'::jsonb"),
        doc="Template for custom workflow variables in JSON format.",
    )


class Release(IDModel):
    """Release is a named point in the repos belongs to a ReleaseProject."""

    __tablename__ = "release"
    __table_args__ = (
        sa.Index(
            "idx_release_rp_id_version", "release_project_id", "version", unique=True
        ),
    )

    release_project_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("release_project.id"),
        nullable=False,
    )
    version: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
        doc="The version name of the release (e.g. 'v1.0.0').",
    )
    created: Mapped[datetime.datetime] = sa.Column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    )
    git_tag_name: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
        doc="The git tag for the release.",
    )
    docker_tag: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
        doc="The docker tag for the release.",
    )
    active_release_candidate_version: Mapped[int] = sa.Column(
        sa.Integer,
        nullable=False,
        doc="Version of the active release candidate.",
        server_default="1",
    )
    head_branch_name: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
        doc="""
        Head branch name if this release is cut from a non-default branch.

        If the release project contains multiple repos, it is required that this branch
        exists in all repos.

        If this is None, release is cut from the default branches.
        """,
    )
    automatic_deployment_after_build_env_id: Mapped[int | None] = sa.Column(
        sa.Integer,
        sa.ForeignKey("release_environment.id"),
        nullable=True,
        doc="Environment ID to deploy automatically after build",
    )

    release_cuts: Mapped[list[ReleaseCut]] = relationship(
        "ReleaseCut",
        uselist=True,
        back_populates="release",
    )
    release_project: Mapped[ReleaseProject] = relationship(
        "ReleaseProject",
        uselist=False,
    )
    automatic_deployment_after_build_environment: Mapped[ReleaseEnvironment | None] = (
        relationship(
            "ReleaseEnvironment",
            uselist=False,
            foreign_keys=[automatic_deployment_after_build_env_id],
        )
    )

    def as_json(self) -> dict:
        return dict(
            release_project_id=self.release_project_id,
            version=self.version,
            git_tag_name=self.git_tag_name,
            docker_tag=self.docker_tag,
        )

    def bind_contextvars(self) -> None:
        structlog.contextvars.bind_contextvars(**self.__contextvars())

    @contextlib.contextmanager
    def bound_contextvars(self) -> Generator[None]:
        with structlog.contextvars.bound_contextvars(**self.__contextvars()):
            yield

    def __contextvars(self) -> dict[str, object]:
        return {
            "release_id": self.id,
        }


class ReleasePipeline(DBModel):
    """Release Pipeline is the workflow of how a release version gets deployed"""

    __tablename__ = "release_pipeline"

    release_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("release.id"),
        nullable=False,
        primary_key=True,
    )
    release_candidate_number: Mapped[int] = sa.Column(
        sa.Integer,
        nullable=False,
        primary_key=True,
    )
    conditions: Mapped[dict] = sa.Column(
        dbutil.JSONDictType(),
        nullable=False,
        doc="Current conditions (state) of the pipeline",
    )


class PreviousReleaseAssociation(DBModel):
    """Parent-child relationship on releases."""

    __tablename__ = "previous_release"

    release_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("release.id"),
        nullable=False,
        primary_key=True,
    )
    previous_release_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("release.id"),
        nullable=False,
        primary_key=True,
    )


class ReleaseCutBuildStatus(enum.Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELED = "canceled"
    ERROR = "error"
    UNKNOWN = "unknown"

    @classmethod
    def from_buildkite_state(cls, state: str) -> ReleaseCutBuildStatus:
        # https://buildkite.com/docs/apis/graphql/schemas/enum/buildstates
        if state == "blocked":
            return cls.PENDING
        if state == "canceled":
            return cls.CANCELED
        if state == "canceling":
            return cls.CANCELED
        if state == "creating":
            return cls.PENDING
        if state == "failed":
            return cls.FAILURE
        if state == "failing":
            return cls.FAILURE
        if state == "not_run":
            return cls.PENDING
        if state == "passed":
            return cls.SUCCESS
        if state == "running":
            return cls.IN_PROGRESS
        if state == "scheduled":
            return cls.IN_PROGRESS
        return cls.UNKNOWN

    def is_terminal(self) -> bool:
        return self in {
            ReleaseCutBuildStatus.SUCCESS,
            ReleaseCutBuildStatus.FAILURE,
            ReleaseCutBuildStatus.CANCELED,
            ReleaseCutBuildStatus.ERROR,
        }


class ReleaseCut(BaseModel):
    """ReleaseCut represents a point in a Git repository.

    A Release can span multiple repositories. For this reason, we have 1:N mappings with
    a Release and ReleaseCuts.
    """

    __tablename__ = "release_cut"
    __table_args__ = (
        sa.Index(
            "idx_release_cut_release_id_repo_id",
            "release_id",
            "repo_id",
            "release_candidate_version",
            unique=True,
        ),
        sa.Index(
            "idx_release_cut_buildkite_build_id",
            "buildkite_build_id",
        ),
    )

    release_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("release.id"),
        nullable=False,
    )
    repo_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_repo.id"),
        nullable=False,
    )
    commit_hash: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
        doc="The commit hash of the release cut.",
    )
    tag_hash: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
        doc="Annotated tag hash of the release cut if it's a tag.",
    )
    default_branch_commit_hashes: Mapped[list[str]] = sa.Column(
        ARRAY(sa.Text, dimensions=1),
        nullable=False,
        server_default="{}",
        doc="""The nearest commit hashes that are on the default branch.

        This will be the same as commit_hash if the release cut is on the default
        branch. This will be different if the release cut is on a commit that is not on
        the default branch.
        """,
    )
    need_commit_graph_calculation: Mapped[bool] = sa.Column(
        sa.Boolean,
        nullable=False,
        server_default=sa.false(),
        doc="""If true, this ReleaseCut requires a commit graph traversal.

        When this is true, we fetch git commits from the repository and calculate the
        pull requests that this release includes. This means that
        ReleaseIncludedPullRequest and PreviousReleaseAssociation will be deleted and
        recreated, and default_branch_commit_hashes are recalculated.
        """,
    )
    github_action_workflow_run_id: Mapped[int | None] = sa.Column(
        sa.BigInteger,
        nullable=True,
        doc="ID of the corresponding GitHub Action workflow run.",
    )
    buildkite_org_slug: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
        doc="Organization slug of the corresponding Buildkite build.",
    )
    buildkite_pipeline_slug: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
        doc="Pipeline slug of the corresponding Buildkite build.",
    )
    buildkite_build_number: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
        doc="Build number of the corresponding Buildkite build.",
    )
    buildkite_build_id: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
        doc="ID of the corresponding Buildkite build.",
    )
    release_candidate_version: Mapped[int] = sa.Column(
        sa.Integer,
        nullable=False,
        doc="Version as a Release Candidate.",
        server_default="1",
    )

    build_status: Mapped[ReleaseCutBuildStatus] = sa.Column(
        dbutil.TextEnum(ReleaseCutBuildStatus),
        nullable=False,
        server_default=ReleaseCutBuildStatus.UNKNOWN.value,
        doc="The status of the deployment.",
    )

    error_message: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
        doc="Store error message to be exposed to user in UI.",
    )

    release: Mapped[Release] = relationship(
        "Release",
        uselist=False,
        back_populates="release_cuts",
    )
    github_repo: Mapped[GithubRepo] = relationship(
        "GithubRepo",
        uselist=False,
    )

    def release_included_cherry_picks(self) -> list[ReleaseIncludedPullRequest]:
        return db.session.scalars(
            sa.select(ReleaseIncludedPullRequest)
            .where(
                ReleaseIncludedPullRequest.release_cut_id == self.id,
                ReleaseIncludedPullRequest.is_cherry_pick == True,
            )
            .order_by(ReleaseIncludedPullRequest.cherry_pick_order)
        ).all()

    def bind_contextvars(self) -> None:
        structlog.contextvars.bind_contextvars(**self.__contextvars())

    @contextlib.contextmanager
    def bound_contextvars(self) -> Generator[None]:
        with structlog.contextvars.bound_contextvars(**self.__contextvars()):
            yield

    def __contextvars(self) -> dict[str, object]:
        return {
            "release_cut_id": self.id,
        }


class ReleaseProjectNonReleasedPullRequest(DBModel):
    """NxM relationship between ReleaseProject and non-released PullRequest.

    This entry is created when a PR is merged and kept until a PR is included to
    a Release as a baseline (i.e. not as a cherry-pick).

    This entry is created irrelevant to the PR touches the files that matches with
    the ReleaseProject's file path config. If it matches, match_file_path becomes
    True.
    """

    __tablename__ = "release_project_non_released_pull_request"

    release_project_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("release_project.id"),
        nullable=False,
        primary_key=True,
    )
    pull_request_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("pull_request.id"),
        nullable=False,
        primary_key=True,
    )
    match_file_path: Mapped[bool] = sa.Column(
        sa.Boolean,
        nullable=False,
        doc="Whether the pull request matches the file path patterns of the ReleaseProject.",
    )

    pull_request: Mapped[PullRequest] = relationship(
        "PullRequest",
        uselist=False,
    )


class CherryPickStatus(enum.Enum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    NEEDS_SYNC = "needs_sync"  # For branch-based release only


class ReleaseIncludedPullRequest(DBModel):
    """NxM PullRequest relationship with Releease."""

    __tablename__ = "release_included_pull_request"

    release_cut_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("release_cut.id"),
        nullable=False,
        primary_key=True,
    )
    pull_request_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("pull_request.id"),
        nullable=False,
        primary_key=True,
    )
    is_cherry_pick: Mapped[bool] = sa.Column(
        sa.Boolean,
        nullable=False,
        doc="If true, this pull request was cherry-picked into the release.",
    )
    cherry_pick_order: Mapped[int | None] = sa.Column(
        sa.Integer,
        nullable=True,
        doc="The order of the cherry-picked pull request",
    )
    cherry_pick_commit_sha: Mapped[str | None] = sa.Column(
        sa.Text, nullable=True, doc="Commit SHA of the cherry-picked pull request."
    )
    cherry_pick_status: Mapped[CherryPickStatus | None] = sa.Column(
        dbutil.TextEnum(CherryPickStatus),
        nullable=True,
    )
    cherry_pick_resolution_pull_request_id: Mapped[int | None] = sa.Column(
        sa.Integer,
        sa.ForeignKey("pull_request.id"),
        nullable=True,
        doc="""The pull request that resolves the cherry-pick conflict.

        When there's a cherry-pick conflict, we create a new pull request with a
        conflicting content and ask a user to resolve the conflict. This points to that
        pull request. The pull_request_id is the original PR that is cherry-picked.
        """,
    )
    match_file_path: Mapped[bool] = sa.Column(
        sa.Boolean,
        nullable=False,
        # This is added later. For the existing records, we set this to True.
        server_default=sa.true(),
        doc="Whether the pull request matches the file path patterns of the ReleaseProject.",
    )

    pull_request: Mapped[PullRequest] = relationship(
        "PullRequest",
        uselist=False,
        foreign_keys=[pull_request_id],
    )
    cherry_pick_resolution_pull_request: Mapped[PullRequest | None] = relationship(
        "PullRequest",
        uselist=False,
        foreign_keys=[cherry_pick_resolution_pull_request_id],
    )


class DeploymentMethod(enum.Enum):
    NOT_CONFIGURED = "not_configured"
    GITHUB_ACTION = "github_action"
    BUILDKITE = "buildkite"
    WEBHOOK = "webhook"


class ReleaseEnvironment(BaseModel):
    """ReleaseEnvironment represents an environment where a release is deployed."""

    __tablename__ = "release_environment"
    __table_args__ = (
        sa.Index(
            "idx_release_environment_rp_id_name",
            "release_project_id",
            "name",
            unique=True,
        ),
    )

    release_project_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("release_project.id"),
        nullable=False,
    )
    name: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
        doc="The name of the environment (e.g. 'staging').",
    )
    tier: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
        server_default="development",
    )
    require_verification: Mapped[bool] = sa.Column(
        sa.Boolean,
        nullable=False,
        server_default="false",
    )
    deployment_method: Mapped[DeploymentMethod] = sa.Column(
        dbutil.TextEnum(DeploymentMethod),
        nullable=False,
    )
    config: Mapped[ReleaseEnvironmentConfig] = sa.Column(
        dbutil.PydanticType(ReleaseEnvironmentConfig, exclude_none=False),
        nullable=False,
        doc=(
            "JSON-encoded metadata for this environment. "
            "Contains info like github_workflow, etc."
        ),
    )
    rank: Mapped[int] = sa.Column(
        sa.Integer,
        nullable=False,
        server_default="0",
        doc="Rank of the environment within the release project",
    )
    parent_release_environment_id: Mapped[int | None] = sa.Column(
        sa.Integer,
        sa.ForeignKey("release_environment.id"),
        nullable=True,
        doc="ID of the parent environment.",
    )

    parent_release_environment: Mapped[ReleaseEnvironment | None] = relationship(
        "ReleaseEnvironment",
        uselist=False,
        foreign_keys=[parent_release_environment_id],
    )
    release_project: Mapped[ReleaseProject] = relationship(
        "ReleaseProject",
        uselist=False,
        back_populates="environments",
        foreign_keys="ReleaseEnvironment.release_project_id",
    )

    @property
    def rbac_entity(self) -> str:
        return f"{self.release_project.rbac_entity}:{self.name}"

    def bind_contextvars(self) -> None:
        structlog.contextvars.bind_contextvars(**self.__contextvars())

    @contextlib.contextmanager
    def bound_contextvars(self) -> Generator[None]:
        with structlog.contextvars.bound_contextvars(**self.__contextvars()):
            yield

    def __contextvars(self) -> dict[str, object]:
        return {
            "release_environment_id": self.id,
        }


class DeploymentStatus(enum.Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELING = "canceling"
    CANCELED = "canceled"
    ERROR = "error"
    UNKNOWN = "unknown"

    @classmethod
    def from_buildkite_state(cls, state: str) -> DeploymentStatus:
        # https://buildkite.com/docs/apis/graphql/schemas/enum/buildstates
        if state == "blocked":
            return cls.PENDING
        if state == "canceled":
            return cls.CANCELED
        if state == "canceling":
            return cls.CANCELED
        if state == "creating":
            return cls.PENDING
        if state == "failed":
            return cls.FAILURE
        if state == "failing":
            return cls.FAILURE
        if state == "not_run":
            return cls.PENDING
        if state == "passed":
            return cls.SUCCESS
        if state == "running":
            return cls.IN_PROGRESS
        if state == "scheduled":
            return cls.IN_PROGRESS
        return cls.UNKNOWN

    def is_terminal(self) -> bool:
        return self in {
            DeploymentStatus.SUCCESS,
            DeploymentStatus.FAILURE,
            DeploymentStatus.CANCELED,
            DeploymentStatus.ERROR,
        }


class Deployment(BaseModel):
    """Deployment is a record of a release being deployed to an environment."""

    __tablename__ = "deployment"
    __table_args__ = (
        sa.Index(
            "idx_deployment_buildkite_build_id",
            "buildkite_build_id",
        ),
    )

    environment_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("release_environment.id"),
        nullable=False,
    )
    release_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("release.id"),
        nullable=False,
    )
    # TODO: Change this to release_candidate_number since RCV is now
    #  used to represent the full qualified string of release_version + RCN
    release_candidate_version: Mapped[int] = sa.Column(
        "release_candidate_version",
        sa.Integer,
        nullable=False,
        server_default="1",
    )
    from_release_id: Mapped[int | None] = sa.Column(
        sa.Integer,
        sa.ForeignKey("release.id"),
        nullable=True,
        doc="""
        The release that this environment is transitioning from.

        This can be None if the previous release is not known (i.e. the first release to
        the environment).
        """,
    )
    from_release_candidate_version: Mapped[int] = sa.Column(
        sa.Integer,
        nullable=False,
        server_default="1",
        doc="""
        The RC version of the previous release that this environment is transitioning from.

        This value will be ignored if the from_release_id is None.
        """,
    )
    deploy_start_at: Mapped[datetime.datetime] = sa.Column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        doc="The timestamp that the deployment process started at.",
    )
    deploy_end_at: Mapped[datetime.datetime | None] = sa.Column(
        sa.TIMESTAMP(timezone=True),
        nullable=True,
        doc="The timestamp that the deployment process ended at.",
    )
    actor_id: Mapped[int | None] = sa.Column(
        sa.Integer,
        sa.ForeignKey("user_info.id"),
        nullable=True,
        doc="ID of the user who triggered the deployment. If null, deployment might be triggered from elsewhere",
    )
    status: Mapped[DeploymentStatus] = sa.Column(
        dbutil.TextEnum(DeploymentStatus),
        nullable=False,
        server_default=DeploymentStatus.UNKNOWN.value,
        doc="The status of the deployment.",
    )
    github_action_workflow_run_id: Mapped[int | None] = sa.Column(
        sa.BigInteger,
        nullable=True,
        doc="ID of the corresponding Github Action workflow run.",
    )
    # NOTE: Buildkite API, as of 2024-05-20, doesn't have a way to get the build info
    # with UUID in the REST API. It's available only in GraphQL API. However, GraphQL
    # API doesn't support API scope, so it'll be wide-open. We decided to go with REST
    # API to avoid asking for a wide-open scope. The drawback is that we need to
    # remember (org_slug, pipeline_slug, build_number) instead of just UUID.
    buildkite_org_slug: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
        doc="Organization slug of the corresponding Buildkite build.",
    )
    buildkite_pipeline_slug: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
        doc="Pipeline slug of the corresponding Buildkite build.",
    )
    buildkite_build_number: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
        doc="Build number of the corresponding Buildkite build.",
    )
    buildkite_build_id: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
        doc="Build ID of the corresponding Buildkite build.",
    )
    error_message: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
        doc="Store error message to be exposed to user in UI.",
    )

    actor: Mapped[User] = relationship(
        "User",
        uselist=False,
    )
    release: Mapped[Release] = relationship(
        "Release",
        uselist=False,
        foreign_keys=[release_id],
    )
    from_release: Mapped[Release | None] = relationship(
        "Release",
        uselist=False,
        foreign_keys=[from_release_id],
    )
    environment: Mapped[ReleaseEnvironment] = relationship(
        "ReleaseEnvironment",
        uselist=False,
    )

    @property
    def url(self) -> str:
        return f"{app.config['BASE_URL']}/releases/{self.release.release_project.name}/environments/{self.environment.name}/deployments/{self.id}"

    def bind_contextvars(self) -> None:
        structlog.contextvars.bind_contextvars(**self.__contextvars())

    @contextlib.contextmanager
    def bound_contextvars(self) -> Generator[None]:
        with structlog.contextvars.bound_contextvars(**self.__contextvars()):
            yield

    def __contextvars(self) -> dict[str, object]:
        return {
            "deployment_id": self.id,
        }


# Store GitHub workflows: https://docs.github.com/en/rest/actions/workflows?apiVersion=2022-11-28#list-repository-workflows
class GithubWorkflow(BaseModel):
    __tablename__ = "github_workflow"
    __table_args__ = (
        UniqueConstraint(
            "repo_id",
            "path",
            name="github_workflow_repo_id_path",
        ),
    )
    repo_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_repo.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )
    active: Mapped[bool] = sa.Column(
        sa.Boolean,
        nullable=False,
    )
    path: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )
    gh_database_id: Mapped[int | None] = sa.Column(
        sa.BigInteger,
        nullable=True,
    )
    gh_node_id: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
    )


class BuildkitePipeline(BaseModel):
    __tablename__ = "buildkite_pipeline"
    __table_args__ = (
        sa.Index(
            "idx_buildkite_pipeline_account_id_org_slug_pipeline_slug",
            "account_id",
            "org_slug",
            "pipeline_slug",
            unique=True,
        ),
    )
    account_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )
    org_name: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )
    org_slug: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )
    pipeline_name: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )
    pipeline_slug: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )


class AuditLogEntity(enum.Enum):
    MERGE_QUEUE = "merge_queue"
    REPOSITORY = "repository"
    ACCOUNT = "account"
    BILLING = "billing"
    USERS = "user"
    GITHUB_TEAM = "github_team"
    INTEGRATIONS = "integrations"
    FLEXREVIEW = "flexreview"


class AuditLogAction(enum.Enum):
    QUEUE_PAUSED = "queue_paused"
    QUEUE_RESUMED = "queue_resumed"
    QUEUE_ACTIVATED = "queue_activated"
    QUEUE_DEACTIVATED = "queue_deactivated"
    QUEUE_RESET = "queue_reset"
    QUEUE_CONFIG_CHANGED = "queue_config_changed"

    REPO_ADDED = "repo_added"
    REPO_REMOVED = "repo_removed"

    FLEXREVIEW_ENABLED = "repo_flexreview_enabled"
    FLEXREVIEW_DISABLED = "repo_flexreview_disabled"

    ACCOUNT_DOMAIN_WHITELISTED = "account_domain_whitelisted"
    ACCOUNT_DOMAIN_UNWHITELISTED = "account_domain_unwhitelisted"
    ACCOUNT_COMPANY_UPDATED = "account_company_updated"
    ACCOUNT_TIMEZONE_UPDATED = "account_timezone_updated"

    BILLING_PAYMENT_METHOD_UPDATED = "billing_payment_method_updated"
    BILLING_EMAIL_UPDATED = "billing_email_updated"

    USER_INVITED = "user_invited"
    USER_ACCOUNT_ACTIVATED = (
        "user_account_activated"  # accepted invite, or directly created via SSO
    )
    USER_ROLE_CHANGED = "user_role_changed"
    USER_REMOVED = "user_removed"
    USER_PASSWORD_RESET_REQUESTED = "user_password_reset_requested"
    USER_PASSWORD_CHANGED = "user_password_changed"
    USER_EMAIL_CHANGED = "user_email_changed"

    GITHUB_TEAM_FLEXREVIEW_ENABLED = "github_team_flexreview_enabled"
    GITHUB_TEAM_FLEXREVIEW_DISABLED = "github_team_flexreview_disabled"
    GITHUB_TEAM_SLACK_CHANNEL_UPDATED = "github_team_slack_channel_updated"
    GITHUB_TEAM_FLEXREVIEW_REVIEWER_INCLUSION_EXCLUSION_LIST_UPDATED = (
        "github_team_flexreview_reviewer_inclusion_exclusion_list_updated"
    )
    GITHUB_TEAM_FLEXREVIEW_AUTOASSIGNMENT_ENABLED = (
        "github_team_flexreview_autoassignment_enabled"
    )
    GITHUB_TEAM_FLEXREVIEW_AUTOASSIGNMENT_DISABLED = (
        "github_team_flexreview_autoassignment_disabled"
    )
    GITHUB_TEAM_FLEXREVIEW_ASSIGNMENT_METHOD_UPDATED = (
        "github_team_flexreview_assignment_method_updated"
    )
    GITHUB_TEAM_FLEXREVIEW_PAGER_DUTY_POLICY_ID_UPDATED = (
        "github_team_flexreview_pagerduty_policy_id_updated"
    )

    INTEGRATION_WEBHOOK_URL_CHANGED = "integration_webhook_url_changed"
    INTEGRATION_WEBHOOK_ADDED = "integration_webhook_added"
    INTEGRATION_WEBHOOK_REMOVED = "integration_webhook_removed"
    INTEGRATION_API_TOKEN_UPDATED = "integration_api_token_updated"
    INTEGRATION_SLACK_CHANNEL_UPDATED = "integration_slack_channel_updated"
    INTEGRATION_SLACK_DM_ENABLED = "integration_slack_dm_enabled"
    INTEGRATION_SLACK_DM_DISABLED = "integration_slack_dm_disabled"
    INTEGRATION_SLACK_CHANNEL_SUBSCRIPTION_UPDATED = (
        "integration_slack_channel_subscription_updated"
    )
    INTEGRATION_SLACK_DM_SUBSCRIPTION_UPDATED = (
        "integration_slack_dm_subscription_updated"
    )
    INTEGRATION_SLACK_DM_SUBSCRIPTION_DEFAULTS_UPDATED = (
        "integration_slack_dm_subscription_defaults_updated"
    )
    INTEGRATION_SLACK_USER_LINKED = "integration_slack_user_linked"
    INTEGRATION_SLACK_USER_UNLINKED = "integration_slack_user_unlinked"
    INTEGRATION_GITHUB_USER_LINKED = "integration_github_user_linked"
    INTEGRATION_GITHUB_USER_UNLINKED = "integration_github_user_unlinked"

    RBAC_ENABLED = "rbac_enabled"
    RBAC_DISABLED = "rbac_disabled"

    FLEXREVIEW_BREAKGLASS_SLASH_COMMAND = "flexreview_breakglass_slash_command"


class AuditLog(IDModel):
    __table_args__ = (
        sa.Index("idx_audit_log_created_entity", "account_id", "created", "entity"),
        sa.Index(
            "idx_audit_log_created_action_entity",
            "account_id",
            "created",
            "action",
            "entity",
        ),
    )
    created: Mapped[datetime.datetime] = sa.Column(
        sa.TIMESTAMP(timezone=True),
        default=time_util.now,
    )
    entity: Mapped[AuditLogEntity] = sa.Column(
        dbutil.TextEnum(AuditLogEntity),
        nullable=False,
    )
    action: Mapped[AuditLogAction] = sa.Column(
        dbutil.TextEnum(AuditLogAction),
        nullable=False,
    )
    target: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
        doc="The target of the action. For example, the repository name or webhook setting.",
    )
    account_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("account.id"),
        nullable=False,
    )
    user_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("user_info.id"),
        nullable=True,
    )
    github_user_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_user.id"),
        nullable=True,
    )

    user: Mapped[User] = relationship("User", uselist=False)
    github_user: Mapped[GithubUser] = relationship(
        "GithubUser",
        uselist=False,
    )

    @staticmethod
    def capture_user_action(
        user: User,
        action: AuditLogAction,
        entity: AuditLogEntity,
        target: str | None,
        commit_to_db: bool = True,
    ) -> None:
        log = AuditLog(
            account_id=user.account_id,
            user_id=user.id,
            action=action,
            entity=entity,
            target=target,
        )
        db.session.add(log)
        if commit_to_db:
            db.session.commit()

    @staticmethod
    def capture_github_user_action(
        github_user: GithubUser,
        action: AuditLogAction,
        entity: AuditLogEntity,
        target: str | None,
        commit_to_db: bool = True,
    ) -> None:
        log = AuditLog(
            account_id=github_user.account_id,
            github_user_id=github_user.id,
            action=action,
            entity=entity,
            target=target,
        )
        db.session.add(log)
        if commit_to_db:
            db.session.commit()

    def as_json(self) -> dict:
        actor = None

        if self.user:
            actor = self.user.email
        elif self.github_user:
            actor = self.github_user.username

        return {
            "timestamp": self.created.isoformat(),
            "entity": self.entity.value,
            "action": self.action.value,
            "target": self.target,
            "actor": actor,
        }


# TODO[MER-3419]: This seems to be completely unused within the codebase.
class Webhook(BaseModel):
    __tablename__ = "webhook"
    repo_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_repo.id"),
        nullable=False,
    )
    test_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_test.id"),
        nullable=False,
    )
    url: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )
    method: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
        server_default="post",
    )  # get or post
    trigger: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
        server_default="failure",
    )  # failure, success
    max_retry_attempts: Mapped[int] = sa.Column(
        sa.Integer,
        nullable=False,
        server_default="1",
    )
    ci_type: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
        server_default="circleci",
    )  # circleci, buildkite, jenkins
    raw_body: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )
    status: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
        server_default="pending",
    )  # pending, verified, failed

    repo: Mapped[GithubRepo] = relationship(
        "GithubRepo",
        uselist=False,
    )
    headers: Mapped[list[WebhookHeader]] = relationship(
        "WebhookHeader",
        uselist=True,
        back_populates="webhook",
    )
    test: Mapped[GithubTest] = relationship(
        "GithubTest",
        uselist=False,
    )


# TODO[MER-3419]: This seems to be completely unused within the codebase.
class WebhookHeader(BaseModel):
    __tablename__ = "webhook_header"
    webhook_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("webhook.id"),
        nullable=False,
    )
    name: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )
    value: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )

    webhook: Mapped[Webhook] = relationship(
        "Webhook", uselist=False, back_populates="headers"
    )


# TODO[MER-3419]: This seems to be completely unused within the codebase.
class WebhookEvent(BaseModel):
    __tablename__ = "webhook_event"
    webhook_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("webhook.id"),
        nullable=False,
    )
    pull_request_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("pull_request.id"),
        nullable=False,
    )
    commit_sha: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )
    total_attempts: Mapped[int] = sa.Column(
        sa.Integer,
        nullable=False,
        server_default="0",
    )
    last_attempt: Mapped[datetime.datetime] = sa.Column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    )

    __table_args__ = (
        UniqueConstraint(
            "webhook_id",
            "pull_request_id",
            "commit_sha",
            name="pull_request_webhook_event",
        ),
    )


class ConfigType(enum.Enum):
    Main = "main"
    PrequeueHook = "prequeue_hook"
    Unknown = "unknown"


class ConfigHistory(BaseModel):
    __tablename__ = "config_history"
    repo_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_repo.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    config_text: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )
    config_type: Mapped[ConfigType] = sa.Column(
        dbutil.TextEnum(ConfigType),
        nullable=False,
        server_default=ConfigType.Unknown.value,
    )
    # NOTE: does not detect in place mutations of JSONB
    # https://amercader.net/blog/beware-of-json-fields-in-sqlalchemy/
    config_data: Mapped[dict] = sa.Column(
        dbutil.JSONDictType(),
        nullable=False,
    )
    account_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )
    # we leave the below fields nullable because UI/GH changes
    # to the config give us different information on user making change.
    # the following field is set if config change is coming from UI
    aviator_user_id: Mapped[int | None] = sa.Column(
        sa.Integer,
        sa.ForeignKey("user_info.id"),
        nullable=True,
    )
    # the following fields are set if config change coming from GitHub
    github_author_id: Mapped[int | None] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_user.id"),
        nullable=True,
    )
    commit_sha: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
    )
    applied_at: Mapped[datetime.datetime | None] = sa.Column(
        sa.TIMESTAMP(timezone=True),
        nullable=True,
    )
    failed: Mapped[bool] = sa.Column(
        sa.Boolean,
        server_default=sa.false(),
        nullable=False,
        doc="If true, the config failed to apply.",
    )
    failure_reason: Mapped[str] = sa.Column(
        sa.Text,
        server_default="",
        nullable=False,
        doc="The user-visible reason for the failure.",
    )
    failure_dismissed: Mapped[bool] = sa.Column(
        sa.Boolean,
        server_default=sa.false(),
        nullable=False,
        doc="If true, we should not show the failure to the user. User dismissed this failure.",
    )

    aviator_user: Mapped[User | None] = relationship("User", uselist=False)
    github_author: Mapped[GithubUser | None] = relationship("GithubUser", uselist=False)
    repo: Mapped[GithubRepo] = relationship("GithubRepo", uselist=False)

    __table_args__ = (
        # add a constraint to ensure at least one user info is set
        CheckConstraint("NOT(aviator_user_id IS NULL AND github_author_id IS NULL)"),
    )


class RegexConfig(BaseModel):
    __tablename__ = "regex_config"
    id: Mapped[int] = sa.Column(
        sa.Integer,
        primary_key=True,
        nullable=False,
    )
    repo_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_repo.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    pattern: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )
    replace: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )


class QueueTimeStat(BaseModel):
    __tablename__ = "queue_time_stat"
    __table_args__ = (
        sa.Index(
            "queue_time_stat_unique_repo_weekdate", "repo_id", "weekdate", unique=True
        ),
    )

    repo_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_repo.id", ondelete="CASCADE"),
        nullable=False,
    )
    # ISO week date (https://en.wikipedia.org/wiki/ISO_week_date) where the
    # data is coming from.
    # For example, "2023-W12".
    weekdate: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )
    # Median queue time of the merged PRs during the period.
    queue_time_median_seconds: Mapped[Decimal] = sa.Column(
        sa.Float,
        nullable=False,
    )


class FlexReviewCheckStatus(enum.Enum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class FlexReviewCheck(BaseModel):
    __tablename__ = "flex_review_check"
    __table_args__ = (
        sa.Index("flex_review_check_commit_sha_repo_id", "commit_sha", "repo_id"),
    )
    repo_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_repo.id", ondelete="CASCADE"),
        nullable=False,
    )
    commit_sha: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )
    check_run_id: Mapped[int] = sa.Column(
        sa.BigInteger,
        nullable=False,
    )
    # The status of the check run. Can be one of in_progress, or completed.
    status: Mapped[FlexReviewCheckStatus] = sa.Column(
        dbutil.TextEnum(FlexReviewCheckStatus),
        nullable=False,
        doc="The status of the check posted by FlexReview bot.",
    )


class SignalRule(BaseModel):
    __tablename__ = "signal_rule"
    __table_args__ = (
        UniqueConstraint("account_id", "name", name="uq_signal_rule_account_id_name"),
    )

    account_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = sa.Column(
        sa.Text,
        nullable=False,
    )
    file_patterns: Mapped[list[str]] = sa.Column(
        ARRAY(sa.Text, dimensions=1),
        nullable=False,
        server_default="{}",
        doc="""
        File patterns to match to detect a signal. This is required.

        Use the same pattern matching syntax as GitHub actions.

        https://docs.github.com/en/actions/using-workflows/workflow-syntax-for-github-actions#patterns-to-match-file-paths
        """,
    )
    file_content_regexp_pattern: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
        doc="""
        Optional regexp pattern for the file content to match to detect a signal.

        If this is specified, the signal is detected only when the file content matches
        with this regexp in addition to file path matching with file_patterns. If this
        is None, the signal is detected when the file path matches with file_patterns.
        """,
    )

    add_github_label_on_open: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
        doc="""
        Optional GitHub label to add when the signal is detected on open.
        """,
    )
    send_slack_notification_channel_on_open: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
        doc="""
        Optional Slack channel name to send a notification when the signal is detected
        on open.
        """,
    )


class OutOfOfficeSource(enum.Enum):
    AVIATOR_UI = "aviator_ui"
    AVIATOR_API = "aviator_api"
    UNKNOWN = "unknown"


class OutOfOfficeEntry(MutableModel):
    __tablename__ = "out_of_office_dates"

    account_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )

    github_user_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_user.id", ondelete="CASCADE"),
        nullable=False,
    )

    start_date: Mapped[datetime.datetime] = sa.Column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
    )

    end_date: Mapped[datetime.datetime] = sa.Column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
    )

    reason: Mapped[str | None] = sa.Column(
        sa.Text,
        nullable=True,
        doc="An optional reason for the out of office, ex: out sick, vacation, etc.",
    )

    creator_id: Mapped[int | None] = sa.Column(
        sa.Integer,
        sa.ForeignKey("user_info.id"),
        nullable=True,
    )

    source: Mapped[OutOfOfficeSource] = sa.Column(
        dbutil.TextEnum(OutOfOfficeSource),
        nullable=False,
        server_default=OutOfOfficeSource.UNKNOWN.value,
    )

    github_user: Mapped[GithubUser] = relationship(
        "GithubUser",
        back_populates="out_of_office_entries",
        uselist=False,
    )

    creator: Mapped[User | None] = relationship("User", uselist=False)

    @staticmethod
    def get_ooo_gh_users(
        account_id: int,
        date: datetime.datetime | None = None,
    ) -> set[GithubUser]:
        if not date:
            date = datetime.datetime.now(tz=datetime.UTC)

        gh_users: set[GithubUser] = set(
            db.session.scalars(
                sa.select(GithubUser)
                .join(OutOfOfficeEntry)
                .where(
                    OutOfOfficeEntry.account_id == account_id,
                    OutOfOfficeEntry.start_date <= date,
                    OutOfOfficeEntry.end_date >= date,
                ),
            ),
        )

        return gh_users


class BreakglassApproval(MutableModel):
    __tablename__ = "breakglass_approval"

    pull_request_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("pull_request.id"),
        nullable=False,
    )

    github_user_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_user.id"),
        nullable=False,
    )

    reason: Mapped[str] = sa.Column(
        sa.Text,
        server_default="",
        nullable=False,
        doc="The reason for using a breakglass override - will be included in any breakglass notifications.",
    )

    account_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("account.id"),
        nullable=False,
    )

    repo_id: Mapped[int] = sa.Column(
        sa.Integer,
        sa.ForeignKey("github_repo.id"),
        nullable=False,
    )

    pull_request: Mapped[PullRequest] = relationship(
        "PullRequest",
        uselist=False,
    )
    github_user: Mapped[GithubUser] = relationship(
        "GithubUser",
        uselist=False,
    )


def _parse_user_or_team_entry(
    account_id: int, entry: str
) -> tuple[bool, GithubUser | None, GithubTeam | None]:
    """Parse a line like '@username' or '@team/team-name'."""
    negate = False
    if entry.startswith("!"):
        entry = entry.removeprefix("!")
        negate = True

    e = entry.strip().replace("@", "")
    if "/" in e:
        # This is a team name. We need to get the team members.
        org, slug = e.split("/", 2)
        team: GithubTeam | None = db.session.scalar(
            sa.select(GithubTeam).where(
                GithubTeam.account_id == account_id,
                GithubTeam.organization == org,
                GithubTeam.slug == slug,
            ),
        )
        return negate, None, team
    user: GithubUser | None = db.session.scalar(
        sa.select(GithubUser).where(
            GithubUser.account_id == account_id,
            GithubUser.username == e,
        ),
    )
    return negate, user, None
