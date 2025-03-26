from __future__ import annotations

import typing_extensions as TE

from . import gh_hacks

# Don't complain about unused import -- we import gh_hacks only for side
# effects/secret monkey patching.
_ = gh_hacks

# The merge method used to merge a pull request on GitHub.
MergeMethod = TE.Literal[
    # Merge a pull request using a Git merge commit.
    "merge",
    # Merge a pull request by creating a squash commit.
    "squash",
    # Merge a pull request by rebasing the pull's commits onto the base branch.
    "rebase",
]

# This file is essentially a "barrel import". It's meant to be used to access
# all of the myriad classes within PyGitHub, mostly for the purposes of
# type-checking. Since PyGitHub requires importing each classes from a different
# module, it's cumbersome, and since many classes names are similar to our
# internal models, this file is helpful to distinguish between (e.g.,)
# PullRequest (the model in our database) and pygithub.PullRequest (which is the data
# we have from GitHub about GH's view of that pull request).

from github import Auth, Github
from github.AccessToken import AccessToken
from github.ApplicationOAuth import ApplicationOAuth
from github.AuthenticatedUser import AuthenticatedUser
from github.Authorization import Authorization
from github.AuthorizationApplication import AuthorizationApplication
from github.Branch import Branch
from github.BranchProtection import BranchProtection
from github.CheckRun import CheckRun
from github.CheckRunAnnotation import CheckRunAnnotation
from github.CheckRunOutput import CheckRunOutput
from github.CheckSuite import CheckSuite
from github.Clones import Clones
from github.Commit import Commit
from github.CommitCombinedStatus import CommitCombinedStatus
from github.CommitComment import CommitComment
from github.CommitStats import CommitStats
from github.CommitStatus import CommitStatus
from github.Comparison import Comparison
from github.ContentFile import ContentFile
from github.Deployment import Deployment
from github.DeploymentStatus import DeploymentStatus
from github.Download import Download
from github.Event import Event
from github.File import File
from github.Gist import Gist
from github.GistComment import GistComment
from github.GistFile import GistFile
from github.GistHistoryState import GistHistoryState
from github.GitAuthor import GitAuthor
from github.GitBlob import GitBlob
from github.GitCommit import GitCommit
from github.GithubApp import GithubApp
from github.GithubException import (
    BadAttributeException,
    BadCredentialsException,
    BadUserAgentException,
    GithubException,
    IncompletableObject,
    RateLimitExceededException,
    TwoFactorException,
    UnknownObjectException,
)

# PyGitHub uses a custom "not set" sentinel object
from github.GithubObject import GithubObject, _NotSetType
from github.GithubObject import NotSet as _NotSet
from github.GitignoreTemplate import GitignoreTemplate
from github.GitObject import GitObject
from github.GitRef import GitRef
from github.GitRelease import GitRelease
from github.GitReleaseAsset import GitReleaseAsset
from github.GitTag import GitTag
from github.GitTree import GitTree
from github.GitTreeElement import GitTreeElement
from github.Hook import Hook
from github.HookDescription import HookDescription
from github.HookResponse import HookResponse
from github.InputFileContent import InputFileContent
from github.InputGitAuthor import InputGitAuthor
from github.InputGitTreeElement import InputGitTreeElement
from github.Installation import Installation
from github.InstallationAuthorization import InstallationAuthorization
from github.Invitation import Invitation
from github.Issue import Issue
from github.IssueComment import IssueComment
from github.IssueEvent import IssueEvent
from github.IssuePullRequest import IssuePullRequest
from github.Label import Label
from github.License import License
from github.Membership import Membership
from github.Migration import Migration
from github.Milestone import Milestone
from github.NamedUser import NamedUser
from github.Notification import Notification
from github.NotificationSubject import NotificationSubject
from github.Organization import Organization
from github.PaginatedList import PaginatedList
from github.Path import Path
from github.Permissions import Permissions
from github.Plan import Plan
from github.Project import Project
from github.ProjectCard import ProjectCard
from github.ProjectColumn import ProjectColumn
from github.PublicKey import PublicKey
from github.PullRequest import PullRequest
from github.PullRequestComment import PullRequestComment
from github.PullRequestMergeStatus import PullRequestMergeStatus
from github.PullRequestPart import PullRequestPart
from github.PullRequestReview import PullRequestReview
from github.Rate import Rate
from github.RateLimit import RateLimit
from github.Reaction import Reaction
from github.Referrer import Referrer
from github.Repository import Repository
from github.RepositoryKey import RepositoryKey
from github.RepositoryPreferences import RepositoryPreferences
from github.Requester import Requester
from github.RequiredPullRequestReviews import RequiredPullRequestReviews
from github.RequiredStatusChecks import RequiredStatusChecks
from github.SelfHostedActionsRunner import SelfHostedActionsRunner
from github.SourceImport import SourceImport
from github.Stargazer import Stargazer
from github.StatsCodeFrequency import StatsCodeFrequency
from github.StatsCommitActivity import StatsCommitActivity
from github.StatsContributor import StatsContributor
from github.StatsParticipation import StatsParticipation
from github.StatsPunchCard import StatsPunchCard
from github.Tag import Tag
from github.Team import Team
from github.TeamDiscussion import TeamDiscussion
from github.TimelineEvent import TimelineEvent
from github.TimelineEventSource import TimelineEventSource
from github.Topic import Topic
from github.UserKey import UserKey
from github.View import View
from github.Workflow import Workflow
from github.WorkflowRun import WorkflowRun

