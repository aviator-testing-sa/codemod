from __future__ import annotations

import collections

import sqlalchemy as sa
import sqlalchemy.dialects.postgresql as sa_pg
import structlog

import errors
from api.schema import ApiPullRequest
from basemodel import BaseModelQuery
from core import comments, common, pygithub
from core.models import (
    AffectedTarget,
    BotPr,
    GithubRepo,
    GithubUser,
    PrDependency,
    PullRequest,
    ReadySource,
    target_pr_mapping,
)
from core.status_codes import StatusCode
from main import db
from util import time_util

logger = structlog.stdlib.get_logger()


def update_pr(
    account_id: int, pr_object: ApiPullRequest, *, action: str
) -> PullRequest:
    """
    This is called anytime affected_targets are defined (via API or slash command).
    """
    if pr_object.affected_targets is None:
        raise errors.InvalidValueException("Missing affected_targets")

    repo_name = f"{pr_object.repository.org}/{pr_object.repository.name}"
    repo, pr = common.get_repo_and_pr(account_id, repo_name, pr_object.number)
    if not pr:
        pr = create_pull_request(repo, pr_object.number)

    if pr.status in ["open", "pending", "blocked"]:
        # TODO(ankit): Figure out how to manage empty affected targets list.
        target_dict = get_or_create_targets(repo, pr_object.affected_targets)
        db.session.commit()
        db.session.execute(
            sa.delete(target_pr_mapping).where(
                target_pr_mapping.c.pull_request_id == pr.id,
            ),
        )
        db.session.execute(
            sa_pg.insert(target_pr_mapping).values(
                [
                    {"pull_request_id": pr.id, "affected_target_id": target.id}
                    for target in target_dict.values()
                ],
            ),
        )
        db.session.commit()
        # By deleting the cached property, it will be refetched on next access.
        if hasattr(pr, "affected_targets"):
            del pr.affected_targets
        if action == "queue":
            set_merge_operations(pr, pr_object)
            pr.set_status("pending", StatusCode.PENDING_MERGEABILITY)
            pr.queued_at = time_util.now()
        db.session.commit()
        update_dependency_graph(repo, pr)
        comments.update_pr_sticky_comment.delay(pr.id)
    return pr


def queue_pr_with_targets(
    repo: GithubRepo, pr: PullRequest, targets_list: list[str], gh_user: GithubUser
) -> PullRequest:
    if pr.status in ["queued", "tagged"]:
        raise errors.InvalidActionException("This pull request is already queued")
    target_dict = get_or_create_targets(repo, targets_list)
    db.session.commit()
    db.session.execute(
        sa.delete(target_pr_mapping).where(
            target_pr_mapping.c.pull_request_id == pr.id,
        ),
    )
    db.session.execute(
        sa_pg.insert(target_pr_mapping).values(
            [
                {"pull_request_id": pr.id, "affected_target_id": target.id}
                for target in target_dict.values()
            ],
        ),
    )
    db.session.commit()
    # By deleting the cached property, it will be refetched on next access.
    if hasattr(pr, "affected_targets"):
        del pr.affected_targets
    common.ensure_merge_operation(
        pr, ready=True, ready_source=ReadySource.SLASH_COMMAND, ready_user_id=gh_user.id
    )
    pr.set_status("pending", StatusCode.PENDING_MERGEABILITY)
    pr.queued_at = time_util.now()
    db.session.commit()
    update_dependency_graph(repo, pr)
    comments.update_pr_sticky_comment.delay(pr.id)
    return pr


def create_pull_request(repo: GithubRepo, pr_number: int) -> PullRequest:
    access_token, client = common.get_client(repo)
    if not client:
        raise errors.InvalidConfigurationException("Could not connect to Github")

    try:
        pull = client.get_pull(pr_number)
    except pygithub.UnknownObjectException:
        raise errors.InvalidValueException("Invalid pull_request.number")

    _, pr = common.ensure_pull_request(repo, pull)
    return pr


def get_or_create_targets(
    repo: GithubRepo, targets: list[str]
) -> dict[str, AffectedTarget]:
    target_objs = (
        AffectedTarget.query.filter_by(repo_id=repo.id)
        .filter(AffectedTarget.name.in_(targets))
        .all()
    )
    target_dict = {t.name: t for t in target_objs}
    for target in targets:
        if target not in target_dict:
            target_obj = AffectedTarget(repo_id=repo.id, name=target)
            db.session.add(target_obj)
            target_dict[target] = target_obj
    return target_dict


