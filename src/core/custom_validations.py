from __future__ import annotations

import re

import structlog

from core.models import GithubRepo, PullRequest, Validation, ValidationType

logger = structlog.stdlib.get_logger()


def check_custom_validations(
    repo: GithubRepo,
    pr: PullRequest,
) -> list[str]:
    """
    Check all the validations associated with a repo for the given pull.

    Returns a list of human-readable issues that were encountered while checking
    the validations.
    """
    validations: list[Validation] = Validation.query.filter(
        Validation.repo_id == repo.id,
        Validation.deleted == False,
    ).all()
    issues: list[str] = []
    for v in validations:
        msg = _check_validation(v, pr)
        if msg:
            issues.append(msg)
    return issues


def _check_validation(v: Validation, pr: PullRequest) -> str:
    try:
        regex = re.compile(v.value)
    except Exception as exc:
        logger.error(
            "Invalid regex-expression",
            validation_id=v.id,
            exc_info=exc,
        )
        return f"Validation ({v.name}) failed: invalid regex expression"

    if v.type is ValidationType.Title:
        if not regex.search(pr.title or ""):
            return f"Validation ({v.name}) failed: title does not match regex ```` {v.value} ````"
    else:
        if not regex.search(pr.body):
            return f"Validation ({v.name}) failed: body does not match regex ```` {v.value} ````"
    return ""
