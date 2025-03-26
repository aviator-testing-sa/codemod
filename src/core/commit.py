from __future__ import annotations

import structlog

from core import pygithub
from core.models import GithubRepo
from main import app

logger = structlog.stdlib.get_logger()


def get_updated_body(
    repo: GithubRepo,
    pull: pygithub.PullRequest,
) -> str:
    """
    This crops the body based on the start and end delimiters. For more info read:
    https://docs.aviator.co/reference/merge-rules#merge-commit
    """
    # body can sometimes be None if the PR body is empty
    body = pull.body or ""

    if repo.merge_commit.cut_body_before and repo.merge_commit.cut_body_before in body:
        # find first such marker
        body = body.split(repo.merge_commit.cut_body_before, 1)[1]
    if repo.merge_commit.cut_body_after and repo.merge_commit.cut_body_after in body:
        # find last marker
        body = body.rsplit(repo.merge_commit.cut_body_after, 1)[0]

    cropped_body = _strip_html_comments(body, prefix="av pr metadata")
    if repo.merge_commit.strip_html_comments:
        cropped_body = _strip_html_comments(body)
    if repo.merge_commit.include_coauthors:
        cropped_body = _add_coauthors(repo, pull, cropped_body)
    return cropped_body


def _strip_html_comments(body: str, *, prefix: str | None = None) -> str:
    """
    Remove HTML comments from a string.
    :param body: The text to remove HTML comments from.
    :param prefix: If given, only ignore comments that start with the prefix
        (after the `<!--`). See the tests for examples.
    """
    comment_start = "<!--"
    if prefix:
        comment_start += f" {prefix}"
    comment_end = "-->"

    output = ""
    inside_comment = False
    i = 0

    # Whether or not the current line is empty.
    # We use this to remove lines that correspond to only a comment.
    line_is_empty = True
    line_has_comment = False
    while i < len(body):
        if inside_comment:
            if body[i : i + len(comment_end)] == comment_end:
                inside_comment = False
                i += len(comment_end)
            else:
                # Skip the current character.
                i += 1
        elif body[i] == "\n":
            # For lines that only correspond to comments, we just delete them
            # altogether. We preserve newlines otherwise since it sometimes
            # matters to Markdown.
            if not (line_has_comment and line_is_empty):
                output += "\n"
            line_is_empty = True
            line_has_comment = False
            i += 1
        elif body[i : i + len(comment_start)] == comment_start:
            inside_comment = True
            line_has_comment = True
            i += len(comment_start)
        else:
            output += body[i]
            line_is_empty = False
            i += 1
    return output


def _add_coauthors(repo: GithubRepo, pull: pygithub.PullRequest, body: str) -> str:
    """
    This adds co-authors to the body of the PR based on the commits. These are based on the
    expected guidelines by GitHub:
    https://docs.github.com/en/pull-requests/committing-changes-to-your-project/creating-and-editing-commits/creating-a-commit-with-multiple-authors
    """
    # body can sometimes be None if the PR body is empty
    body = body or ""

    # This is the number of commits.
    if not pull.commits:
        return body

    coauthors = []
    try:
        for commit in pull.get_commits():
            if (
                commit.author
                and commit.author.login != pull.user.login
                and commit.author.login != app.config["GITHUB_LOGIN_NAME"]
            ):
                coauthors.append(commit.author)
    except pygithub.IncompletableObject:
        logger.info(
            "Could not fetch commit authors for PR",
            repo_id=repo.id,
            pr_number=pull.number,
        )

    if not coauthors:
        return body

    coauthors_list = [
        f"Co-authored-by: {author.name} <{_get_email(author)}>"
        for author in set(coauthors)
    ]
    coauthors_text = "\n".join(coauthors_list)
    return f"{body}\n\n{coauthors_text}"


def _get_email(user: pygithub.NamedUser) -> str:
    if user.email:
        return user.email
    return f"{user.id}+{user.login}@users.noreply.github.com"