def set_merge_operations(pr: PullRequest, pr_object: ApiPullRequest) -> None:
    mo = common.ensure_merge_operation(
        pr, ready=True, ready_source=ReadySource.REST_API
    )
    mo.commit_message = (
        pr_object.merge_commit_message.body if pr_object.merge_commit_message else ""
    )
    mo.commit_title = (
        pr_object.merge_commit_message.title if pr_object.merge_commit_message else ""
    )


def should_reset(repo: GithubRepo, skip_line_pr: PullRequest) -> bool:
    if not repo.parallel_mode_config.use_affected_targets:
        return True
    for pr in skip_line_pr.blocking_prs:
        if not pr.skip_line:
            return True
    return False


def get_all_top_prs(repo: GithubRepo, branch_name: str) -> list[PullRequest]:
    top_prs = []
    tracked_targets: set[AffectedTarget] = set()
    prs = common.get_all_tagged(repo.id, branch_name)
    for pr in prs:
        if not (set(pr.affected_targets) & tracked_targets):
            top_prs.append(pr)
        tracked_targets.update(pr.affected_targets)
    return top_prs


def get_blocking_prs(repo: GithubRepo, pr: PullRequest) -> set[PullRequest]:
    """
    Find all the PRs queued before this PR that will need to be
    cleared before this can be merged.

    This is a bit more complicated than originally expected. It's also
    incorrect to use BotPR objects here because some PRs may still be
    in queued state without a botPR.
    """
    target_ids = [t.id for t in pr.affected_targets]
    pr_query: BaseModelQuery = (
        PullRequest.query.filter_by(
            repo_id=repo.id, target_branch_name=pr.target_branch_name
        )
        .filter(PullRequest.queued_at < pr.queued_at)
        .filter(PullRequest.status.in_(("queued", "tagged")))
    )
    if pr.skip_line:
        pr_query = pr_query.filter_by(skip_line=True)

    pr_list = (
        pr_query.join(target_pr_mapping)
        .filter(target_pr_mapping.c.affected_target_id.in_(target_ids))
        .all()
    )

    skip_pr_list: list[PullRequest] = []
    if not pr.skip_line:
        # If the provided PR is not a skip_line PR, we should then also
        # include all other skip_line PRs that were even queued later.
        skip_prs_query: BaseModelQuery = PullRequest.query.filter_by(
            repo_id=repo.id,
            target_branch_name=pr.target_branch_name,
            skip_line=True,
        ).filter(PullRequest.status.in_(("queued", "tagged")))
        skip_pr_list = (
            skip_prs_query.join(target_pr_mapping)
            .filter(target_pr_mapping.c.affected_target_id.in_(target_ids))
            .all()
        )
    return set(pr_list + skip_pr_list)


def get_dependent_tagged_prs(repo: GithubRepo, pr: PullRequest) -> set[PullRequest]:
    """
    Find all the PRs that are already tagged and have a shared dependency. This
    is slightly different from get_blocking_prs because this method does not
    account for skip line, but rather the just tagged status.
    This is useful to know the "true current state" of tagged PRs, even when
    the order / PR dependencies may shifted.
    """
    target_ids = [t.id for t in pr.affected_targets]
    pr_query: BaseModelQuery = PullRequest.query.filter_by(
        repo_id=repo.id, target_branch_name=pr.target_branch_name
    ).filter(PullRequest.status == "tagged")
    pr_list = (
        pr_query.join(target_pr_mapping)
        .filter(target_pr_mapping.c.affected_target_id.in_(target_ids))
        .all()
    )
    return set(pr_list)


def get_dependent_bot_prs(
    repo: GithubRepo, pull_request_id_list: list[int]
) -> list[BotPr]:
    """
    Find all the PRs queued after this PR that will need to be
    reset if the current PR failed. This also includes the botPR associated
    with current PR.
    """
    # mypy doesn't know about PrDependency.query since it doesn't inherit from
    # BaseModel
    dependencies: list[PrDependency] = PrDependency.query.filter(
        PrDependency.depends_on_pr_id.in_(pull_request_id_list)
    ).all()
    dependent_pr_ids = [d.pull_request_id for d in dependencies] + pull_request_id_list
    pr_list = (
        PullRequest.query.filter_by(repo_id=repo.id, skip_line=False)
        .filter(PullRequest.status.in_(("queued", "tagged")))
        .filter(PullRequest.id.in_(dependent_pr_ids))
    )
    pr_ids = {pr.id for pr in pr_list}

    bot_prs: list[BotPr] = (
        BotPr.query.filter_by(status="queued", repo_id=repo.id)
        .filter(BotPr.pull_request_id.in_(pr_ids))
        .order_by(BotPr.id.asc())
        .all()
    )
    return bot_prs


