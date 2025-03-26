from __future__ import annotations

import subprocess
import textwrap
from typing import Optional

import structlog

from core import common, pygithub
from core.client import GithubClient
from core.models import GithubRepo
from main import app, celery

logger = structlog.stdlib.get_logger()


@celery.task
def backport_pull_request(
    repo_id: int,
    source_pull_number: int,
    target_branch: str,
) -> None:
    repo: GithubRepo | None = GithubRepo.get_by_id(repo_id)
    if repo is None:
        return

    _, client = common.get_client(repo)
    source_pull = client.get_pull(source_pull_number)
    try:
        bp_pull, clean = _create_backport_pr(client, source_pull, target_branch)
    except Exception as e:
        logger.error(
            "Failed to create backport PR",
            repo_id=repo_id,
            source_pull_number=source_pull_number,
            exc_info=e,
        )
        source_pull.create_issue_comment(
            f"An unexpected error occurred while backporting this PR to ``` {target_branch} ```."
        )
        return

    message = f"Created backport PR #{bp_pull} for this pull request."
    if not clean:
        message += (
            " There were merge conflicts while backporting that need to be resolved."
        )
    source_pull.create_issue_comment(message)


def _create_backport_pr(
    client: GithubClient,
    pull: pygithub.PullRequest,
    target_branch: str,
) -> tuple[int, bool]:
    """
    Create a backport PR from the given source PR.

    Returns true if the PR was created cleanly or false if it was created with
    issues (usually merge conflicts).
    """
    commits = list(pull.get_commits())
    shas = [c.sha for c in commits]

    branch_name = f"av-backport-{pull.number}-{target_branch}"
    errors: list[str] = []
    with client.native_git(pull) as git:
        git.run(
            "git",
            "fetch",
            "--quiet",
            # Fetch with --depth=2 to ensure that we have the parent of every
            # commit (git cherry-pick looks at the diff between a commit and
            # it's parent and will generate an incorrect diff if the parent is
            # not fetched).
            "--depth=2",
            "origin",
            target_branch,
            *shas,
        )
        git.run("git", "checkout", "-b", branch_name, "origin/" + target_branch)

        for sha in shas:
            commit = git.parse_commit(sha)
            if len(commit.parents) > 1:
                # This is a merge commit.
                # We don't actually handle those: cherry-pick will refuse to
                # handle merge commits unless the -m/--mainline option is
                # given; we could look into doing that if necessary but
                # AFAICT other things don't actually deal with merge commits
                # (namely mergify) and I'm not sure what the desired
                # behavior is WRT merge commits.
                errors.append(f"* Ignoring merge commit {sha}: ``` {commit.title} ```")
                continue
            try:
                git.run("git", "cherry-pick", "-x", sha)
            except subprocess.CalledProcessError as exc:
                error_msg = textwrap.dedent(
                    f"* Error while cherry-picking commit `{sha}`: (```  {commit.title} ```):\n"
                    + "  ```````````\n"
                    + textwrap.indent((exc.output or "<unknown>").strip() + "\n", "  ")
                    + "  ```````````\n"
                )
                errors.append(error_msg)
                # commit even though we probably have conflicts
                git.run("git", "add", "-A")
                git.run("git", "commit", "--no-edit", "--allow-empty")

        # Use --force in case the backport was re-created.
        git.run("git", "push", "--force", "origin", branch_name)

    pull_title = f"Backport #{pull.number} {pull.title} to {target_branch}"
    pull_body = f"This pull request is a backport of pull request #{pull.number} to `{target_branch}`."
    if errors:
        pull_body += "\n\n"
        pull_body += "**Potential errors occurred while generating this backport:**\n"
        pull_body += "\n".join(errors)

    bp_pull = client.create_pull(
        branch=branch_name,
        base=target_branch,
        title=pull_title,
        body=pull_body,
        draft=False,
    )
    return bp_pull.number, not errors