import schema


class CommitAuthorInfo(schema.BaseModel):
    """
    A commit author object.

    Modeled to match the data included in webhook payloads (e.g., the commits
    array in a `push` webhook event).

    https://docs.github.com/developers/webhooks-and-events/webhooks/webhook-events-and-payloads#push
    (see the "properties of commits" > "properties of author" section)
    """

    date: str | None = None

    #: The email given by the commit author.
    email: str | None = None

    #: The name given by the commit author.
    name: str

    username: str | None = schema.Field(
        None,
        description="The GitHub username of the author, if available. "
        "Because this information is derived from the Git commit (i.e., the "
        "metadata added by Git on the machine where the commit is being "
        "created), the commit author might not actually correspond to a GitHub "
        "user. This can happen (for example) if the Git client's email address "
        "is misconfigured or if the commit was authored by a bot.",
    )


class CommitInfo(schema.BaseModel):
    """
    A commit object.

    Modeled to match the data included in webhook payloads (e.g., the commits
    array in a `push` webhook event).

    https://docs.github.com/developers/webhooks-and-events/webhooks/webhook-events-and-payloads#push
    """

    #: A list of files that were added in this commit.
    added: list[str]

    #: Information about the Git author of this commit.
    author: CommitAuthorInfo

    #: Information about the person who committed this commit.
    committer: CommitAuthorInfo

    #: Whether this commit is distinc from any that have been pushed before.
    distinct: bool

    #: The Git object ID (SHA) of this commit.
    id: str

    # The commit message.
    message: str

    #: A list of files that were removed in this commit.
    modified: list[str]

    #: A list of files that were modified in this commit.
    removed: list[str]

    #: The ISO 8601 timestamp of the commit.
    timestamp: str

    #: The Git object ID (SHA) of the tree associated with this commit.
    tree_id: str

    # URL that points to the commit API resource.
    url: str


NotSet: _NotSetType = _NotSet
NotSetType = _NotSetType

__all__ = [
    "Github",
    # Re-exported from other modules
    "AccessToken",
    "ApplicationOAuth",
    "AuthenticatedUser",
    "Authorization",
    "AuthorizationApplication",
    "BadAttributeException",
    "BadCredentialsException",
    "BadUserAgentException",
    "Branch",
    "BranchProtection",
    "CheckRun",
    "CheckRunAnnotation",
    "CheckRunOutput",
    "CheckSuite",
    "Clones",
    "Commit",
    "CommitCombinedStatus",
    "CommitComment",
    "CommitStats",
    "CommitStatus",
    "Comparison",
    "ContentFile",
    "Deployment",
    "DeploymentStatus",
    "Download",
    "Event",
    "File",
    "Gist",
    "GistComment",
    "GistFile",
    "GistHistoryState",
    "GitAuthor",
    "GitBlob",
    "GitCommit",
    "GitObject",
    "GitRef",
    "GitRelease",
    "GitReleaseAsset",
    "GitTag",
    "GitTree",
    "GitTreeElement",
    "GithubApp",
    "GithubException",
    "GithubObject",
    "GitignoreTemplate",
    "Hook",
    "HookDescription",
    "HookResponse",
    "IncompletableObject",
    "InputFileContent",
    "InputGitAuthor",
    "InputGitTreeElement",
    "Installation",
    "InstallationAuthorization",
    "Invitation",
    "Issue",
    "IssueComment",
    "IssueEvent",
    "IssuePullRequest",
    "Label",
    "License",
    "Membership",
    "Migration",
    "Milestone",
    "NamedUser",
    "Notification",
    "NotificationSubject",
    "NotSet",
    "Organization",
    "PaginatedList",
    "Path",
    "Permissions",
    "Plan",
    "Project",
    "ProjectCard",
    "ProjectColumn",
    "PublicKey",
    "PullRequest",
    "PullRequestComment",
    "PullRequestMergeStatus",
    "PullRequestPart",
    "PullRequestReview",
    "Rate",
    "RateLimit",
    "RateLimitExceededException",
    "Reaction",
    "Referrer",
    "Repository",
    "RepositoryKey",
    "RepositoryPreferences",
    "Requester",
    "RequiredPullRequestReviews",
    "RequiredStatusChecks",
    "SelfHostedActionsRunner",
    "SourceImport",
    "Stargazer",
    "StatsCodeFrequency",
    "StatsCommitActivity",
    "StatsContributor",
    "StatsParticipation",
    "StatsPunchCard",
    "Tag",
    "Team",
    "TeamDiscussion",
    "TimelineEvent",
    "TimelineEventSource",
    "Topic",
    "TwoFactorException",
    "UnknownObjectException",
    "UserKey",
    "View",
    "Workflow",
    "WorkflowRun",
]