def get_all_related_bot_prs(repo: GithubRepo, pr: PullRequest) -> list[BotPr]:
    target_ids: list[int] = [t.id for t in pr.affected_targets]
    pr_list: list[PullRequest] = (
        PullRequest.query.filter_by(repo_id=repo.id, status="tagged")
        .join(target_pr_mapping)
        .filter(target_pr_mapping.c.affected_target_id.in_(target_ids))
        .all()
    )
    pr_ids = {pr.id for pr in pr_list}

    bot_prs: list[BotPr] = (
        BotPr.query.filter_by(status="queued", repo_id=repo.id)
        .filter(BotPr.pull_request_id.in_(pr_ids))
        .order_by(BotPr.id.asc())
        .all()
    )
    return bot_prs


def update_dependency_graph(repo: GithubRepo, pr: PullRequest) -> None:
    pr.blocking_prs = list(get_blocking_prs(repo, pr))
    db.session.commit()


def remove_dependencies(repo: GithubRepo, pr: PullRequest) -> None:
    """
    This should be called when the PR was merged and is no longer in the queue.
    This can also be called if the PR was removed from queue for other reasons, but
    that is also covered in #refresh_dependency_graph_on_reset.
    """
    if not repo.parallel_mode_config.use_affected_targets:
        return
    depends_on: list[PrDependency] = PrDependency.query.filter(
        PrDependency.depends_on_pr_id == pr.id
    ).all()
    depends_on_count = len(depends_on)
    pr_ids = []
    for d in depends_on:
        pr_ids.append(d.pull_request_id)
        db.session.delete(d)
    db.session.commit()
    logger.info(
        "Removed the dependency requirement for the PR",
        repo_id=repo.id,
        pr_number=pr.number,
        count=depends_on_count,
        pr_ids=pr_ids,
    )


def refresh_dependency_graph_on_reset(repo: GithubRepo, prs: list[PullRequest]) -> None:
    """
    Refresh the dependency graph for all PRs that depend on given PRs.
    This should always be called if the queue is reset in target mode.
    """
    if not repo.parallel_mode_config.use_affected_targets:
        return
    if not prs:
        logger.info("Refresh dependency graph called with no PRs", repo_id=repo.id)
        return

    pr_ids = [pr.id for pr in prs]
    pr_numbers = [pr.number for pr in prs]
    depends_on: list[PrDependency] = PrDependency.query.filter(
        PrDependency.depends_on_pr_id.in_(pr_ids)
    ).all()
    dependencies: list[PrDependency] = PrDependency.query.filter(
        PrDependency.pull_request_id.in_(pr_ids)
    ).all()
    dependencies_count = len(dependencies)
    depends_on_count = len(depends_on)
    for d in dependencies:
        db.session.delete(d)
    for d in depends_on:
        db.session.delete(d)
    db.session.commit()

    logger.info(
        "Refresh dependency graph on reset",
        repo_id=repo.id,
        pr_number=pr_numbers,
        depends_on_count=depends_on_count,
        dependencies_count=dependencies_count,
    )


def sort_for_batching(prs: list[PullRequest]) -> list[list[PullRequest]]:
    """
    This function is used to sort PRs when target_mode and batching are used together.
    We want to sort by pr.queued_at and by the PR's affected targets. If we have the following PRs:
        PR1: targets=1,2 queued_at 1:00
        PR2: targets=2, queued_at 1:10
        PR3: targets=1,2 queued_at 1:20
        PR4: targets=2, queued_at 1:30
        PR5: no targets, queued_at 1:40
    This returns [[PR1, PR3], [PR2, PR4], [PR5]].

    Note: this works well for non-overlapping affected targets.
    We should still add optimizations for overlapping cases, eg.
        if batch_size=4, the example above should return [[PR1, PR2, PR3, PR4], [PR5]].
    """
    ordered_keys: list[str] = []
    target_map: dict[str, list[PullRequest]] = collections.defaultdict(list)
    for pr in prs:
        if pr.affected_targets:
            key = "".join(sorted(p.name for p in pr.affected_targets))
            target_map[key].append(pr)
        else:
            key = ""
            target_map[key].append(pr)
        if key not in ordered_keys:
            ordered_keys.append(key)

    # Note: the given prs should already be sorted by queued_at in asc order, so we don't need to
    # worry about the order of PRs in the map. We just need to handle the keys in order.
    sorted_prs: list[list[PullRequest]] = []
    for key in ordered_keys:
        sorted_prs.append(target_map[key])

    return sorted_prs
