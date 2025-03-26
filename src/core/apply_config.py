from __future__ import annotations

import dataclasses
import datetime
import enum
import re

import structlog
from pydantic import ValidationError

from auth.models import User
from core import common, github_label, locks, pygithub
from core.configurator import Configurator
from core.configurator_schema import ConfiguratorSchema
from core.models import ConfigHistory, GithubRepo
from main import celery, db, redis_client
from slackhook import notify
from util.time_util import FIFTEEN_MINUTES_SECONDS, ONE_DAY_SECONDS, ONE_HOUR_SECONDS
from webhooks import controller as webhook_controller

AV_CONFIG_FILE = ".aviator/config.yml"
MQ_CONFIG_FILE = ".mergequeue/config.yml"
CONFIG_FILES = [AV_CONFIG_FILE, MQ_CONFIG_FILE]
logger = structlog.stdlib.get_logger()


def apply_config_from_onboarding(
    *,
    account_id: int,
    repo_name: str,
    text: str,
    user: User,
) -> None:
    repo = common.get_repo_for_account(account_id, repo_name)
    if not repo:
        return

    config_schema, errors = validate_config(repo.account_id, repo.id, text)
    if errors or not config_schema:
        logger.error(
            "Invalid config schema detected for account %d repo %d", account_id, repo.id
        )
        return

    repo.active = True
    common.pause_repo(
        actor=user, repo=repo, paused=False
    )  # this internally does db.commit
    github_label.create_default_labels.delay(repo.id)

    save_config_and_apply(
        repo=repo,
        config_data=config_schema.model_dump(),
        config_text=text,
        config_from_file=False,
        aviator_user_id=user.id,
    )


@celery.task
def apply_config_from_gh_file(
    *,
    repo_name: str,
    author_id: int | None = None,
    commit_sha: str | None = None,
    aviator_user_id: int | None = None,
) -> None:
    """
    We read the config from the Github repo, validate, and then apply the valid config

    :param repo_name: the repo name config will be applied to
    :param author_id: the GitHub user that authored commit if there is one
    :param commit_sha: the commit that made the change if there is one
    :param aviator_user_id: the aviator user that changed the config if there is one
    :return: Nothing
    """
    repo = common.get_repo_by_name(repo_name, require_billing_active=False)
    if not repo:
        return
    access_token, client = common.get_client(repo)
    if not client:
        return

    try:
        # since we input a path to a file we know that this will
        # return just a ContentFile instead of a List[ContentFile]
        contents = client.repo.get_contents(AV_CONFIG_FILE)
        assert isinstance(contents, pygithub.ContentFile), (
            f"expected config file contents to be ContentFile, got {type(contents)}"
        )
    except (pygithub.UnknownObjectException, pygithub.GithubException) as e:
        contents = None
        logger.info("Repo %d, no config file found at %s", repo.id, AV_CONFIG_FILE)

    if not contents:
        try:
            contents = client.repo.get_contents(MQ_CONFIG_FILE)
            assert isinstance(contents, pygithub.ContentFile), (
                f"expected config file contents to be ContentFile, got {type(contents)}"
            )
        except (pygithub.UnknownObjectException, pygithub.GithubException) as e:
            logger.info("Repo %d, no config file found at %s", repo.id, MQ_CONFIG_FILE)
            repo.configurations_from_file = False
            db.session.commit()
            return

    text = contents.decoded_content.decode("utf-8")
    config_schema, errors = validate_config(repo.account_id, repo.id, text)
    if errors or not config_schema:
        logger.info("Invalid config file detected for repo %d", repo.id)
        repo.configurations_from_file = False
        db.session.commit()
        return
    save_config_and_apply(
        repo=repo,
        config_data=config_schema.model_dump(),
        config_text=text,
        config_from_file=True,
        aviator_user_id=aviator_user_id,
        github_author_id=author_id,
        head_commit_sha=commit_sha,
    )


def validate_config(
    account_id: int, repo_id: int, config_text: str
) -> tuple[ConfiguratorSchema | None, list[str]]:
    """
    We try and parse the config text:
    - the first return value is the config schema if parsing was successful
    - the second return value is the list of errors if parsing was unsuccessful

    :param account_id: the account associated with this config.
    :param repo_id: the repo associated with this config. Note: this value is 0 if we are validating the config during onboarding.
    :param config_text: the config we want to apply
    :return: the configuration schema from the config text and an empty list if there were no errors
        otherwise we return None for the config schema and the list of errors found
    """
    # try parsing the text into the schema to see if we validate against the schema
    try:
        configurator_schema = ConfiguratorSchema.parse_yaml(config_text)
    except ValidationError as exc:
        logger.info(
            "Invalid config file",
            account_id=account_id,
            repo_id=repo_id,
            exc_info=exc,
        )
        return None, get_config_file_errors_list(exc)
    except Exception as exc:
        logger.info("Invalid config", account_id=account_id, repo_id=repo_id, exc=exc)
        return None, [str(exc)]

    # confirm that validations are set up correctly if present in schema
    if (
        configurator_schema.merge_rules.preconditions
        and configurator_schema.merge_rules.preconditions.validations
    ):
        errors: list[str] = []
        regex_expressions = []
        for validation in configurator_schema.merge_rules.preconditions.validations:
            regex = validation.match.regex
            if regex:
                if isinstance(regex, str):
                    regex = [regex]
                regex_expressions.extend(regex)
        for regex in regex_expressions:
            try:
                re.compile(regex)
            except re.error as e:
                errors.append(e.msg)
        if errors:
            logger.info(
                "Invalid config file detected for account %d repo %d errors: %s",
                account_id,
                repo_id,
                errors,
            )
            return None, errors

    repo: GithubRepo | None = GithubRepo.get_by_id(repo_id)
    if not repo:
        logger.error(
            "Repo not found by id lookup during config validation", repo_id=repo_id
        )
        return None, ["Invalid repository, not found."]
    try:
        configurator = Configurator(repo)
        cfg_valid, errmsg = configurator.parse_and_validate(
            configurator_schema.model_dump()
        )
        if not cfg_valid:
            logger.info(
                "Failed to validate config for repo.", errmsg=errmsg, repo_id=repo_id
            )
            return None, [errmsg]
    except Exception as exc:
        logger.error("Failed to validate config")
        return None, [str(exc)]

    return configurator_schema, []


