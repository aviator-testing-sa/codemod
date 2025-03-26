import structlog

from auth.models import AccessToken
from core import client
from core.common import get_client, get_repo_by_id
from core.models import GithubRepo, GithubWorkflow
from main import celery, db

logger = structlog.stdlib.get_logger()


@celery.task
def update_repository_github_workflows(
    repo_id: int, require_billing_active: bool = True
) -> None:
    repo = get_repo_by_id(repo_id, require_billing_active=require_billing_active)
    if not repo:
        return
    access_token, _ = get_client(repo)
    if access_token:
        fetch_repository_github_workflows(access_token, repo)


def fetch_repository_github_workflows(token: AccessToken, repo: GithubRepo) -> None:
    gh_client = client.GithubClient(access_token=token, repo=repo)
    gh_workflows = gh_client.fetch_github_workflows()

    logger.info(
        "Got GitHub workflows for repo",
        repo_id=repo.id,
        num_workflows=len(gh_workflows),
    )

    all_repo_workflows: list[GithubWorkflow] = GithubWorkflow.query.filter_by(
        repo_id=repo.id
    ).all()
    existing_workflows: set[str] = set()

    for gh_workflow in gh_workflows:
        existing_workflows.add(gh_workflow.path)
        ensure_github_workflow(
            repo_id=repo.id,
            name=gh_workflow.name,
            active=(gh_workflow.state == "active"),
            path=gh_workflow.path,
            gh_database_id=gh_workflow.gh_database_id,
            gh_node_id=gh_workflow.node_id,
        )

    workflows_to_remove = [
        wf for wf in all_repo_workflows if wf.path not in existing_workflows
    ]

    if workflows_to_remove:
        logger.info(
            "Marking GitHub workflows as inactive",
            repo_id=repo.id,
            workflows=[w.name for w in workflows_to_remove],
        )
    for workflow in workflows_to_remove:
        workflow.active = False
    db.session.commit()


def ensure_github_workflow(
    repo_id: int,
    name: str,
    active: bool,
    path: str,
    gh_database_id: int,
    gh_node_id: str,
) -> GithubWorkflow:
    wf: GithubWorkflow | None = GithubWorkflow.query.filter_by(
        repo_id=repo_id, path=path
    ).first()

    if not wf:
        wf = GithubWorkflow(
            repo_id=repo_id,
            name=name,
            active=active,
            path=path,
            gh_database_id=gh_database_id,
            gh_node_id=gh_node_id,
        )
        db.session.add(wf)
    else:
        wf.name = name
        wf.active = active
        wf.gh_database_id = gh_database_id
        wf.gh_node_id = gh_node_id

    db.session.commit()
    return wf
