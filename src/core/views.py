from __future__ import annotations

import csv
import datetime
import difflib
import io
import math
import re
import urllib.parse

import sqlalchemy as sa
import structlog
from flask import (
    Response,
    abort,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from auth import admin_required, ghauth, mq_redirect
from auth.common import is_domain_eligible
from auth.decorators import maintainer_required
from auth.models import AccessToken, Account
from billing import capability
from billing.plans import Feature
from core import (
    apply_config,
    common,
    connect,
    extract,
    github_label,
    queue,
)
from core.configurator import Configurator
from core.configurator_schema import (
    MergeMode,
    MergeRules,
    ParallelMode,
    Preconditions,
)
from core.models import (
    AuditLog,
    AuditLogAction,
    AuditLogEntity,
    BaseBranch,
    ConfigHistory,
    ConfigType,
    GithubLabel,
    GithubLabelPurpose,
    GithubRepo,
    GithubTest,
    GithubUser,
    PullRequest,
)
from core.status_codes import StatusCode
from main import app, csrf, db, token_auth
from slackhook import notify
from util.flaskutil import RouteResponse
from webhooks import controller as webhook_controller

logger = structlog.stdlib.get_logger()
QUEUE_STATUS_PAGE_SIZE = 10
REPOS_PER_PAGE = 10
COLLABORATORS_PER_PAGE = 10
CONFIG_HISTORY_PAGE_SIZE = 5


@app.route("/github")
@mq_redirect
def github_index() -> RouteResponse:
    return render_template("index.html")


@app.route("/github/config-history", methods=["GET"])
@mq_redirect
@login_required
def yaml_config_history_get() -> RouteResponse:
    repo: GithubRepo | None = _selected_repo()
    if not repo:
        return redirect("/github/repos")
    account_id = current_user.user.account_id
    repos: list[GithubRepo] = GithubRepo.query.filter_by(account_id=account_id).all()

    page = int(request.args.get("page", default=1))
    if page < 1:
        page = 1

    next_page = f"/github/config-history?repo={repo.name}&page={page + 1}"
    prev_page = f"/github/config-history?repo={repo.name}&page={page - 1}"
    offset = (page - 1) * CONFIG_HISTORY_PAGE_SIZE
    config_history_records: list[ConfigHistory] = (
        ConfigHistory.query.filter_by(repo_id=repo.id, config_type=ConfigType.Main)
        .filter(sa.sql.expression.or_(ConfigHistory.applied_at.is_not(None)))
        .order_by(ConfigHistory.created.desc())
        .offset(offset)
        .limit(CONFIG_HISTORY_PAGE_SIZE + 1)  # we need one extra for the diffs!
        .all()
    )
    has_next = len(config_history_records) == CONFIG_HISTORY_PAGE_SIZE + 1
    config_diffs = []
    for index, current_elem in enumerate(config_history_records[:-1]):
        # elements are in descending order so "next" element is the past one!
        past_elem = config_history_records[index + 1]
        config_diffs.append(
            "\n".join(
                difflib.unified_diff(
                    past_elem.config_text.splitlines(),
                    current_elem.config_text.splitlines(),
                    fromfile="old/config.yml",
                    tofile="new/config.yml",
                    lineterm="",
                )
            )
        )

    return render_template(
        "forms/config_history.html",
        repo=repo,
        repos=repos,
        history=config_history_records[:-1],
        config_diffs=config_diffs,
        next_page=next_page,
        prev_page=prev_page,
        has_next=has_next,
    )


@app.route("/github/tests", methods=["POST"])
@mq_redirect
@maintainer_required
def github_tests_post() -> RouteResponse:
    # For post request, always read from form data
    repo_name = request.form.get("repo_name")
    repo: GithubRepo | None = GithubRepo.query.filter_by(
        account_id=current_user.user.account_id, name=repo_name
    ).first()
    if not repo:
        return abort(403, "unable to find repository")

    dbtests = _get_all_db_tests()
    botpr = False

    gh_mergeability: bool = repo.preconditions.use_github_mergeability or True
    # handle required checks
    if "github-mergeability" in request.form:
        github_mergeability = request.form.get("github-mergeability")
        if github_mergeability == "on":
            gh_mergeability = True
    elif "required-tests[]" in request.form:
        required_tests = request.form.getlist("required-tests[]")
        for dbtest in dbtests:
            dbtest.ignore = dbtest.name not in required_tests
        gh_mergeability = False

    # handle override checks for parallel mode
    if "same-checks" in request.form:
        botpr = True
        same_checks = request.form.get("same-checks")
        if same_checks == "on":
            for dbtest in dbtests:
                dbtest.is_required_bot_pr_check = False
    elif "draft-tests[]" in request.form:
        botpr = True
        draft_tests = request.form.getlist("draft-tests[]")
        for dbtest in dbtests:
            dbtest.is_required_bot_pr_check = dbtest.name in draft_tests

    db.session.commit()
    logger.info("Account %d updated tests for repo %d", repo.account_id, repo.id)

    # now that we have committed the tests changes to the DB, update config history
    configurator = Configurator(repo)
    merge_rules = _merge_tests_from_form(
        repo, botpr=botpr, gh_mergeability=gh_mergeability
    )
    configurator.update_config_history(
        applied=True,
        aviator_user_id=current_user.user.id,
        # this is the latest merge rules built from database
        # we do this because we updated the associated objects above
        # now we just want to get the merge rules object, so we can
        # store the current state of the config
        merge_rules=merge_rules,
        # we made no changes to the scenarios, so we just pull
        # whatever they were from the last config record
        scenarios=configurator.current_scenarios(),
        testdeck=configurator.current_testdeck(),
    )

    use_same_checks = not any([dbtest.is_required_bot_pr_check for dbtest in dbtests])

    if repo.preconditions.use_github_mergeability:
        checks_label = "Use GitHub required checks"
    else:
        selected_checks = [dbtest.name for dbtest in dbtests if not dbtest.ignore]
        checks_label = f"{len(selected_checks)} checks selected"

    if use_same_checks:
        draft_checks_label = "Use same checks on draft PRs"
    else:
        botpr_checks = [
            dbtest.name for dbtest in dbtests if dbtest.is_required_bot_pr_check
        ]
        draft_checks_label = f"{len(botpr_checks)} checks selected"

    return jsonify(
        success=True, checks_label=checks_label, draft_checks_label=draft_checks_label
    )


@app.route("/github/rules", methods=["POST"])
@mq_redirect
@maintainer_required
def github_rules_post() -> RouteResponse:
    error = None
    repos = _all_repos()
    # For post request, always read from form data
    repo_name = request.form.get("repo_name")
    repo: GithubRepo | None = GithubRepo.query.filter_by(
        account_id=current_user.user.account_id, name=repo_name
    ).first()
    if not repo:
        return abort(403, "unable to find repository")

    repo.active = True
    merge_mode = request.form.get("merge-mode")

    merge_rules = repo.current_config.merge_rules
    if not merge_rules.merge_mode:
        logger.warning("MergeMode not found", repo_id=repo.id)
        merge_rules.merge_mode = MergeMode()
    if merge_mode == "unordered":
        merge_rules.merge_mode.type = "no-queue"
    elif merge_mode == "parallel":
        merge_rules.merge_mode.type = "parallel"
        if not merge_rules.merge_mode.parallel_mode:
            merge_rules.merge_mode.parallel_mode = ParallelMode()
    else:
        merge_rules.merge_mode.type = "default"

    configurator = Configurator(repo)
    history = configurator.update_config_history(
        applied=True,
        aviator_user_id=current_user.user.id,
        merge_rules=merge_rules,
        # we made no changes to the scenarios, so we just pull
        # whatever they were from the last config record
        scenarios=configurator.current_scenarios(),
        testdeck=configurator.current_testdeck(),
    )
    AuditLog.capture_user_action(
        user=current_user.user,
        action=AuditLogAction.QUEUE_CONFIG_CHANGED,
        entity=AuditLogEntity.MERGE_QUEUE,
        target=repo.name,
    )

    logger.info("Account %d updated rules for repo %d", repo.account_id, repo.id)

    queue.config_consistency_check.delay(repo.id)
    if history:
        webhook_controller.call_webhook_for_config_change.delay(history.id)
        notify.notify_config_change.delay(history.id)

    if repo.is_no_queue_mode:
        # When someone updates the config to a non-queue mode,
        # we need to trigger a re-process to clear out the existing queue.
        queue.process_unordered_async.delay(repo.id)

    merge_labels = {label.purpose: label.name for label in repo.merge_labels}
    dbtests = _get_all_db_tests()
    use_same_checks = not any([dbtest.is_required_bot_pr_check for dbtest in dbtests])
    selected_checks = [dbtest.name for dbtest in dbtests if not dbtest.ignore]
    botpr_checks = [
        dbtest.name for dbtest in dbtests if dbtest.is_required_bot_pr_check
    ]

    return render_template(
        "forms/merge_rules.html",
        repo=repo,
        repos=repos,
        error=error,
        success=True,
        merge_labels=merge_labels,
        tests=dbtests,
        use_same_checks=use_same_checks,
        selected_checks=selected_checks,
        botpr_checks=botpr_checks,
    )


def _merge_tests_from_form(
    repo: GithubRepo, botpr: bool, gh_mergeability: bool
) -> MergeRules:
    """
    This method explicitly picks the tests overrides based on the changes done
    in the UI. This ensures that we don't accidentally set wrong values in the config history.
    """
    merge_rules = repo.current_config.merge_rules
    if not botpr:
        if not merge_rules.preconditions:
            merge_rules.preconditions = Preconditions()
        merge_rules.preconditions.use_github_mergeability = gh_mergeability
        if not gh_mergeability:
            required_tests = repo.get_required_tests(botpr=False)
            merge_rules.preconditions.required_checks = [
                test.name for test in required_tests
            ]
    else:
        bot_pr_checks: list[GithubTest] = GithubTest.query.filter_by(
            repo_id=repo.id, is_required_bot_pr_check=True
        ).all()
        if not merge_rules.merge_mode:
            merge_rules.merge_mode = MergeMode()
        if not merge_rules.merge_mode.parallel_mode:
            merge_rules.merge_mode.parallel_mode = ParallelMode()
        merge_rules.merge_mode.parallel_mode.override_required_checks = [
            test.name for test in bot_pr_checks
        ]
    return merge_rules


@app.route("/github/rules", methods=["GET"])
@mq_redirect
@login_required
def github_rules_get() -> RouteResponse:
    repos = _all_repos()
    repo = _selected_repo()
    if not repo:
        return redirect("/github/repos")
    merge_labels = {label.purpose: label.name for label in repo.merge_labels}
    if not repo.active:
        extract.analyze_first_pull.delay(repo.id)
    dbtests = _get_all_db_tests()
    use_same_checks = not any([dbtest.is_required_bot_pr_check for dbtest in dbtests])
    selected_checks = [dbtest.name for dbtest in dbtests if not dbtest.ignore]
    botpr_checks = [
        dbtest.name for dbtest in dbtests if dbtest.is_required_bot_pr_check
    ]

    resp = make_response(
        render_template(
            "forms/merge_rules.html",
            repo=repo,
            repos=repos,
            merge_labels=merge_labels,
            tests=dbtests,
            use_same_checks=use_same_checks,
            selected_checks=selected_checks,
            botpr_checks=botpr_checks,
            use_github_mergeability=repo.preconditions.use_github_mergeability,
        )
    )
    set_repo_cookie(resp, repo)
    return resp


@app.route("/github/repos")
@mq_redirect
@login_required
def github_repos() -> RouteResponse:
    repos = _all_repos()
    return render_template("github/repos.html", repos=repos)


@app.route("/internal/api/github/repos")
@login_required
def github_repos_json() -> RouteResponse:
    page: int = request.args.get("page", type=int, default=1)
    offset = REPOS_PER_PAGE * (page - 1) if page > 1 else 0
    total_repos_count: int = GithubRepo.query.filter_by(
        account_id=current_user.user.account_id
    ).count()

    page_repos_info = [
        {"name": repo.name, "active": repo.active, "enabled": repo.enabled}
        for repo in GithubRepo.query.filter_by(account_id=current_user.user.account_id)
        .order_by("id")
        .offset(offset)
        .limit(REPOS_PER_PAGE)
        .all()
    ]

    page_info = {
        "page": page,
        "total_pages": math.ceil(total_repos_count / REPOS_PER_PAGE),
        "total_count": total_repos_count,
        "page_size": REPOS_PER_PAGE,
    }

    return jsonify(
        {
            "page_info": page_info,
            "repos": page_repos_info,
            "setup_url": app.config["APP_SETUP_URL"],
        }
    )


@app.route("/internal/api/github/repos/setup")
@login_required
def github_repos_setup() -> RouteResponse:
    account_id = current_user.user.account_id
    flow_arg = request.args.get("flow", type=str, default="onboarding")

    flow: ghauth.GitHubAppInstallationFlow = "onboarding"
    if flow_arg == "flexreview_onboarding":
        flow = "flexreview_onboarding"
    elif flow_arg == "releases_onboarding":
        flow = "releases_onboarding"

    return jsonify(
        {
            "setup_url": ghauth.create_github_app_install_url(
                account_id=account_id,
                flow=flow,
            ),
        }
    )


@app.route("/github/repos/fetch")
@mq_redirect
@login_required
def fetch_github_repos() -> RouteResponse:
    authorized, has_valid_token = _fetch_github_repos_internal()

    if not authorized:
        return "You need to authorize your Github repository before requesting fetch."

    if not has_valid_token:
        return "Your Github connection has been revoked. Please reauthorize your Github repository before requesting fetch."

    return redirect("/github/repos")


@app.route("/internal/api/github/repos/fetch", methods=["POST"])
@login_required
def fetch_github_repos_json() -> tuple[Response, int]:
    authorized, has_valid_token = _fetch_github_repos_internal()if not authorized:
        return (
            jsonify(
                error="no-authorization",
                message="You need to authorize your Github repository before requesting fetch.",
            ),
            400,
        )

    if not has_valid_token:
        return (
            jsonify(
                error="revoked-connection",
                message="Your Github connection has been revoked. Please reauthorize your Github repository before requesting fetch.",
            ),
            400,
        )

    return jsonify({})


def _fetch_github_repos_internal() -> tuple[bool, bool]:
    account_id = current_user.user.account_id
    access_token: AccessToken | None = db.session.scalar(
        sa.select(AccessToken).filter_by(
            account_id=account_id,
            is_valid=True,
        )
    )
    if not access_token:
        return (False, False)

    access_token = connect.ensure_access_token(access_token)
    if not access_token:
        return (True, False)

    # this fetches the first page of repos for the first access_token
    connect.fetch_repos(access_token)
    # we separate out this async call to fetch all other repos for all pages and access_tokens
    # so that the user is not blocked
    connect.fetch_repos_async.delay(account_id)

    return (True, True)


@app.route("/github/repo/action", methods=["GET"])
@mq_redirect
@maintainer_required
def github_repo_action() -> RouteResponse:
    repo_name = request.args.get("repo_name")
    repo: GithubRepo | None = db.session.scalar(
        sa.select(GithubRepo).filter_by(
            account_id=current_user.user.account_id, name=repo_name
        )
    )
    if not repo:
        return redirect("/github/repos")
    action = request.args.get("action")
    if action == "deactivate" and repo.active:
        domain = (
            repo.account.email.split("@")[1]
            if (repo.account.email and "@" in repo.account.email)
            else repo.account.email
        )
        logger.info(
            "Deactivating repo %d for account %d, domain %s",
            repo.id,
            repo.account_id,
            domain,
        )
        repo.active = False
        db.session.commit()
        AuditLog.capture_user_action(
            user=current_user.user,
            action=AuditLogAction.QUEUE_DEACTIVATED,
            entity=AuditLogEntity.MERGE_QUEUE,
            target=repo.name,
        )
    elif action == "activate" and not repo.active:
        repo.active = True
        common.pause_repo(
            actor=current_user.user, repo=repo, paused=False
        )  # this internally does db.commit
        github_label.create_default_labels.delay(repo.id)

        # since we are activating the repo we might have changes to configs
        # that were from GH in events that got ignored
        apply_config.apply_config_from_gh_file.delay(
            repo_name=repo.name, aviator_user_id=repo.account.first_admin.id
        )
        AuditLog.capture_user_action(
            user=current_user.user,
            action=AuditLogAction.QUEUE_ACTIVATED,
            entity=AuditLogEntity.MERGE_QUEUE,
            target=repo.name,
        )
    elif action == "delete":
        return redirect(url_for("github_repos", success=True))
    return redirect("/github/repos")


@app.route("/internal/api/github/repo/action", methods=["POST"])
@maintainer_required
def github_repo_action_json() -> RouteResponse:
    repo_name: str = request.json["repo_name"].strip()
    repo: GithubRepo | None = db.session.scalar(
        sa.select(GithubRepo).filter_by(
            account_id=current_user.user.account_id, name=repo_name
        )
    )
    if not repo:
        return (
            jsonify(error="invalid-repo", message="Invalid repo"),
            400,
        )
    action: str = request.json["action"].strip()
    if action == "deactivate" and repo.active:
        domain = (
            repo.account.email.split("@")[1]  # type: ignore[union-attr]
            if "@" in repo.account.email  # type: ignore[operator]
            else repo.account.email
        )
        logger.info(
            "Deactivating repo %d for account %d, domain %s",
            repo.id,
            repo.account_id,
            domain,
        )
        repo.active = False
        db.session.commit()
        AuditLog.capture_user_action(
            user=current_user.user,
            action=AuditLogAction.QUEUE_DEACTIVATED,
            entity=AuditLogEntity.MERGE_QUEUE,
            target=repo.name,
        )
    elif action == "activate" and not repo.active:
        repo.active = True
        common.pause_repo(
            actor=current_user.user, repo=repo, paused=False
        )  # this internally does db.commit

        # since we are activating the repo we might have changes to configs
        # that were from GH in events that got ignored
        apply_config.apply_config_from_gh_file.delay(
            repo_name=repo.name, aviator_user_id=repo.account.first_admin.id
        )
        AuditLog.capture_user_action(
            user=current_user.user,
            action=AuditLogAction.QUEUE_ACTIVATED,
            entity=AuditLogEntity.MERGE_QUEUE,
            target=repo.name,
        )
    elif action == "unpause" and not repo.enabled:
        common.pause_repo(actor=current_user.user, repo=repo, paused=False)
    elif action == "pause" and repo.enabled:
        common.pause_repo(actor=current_user.user, repo=repo, paused=True)
    else:
        return (
            jsonify(error="invalid-action", message="Invalid action for this repo"),
            400,
        )
    return jsonify({}), 200


@app.route("/github/repo/delete", methods=["POST"])
@mq_redirect
@admin_required
def github_repo_delete() -> RouteResponse:
    repo_id: str = request.form.get("repo_id")  # type: ignore[assignment]
    repo: GithubRepo = db.session.scalar(
        sa.select(GithubRepo).filter_by(
            account_id=current_user.user.account_id, id=int(repo_id)
        )
    )
    if not repo:
        return jsonify(success=False, error="Invalid repo selected or already removed")

    repo.deleted = True
    db.session.commit()
    AuditLog.capture_user_action(
        user=current_user.user,
        action=AuditLogAction.REPO_REMOVED,
        entity=AuditLogEntity.REPOSITORY,
        target=repo.name,
    )
    return jsonify(success=True)


@app.route("/internal/api/github/repo/delete", methods=["POST"])
@mq_redirect
@admin_required
def github_repo_delete_json() -> RouteResponse:
    repo_name: str = request.json["repo_name"].strip()
    repo: GithubRepo | None = db.session.scalar(
        sa.select(GithubRepo).filter_by(
            account_id=current_user.user.account_id, name=repo_name
        )
    )
    if not repo:
        return (
            jsonify(
                error="invalid-repo", message="Invalid repo selected or already removed"
            ),
            400,
        )

    repo.deleted = True
    db.session.commit()
    AuditLog.capture_user_action(
        user=current_user.user,
        action=AuditLogAction.REPO_REMOVED,
        entity=AuditLogEntity.REPOSITORY,
        target=repo.name,
    )
    return jsonify({}), 200


@app.route("/github/rules/<owner>/<repo_name>", methods=["GET", "POST"])
@mq_redirect
@login_required
def github_repo(owner: str, repo_name: str) -> RouteResponse:
    return redirect(f"/repos/{owner}/{repo_name}/queue/config")


@app.route("/github/collaborators")
@mq_redirect
@login_required
def github_users() -> RouteResponse:
    repos = _active_repos()
    if not repos:
        return redirect("/github/repos")
    gusers: list[GithubUser] = db.session.scalars(
        sa.select(GithubUser).filter_by(
            account_id=current_user.user.account_id
        )
    ).all()
    billed_count = len([u for u in gusers if u.billed])
    total_count = len([u for u in gusers])
    return render_template(
        "github/users.html",
        gusers=gusers,
        total_count=total_count,
        billed_count=billed_count,
    )


@app.route("/internal/api/github/collaborators")
@login_required
def github_users_json() -> RouteResponse:
    page: int = request.args.get("page", type=int, default=1)
    user = current_user.user
    repos = _active_repos()

    offset = COLLABORATORS_PER_PAGE * (page - 1) if page > 1 else 0
    total_github_users_count: int = db.session.scalar(
        sa.select(sa.func.count()).select_from(GithubUser).filter_by(
            account_id=user.account_id
        )
    )

    page_gh_users_info = [
        {
            "avatar": user.qualified_avatar,
            "username": user.username,
            "billed": user.billed,
        }
        for user in db.session.scalars(
            sa.select(GithubUser)
            .filter_by(account_id=current_user.user.account_id)
            .order_by(GithubUser.account_id.desc())
            .offset(offset)
            .limit(COLLABORATORS_PER_PAGE)
        ).all()
    ]

    account = Account.get_by_id_x(user.account_id)
    account_domain_enabled = bool(account.email_domain)
    company_email = current_user.user.account.email
    assert company_email
    email_domain = company_email.split("@")[1]
    sso_enabled = capability.is_supported(
        current_user.user.account_id, Feature.GOOGLE_SSO_WHITELIST
    )
    domain_settings = None
    if capability.is_supported(current_user.user.account_id, Feature.MULTI_USER_LOGIN):
        domain_settings = {
            "email_domain": email_domain,  # use this to check whether company email is ok
            "domain_enable_eligible": is_domain_eligible(email_domain) and sso_enabled,
            "account_domain_enabled": account_domain_enabled,
        }

    page_info = {
        "page": page,
        "total_pages": math.ceil(total_github_users_count / COLLABORATORS_PER_PAGE),
        "total_count": total_github_users_count,
        "page_size": COLLABORATORS_PER_PAGE,
    }

    return jsonify(
        {
            "page_info": page_info,
            "github_users_info": page_gh_users_info if repos else [],
            "domain_settings": domain_settings,
            "is_admin": current_user.user.is_admin,
        }
    )


def _get_validate_yaml_key(repo_id: int) -> str:
    return f"validate-yaml-{repo_id}"


@app.route("/github/validate-config", methods=["POST"])
@mq_redirect
@maintainer_required
def validate_yaml_config_post() -> RouteResponse:
    """
    When a user requests to apply the config, we will first validate
    to identify any failures and return them back to the user. Once validation
    succeeds, we will then store the config in the DB and queue a task to apply
    it async. This ensures the actual application of config happens after acquiring
    a repo level lock. In addition, we also temporarily store the status of the config
    application in redis to show that to the user.
    """
    action = request.form.get("submit")
    yaml_input = request.form.get("yaml_input", "")
    repo_name = request.form.get("repo_name", "")
    repo: GithubRepo | None = db.session.scalar(
        sa.select(GithubRepo).filter_by(
            account_id=current_user.user.account_id, name=repo_name
        )
    )
    if not repo:
        return abort(403, "unable to find repository")
    if not yaml_input:
        return abort(400, "YAML input cannot be empty")

    # parse and validate the input yaml
    config_schema, errors = apply_config.validate_config(
        repo.account_id, repo.id, yaml_input
    )

    if errors or action == "validate":
        # In case of any errors or if the user only requested validation, pass the yaml input
        # as it is back. This yaml input is not saved anywhere else.
        return render_template(
            "forms/validate_config.html",
            action=action,
            errors=errors,
            yaml_input=yaml_input,
            repo=repo,
            repos=_all_repos(),
        )

    # After validation, the config application should not fail. Even if it fails,
    # we have no good of knowing what caused it. So we will return a generic
    # error message to the user after the redirect.
    if not config_schema:
        logger.error("Invalid state, no errors and no config schema repo %d", repo.id)
    else:
        # This will save the config in the DB and apply it asynchronously.
        apply_config.save_config_and_apply(
            repo=repo,
            config_data=config_schema.model_dump(),
            config_text=yaml_input,
            config_from_file=False,
            aviator_user_id=current_user.user.id,
        )
        AuditLog.capture_user_action(
            user=current_user.user,
            action=AuditLogAction.QUEUE_CONFIG_CHANGED,
            entity=AuditLogEntity.MERGE_QUEUE,
            target=repo.name,
        )

    # redirect back to the GET page with correct repo name
    # NOTE: Adding "/embed" as a workaround with iframe in the frontend
    return redirect("/embed" + url_for("validate_yaml_config_get", repo=repo.name))


@app.route("/github/validate-config", methods=["GET"])
@mq_redirect
@login_required
def validate_yaml_config_get() -> RouteResponse:
    repo = _selected_repo()
    if not repo:
        return redirect("/github/repos")

    # Always should the most recent config even if it is not applied yet.
    current_history: ConfigHistory | None = db.session.scalar(
        sa.select(ConfigHistory)
        .where(
            ConfigHistory.repo_id == repo.id,
            ConfigHistory.config_type == ConfigType.Main,
            ConfigHistory.failure_dismissed.is_(False),
        )
        .order_by(ConfigHistory.id.desc())
        .limit(1)
    )
    if current_history:
        yaml_input = current_history.config_text
    else:
        yaml_input = Configurator(repo).generate_new_yaml()

    return render_template(
        "forms/validate_config.html",
        yaml_input=yaml_input,
        repo=repo,
        repos=_all_repos(),
        config_status=_extract_config_status(current_history),
    )


@app.route("/api/v1/config", methods=["POST"])
@csrf.exempt
@token_auth.login_required
def api_config_update_post() -> RouteResponse:
    """
    Same as validate_yaml_config_post but for API.
    """
    user = token_auth.current_user().user
    yaml_input = request.get_data(as_text=True)
    repo_name = request.args.get("org", "") + "/" + request.args.get("repo", "")
    repo: GithubRepo | None = db.session.scalar(
        sa.select(GithubRepo).filter_by(
            account_id=user.account_id, name=repo_name
        )
    )
    if not repo:
        return abort(403, f"unable to find repository: {repo_name}")
    if not yaml_input:
        return abort(400, "YAML input cannot be empty")

    # parse and validate the input yaml
    config_schema, errors = apply_config.validate_config(
        repo.account_id, repo.id, yaml_input
    )

    if errors:
        return jsonify(success=False, errors=errors)

    # After validation, the config application should not fail. Even if it fails,
    # we have no good of knowing what caused it. So we will return a generic
    # error message to the user after the redirect.
    if not config_schema:
        logger.error("Invalid state, no errors and no config schema repo %d", repo.id)
    else:
        # This will save the config in the DB and apply it asynchronously.
        apply_config.save_config_and_apply(
            repo=repo,
            config_data=config_schema.model_dump(),
            config_text=yaml_input,
            config_from_file=False,
            aviator_user_id=user.id,
        )
        AuditLog.capture_user_action(
            user=user,
            action=AuditLogAction.QUEUE_CONFIG_CHANGED,
            entity=AuditLogEntity.MERGE_QUEUE,
            target=repo.name,
        )

    # redirect back to the GET page with correct repo name
    return jsonify(success=True)@app.route("/api/v1/config", methods=["GET"])
@token_auth.login_required
def api_config_get() -> RouteResponse:
    user = token_auth.current_user().user
    repo_name = request.args.get("org", "") + "/" + request.args.get("repo", "")
    repo: GithubRepo | None = db.session.execute(
        sa.select(GithubRepo).filter_by(
            account_id=user.account_id, name=repo_name
        )
    ).scalar_one_or_none()
    if not repo:
        return abort(403, f"unable to find repository: {repo_name}")
    return _get_current_config(repo)


def _get_current_config(repo: GithubRepo) -> str:
    current_history = db.session.execute(
        sa.select(ConfigHistory)
        .filter_by(repo_id=repo.id, config_type=ConfigType.Main)
        .order_by(ConfigHistory.id.desc())
    ).scalar_one_or_none()
    if current_history:
        return current_history.config_text

    return Configurator(repo).generate_new_yaml()


def _extract_config_status(config_history: ConfigHistory | None) -> str | None:
    """
    :return: status of the config application, one of "success", "failure", "pending" or None
    """
    if not config_history:
        return None
    if config_history.applied_at:
        return "success"
    if config_history.failed:
        return "failure"
    return "pending"


@app.route("/github/collaborators/billed")
@mq_redirect
@login_required
def github_billed_users() -> RouteResponse:
    repos = _active_repos()
    if not repos:
        return redirect("/github/repos")
    gusers: list[GithubUser] = db.session.execute(
        sa.select(GithubUser).filter_by(account_id=current_user.user.account_id)
    ).scalars().all()
    total_count = len([u for u in gusers])
    users = [u for u in gusers if u.billed]
    billed_count = len(users)
    return render_template(
        "github/users.html",
        gusers=users,
        total_count=total_count,
        billed_count=billed_count,
    )


@app.route("/github/queue/reset", methods=["POST"])
@mq_redirect
@login_required
def reset_queue() -> RouteResponse:
    repo_name = request.form.get("repo_name")
    account_id = current_user.user.account_id
    repo: GithubRepo | None = db.session.execute(
        sa.select(GithubRepo).filter_by(name=repo_name, account_id=account_id)
    ).scalar_one_or_none()
    success = False
    error = None
    if not repo or not repo.parallel_mode:
        error = "Invalid repo or parallel mode is inactive"
    elif not current_user.user.is_maintainer:
        error = "Admin or Maintainer privileges are required to reset the queue"
    else:
        queue.full_queue_reset(repo, StatusCode.MANUAL_RESET)
        success = True
        AuditLog.capture_user_action(
            user=current_user.user,
            action=AuditLogAction.QUEUE_RESET,
            entity=AuditLogEntity.MERGE_QUEUE,
            target=repo.name,
        )
    return jsonify(success=success, error=error)


@app.route("/github/queue/process-all", methods=["POST"])
@mq_redirect
@maintainer_required
def process_all_queue() -> RouteResponse:
    repo_name = request.form.get("repo_name")
    account_id = current_user.user.account_id
    repo: GithubRepo | None = db.session.execute(
        sa.select(GithubRepo).filter_by(name=repo_name, account_id=account_id)
    ).scalar_one_or_none()
    success = False
    error = None
    if not repo or not repo.is_no_queue_mode:
        error = "Invalid repo or not in no-queue mode"
    else:
        queue.process_unordered_async.delay(repo.id)
        success = True
    return jsonify(success=success, error=error)


@app.route("/github/auth/dev_token", methods=["GET", "POST"])
@mq_redirect
@admin_required
def auth_dev_token() -> RouteResponse:
    success = False
    error = ""

    allow_dev_token = capability.is_supported(
        current_user.user.account_id, Feature.GITHUB_DEV_TOKEN
    )
    if not allow_dev_token:
        error = "This feature is not enabled for this account. Contact support."
    elif request.method == "POST":
        token: str = request.form.get("dev_token")  # type: ignore[assignment]
        if connect.validate_access_token(token, current_user.user.account_id):
            access_token: AccessToken | None = db.session.execute(
                sa.select(AccessToken).filter_by(
                    account_id=current_user.user.account_id,
                )
            ).scalar_one_or_none()
            if not access_token or not connect.is_dev_token(access_token):
                # If the current token is not a PAT, create a new token. This way
                # we can keep the existing token as a secondary token.
                installation_id = access_token.installation_id if access_token else 0
                access_token = AccessToken(
                    account_id=current_user.user.account_id,
                    installation_id=installation_id,
                )
                db.session.add(access_token)
            access_token.token = token
            access_token.expiration_date = datetime.datetime(2050, 12, 12)
            access_token.is_valid = True
            # if the user is setting their dev token,
            # we should set this token as the access_token for all repos under their account_id
            repos: list[GithubRepo] = db.session.execute(
                sa.select(GithubRepo).filter_by(account_id=current_user.user.account_id)
            ).scalars().all()
            for r in repos:
                r.access_token_id = access_token.id
            db.session.commit()
            success = True
        else:
            error = "Unable to validate the access token"
    return render_template("forms/dev_token.html", success=success, error=error)


@app.route("/github/connect", methods=["GET"])
@mq_redirect
@login_required
def github_connect_view() -> RouteResponse:
    state = request.args.get("state", "")
    if state == "request":
        return render_template("dashboard/pending_request.html")
    elif state == "error":
        return render_template("dashboard/connect_error.html")
    return redirect("/github/repos")


@app.route("/repo/<string:org>/<string:repo>/pull/<string:number_str>")
@login_required
def repo_redirect(org: str, repo: str, number_str: str) -> RouteResponse:
    # A fallback route for the frontend links
    number = re.findall(r"\d+", number_str)[0]
    url = f"{app.config['GITHUB_BASE_URL']}/{org}/{repo}/pull/{number}"
    return redirect(url)


@app.route("/api/v1/github/users/csv")
@admin_required
def get_active_users_csv() -> RouteResponse:
    all_users: list[GithubUser] = db.session.scalars(
        sa.select(GithubUser)
        .filter(GithubUser.account_id == current_user.user.account_id)
        .filter(GithubUser.deleted.is_(False))
    ).all()
    user_ids = [u.id for u in all_users]
    latest_prs = _find_latest_pr_for_users(current_user.user.account_id, user_ids)

    csv_data = [("username", "github_database_id", "billed", "last_active")]
    for user in all_users:
        last_pr = latest_prs.get(user.id, None)
        last_pr_time = (
            last_pr.queued_at.isoformat() if last_pr and last_pr.queued_at else ""
        )
        if not last_pr_time and user.billed:
            logger.warning(
                "No PR found for a billed user",
                account_id=current_user.user.account_id,
                username=user.username,
                last_pr=last_pr,
            )
        csv_data.append(
            (
                user.username,
                str(user.gh_database_id),
                str(user.billed).lower(),
                last_pr_time,
            )
        )

    si = io.StringIO()
    writer = csv.writer(si)
    writer.writerows(csv_data)
    response = make_response(si.getvalue())  # nosemgrep
    response.headers["Content-Disposition"] = "attachment; filename=data.csv"
    response.headers["Content-type"] = "text/csv"
    return response


@app.route("/api/v1/github/users/user_integrations.csv")
@admin_required
def get_user_integrations_csv() -> RouteResponse:
    all_users: list[GithubUser] = db.session.scalars(
        sa.select(GithubUser).where(
            GithubUser.account_id == current_user.user.account_id,
            GithubUser.deleted.is_(False),
        ),
    ).all()

    csv_data = [
        (
            "internal_user_id",
            "internal_github_user_id",
            "github_username",
            "github_teams",
            "slack_username",
            "slack_user_id",
        ),
    ]

    for user in all_users:
        internal_user_id = str(user.aviator_user.id) if user.aviator_user else ""
        internal_github_user_id = str(user.id)
        github_username = user.username
        github_teams = sorted({team.slug for team in user.teams})
        slack_username = user.slack_user.username if user.slack_user else ""
        slack_user_id = user.slack_user.external_user_id if user.slack_user else ""
        csv_data.append(
            (
                internal_user_id,
                internal_github_user_id,
                github_username,
                " ".join(github_teams),
                slack_username,
                slack_user_id,
            ),
        )
    si = io.StringIO()
    writer = csv.writer(si)
    writer.writerows(csv_data)
    response = make_response(si.getvalue())  # nosemgrep
    response.headers["Content-Disposition"] = (
        "attachment; filename=user_integrations.csv"
    )
    response.headers["Content-type"] = "text/csv"
    return response


def _find_latest_pr_for_users(
    account_id: int, user_ids: list[int]
) -> dict[int, PullRequest]:
    six_months_ago = datetime.datetime.now() - datetime.timedelta(days=180)
    subquery = (
        sa.select(
            PullRequest.creator_id,
            sa.func.max(PullRequest.queued_at).label("max_queued_at"),
        )
        .where(PullRequest.creator_id.in_(user_ids))  # Filter by the given user_ids
        .filter(PullRequest.account_id == account_id)
        .filter(PullRequest.created > six_months_ago)
        .group_by(PullRequest.creator_id)
        .subquery()
    )
    query = sa.select(PullRequest).join(
        subquery,
        sa.and_(
            PullRequest.creator_id == subquery.c.creator_id,
            PullRequest.queued_at == subquery.c.max_queued_at,
        ),
    )
    result = db.session.execute(query).scalars().all()
    return {pr.creator_id: pr for pr in result}


def _get_all_db_tests() -> list[GithubTest]:
    repo = _selected_repo()
    if repo:
        return db.session.execute(
            sa.select(GithubTest)
            .filter_by(repo_id=repo.id)
            .order_by(GithubTest.id.asc())
        ).scalars().all()
    return []


def _all_repos() -> list[GithubRepo]:
    return db.session.execute(
        sa.select(GithubRepo)
        .filter_by(account_id=current_user.user.account_id)
        .order_by("id")
    ).scalars().all()


def _active_repos() -> list[GithubRepo]:
    return db.session.execute(
        sa.select(GithubRepo)
        .filter_by(account_id=current_user.user.account_id, active=True)
        .order_by("id")
    ).scalars().all()


def _selected_repo() -> GithubRepo | None:
    repos = _all_repos()
    repo_name = request.args.get("repo", "")
    if not repo_name:
        repo_name = request.cookies.get("repoName")
    if repo_name:
        repo_name = urllib.parse.unquote(repo_name)
    for repo in repos:
        if repo.name == repo_name:
            return repo
    if repos:
        return repos[0]
    return None


def _update_labels(
    repo: GithubRepo,
    labels: list[str],
    purpose: GithubLabelPurpose,
) -> None:
    dblabels = db.session.execute(
        sa.select(GithubLabel).filter_by(repo_id=repo.id, purpose=purpose)
    ).scalars().all()
    dblabel_names = [l.name for l in dblabels]
    for dblabel in dblabels:
        if dblabel.name not in labels:
            dblabel.deleted = True

    for label in labels:
        if label and label not in dblabel_names:
            new_label = GithubLabel(name=label, repo_id=repo.id, purpose=purpose)
            db.session.add(new_label)


def _update_branches(repo: GithubRepo, branches: list[str]) -> None:
    db_branches: list[BaseBranch] = db.session.execute(
        sa.select(BaseBranch).filter_by(repo_id=repo.id)
    ).scalars().all()
    db_branches_names = [b.name for b in db_branches]
    for db_branch in db_branches:
        if db_branch.name not in branches:
            db_branch.deleted = True

    for branch in branches:
        if branch and branch not in db_branches_names:
            new_branch = BaseBranch(name=branch, repo_id=repo.id)
            db.session.add(new_branch)


def set_repo_cookie(resp: Response, repo: GithubRepo) -> None:
    # default value is False -> see flask docs
    secure: bool = app.config.get("SESSION_COOKIE_SECURE")  # type: ignore[assignment]
    resp.set_cookie(
        key="repoName",
        value=repo.name if repo else "all",
        secure=secure,
    )