def get_config_file_errors_list(e: ValidationError) -> list[str]:
    errors = []
    for error in e.errors():
        if error.get("ctx", {}).get("problem"):
            errors.append(
                str(error["ctx"]["problem"]) + " " + str(error["ctx"]["context_mark"])
            )
        else:
            loc = " -> ".join(
                f"item {x}" if isinstance(x, int) else x for x in error["loc"]
            )
            errors.append(f"{loc}: {error['msg']}")
    return errors


def get_config_status_key(repo_id: int) -> str:
    return f"apply-config-status-{repo_id}"


class MergeQueueConfigApplyStatus(enum.Enum):
    QUEUED = "queued"
    NO_CHANGE = "no_change"
    INVALID_CONFIG = "invalid_config"


@dataclasses.dataclass(frozen=True)
class MergeQueueConfigApplyResult:
    status: MergeQueueConfigApplyStatus
    errors: list[str]
    history: ConfigHistory | None = None


def save_config_and_apply(
    *,
    repo: GithubRepo,
    config_data: dict,
    config_text: str,
    config_from_file: bool,
    github_author_id: int | None = None,
    head_commit_sha: str | None = None,
    aviator_user_id: int | None = None,
) -> MergeQueueConfigApplyResult:
    config_schema, errors = validate_config(repo.account_id, repo.id, config_text)
    if errors or not config_schema:
        logger.error(
            "Invalid config schema detected.",
            account_id=repo.account_id,
            repo_id=repo.id,
        )
        return MergeQueueConfigApplyResult(
            status=MergeQueueConfigApplyStatus.INVALID_CONFIG, errors=errors
        )

    try:
        logger.info("Saving config for repo %d", repo.id)
        configurator = Configurator(repo)
        cfg_valid, errmsg = configurator.parse_and_validate(config_data)
        if not cfg_valid:
            logger.error(
                "Failed to apply config for repo.", repo_id=repo.id, errmsg=errmsg
            )
            return MergeQueueConfigApplyResult(
                status=MergeQueueConfigApplyStatus.INVALID_CONFIG, errors=[errmsg]
            )

        # For now, store the config history in DB as not applied yet. This will be
        # applied in the background using locks.
        history = configurator.update_config_history(
            applied=False,
            aviator_user_id=aviator_user_id,
            github_author_id=github_author_id,
            head_commit_sha=head_commit_sha,
            # these are the *parsed* rules and scenarios
            merge_rules=configurator.merge_rules,
            scenarios=configurator.scenarios,
            testdeck=configurator.testdeck,
            text=config_text,
        )
        if not history:
            # There's no change in the config.
            return MergeQueueConfigApplyResult(
                status=MergeQueueConfigApplyStatus.NO_CHANGE, errors=[]
            )

        apply_config_async.delay(history.id, config_data, config_from_file)
        return MergeQueueConfigApplyResult(
            status=MergeQueueConfigApplyStatus.QUEUED, errors=[], history=history
        )
    except Exception as e:
        logger.error("failed to update config for repo", repo_id=repo.id, exc_info=e)
        raise e


@celery.task
def apply_config_async(
    config_id: int, config_data: dict, config_from_file: bool
) -> None:
    config_history = ConfigHistory.get_by_id_x(config_id)

    if config_history.applied_at:
        raise Exception(f"ConfigHistory {config_id} has already been applied")

    repo = config_history.repo
    with locks.for_repo(repo, expire=90):
        # make sure we have the latest repo object before processing
        db.session.refresh(repo)
        configurator = Configurator(repo)
        cfg_valid, errmsg = configurator.parse_and_validate(config_data)
        if not cfg_valid:
            logger.error("Config failed validation.", errmsg=errmsg, repo_id=repo.id)
            config_history.failed = True
            config_history.failure_reason = errmsg
            db.session.commit()
            raise Exception("Failed to apply config for repo %d", repo.id)

        configurator.update_config()
        repo.configurations_from_file = config_from_file
        config_history.applied_at = datetime.datetime.now(datetime.UTC)
        db.session.commit()
        logger.info(
            "Finished applying config for repo %d config id %d", repo.id, config_id
        )
        # BAD: find some better way of structuring dependencies
        from core import queue

        webhook_controller.call_webhook_for_config_change.delay(config_history.id)
        notify.notify_config_change.delay(config_history.id)
        queue.config_consistency_check.delay(repo.id)
        if repo.is_no_queue_mode:
            # When someone updates the config to a non-queue mode,
            # we need to trigger a re-process to clear out the existing queue.
            queue.process_unordered_async.delay(repo.id)
