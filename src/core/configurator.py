from __future__ import annotations

import datetime
import difflib
from typing import Any

import structlog

from core import common, locks
from core.configurator_schema import (
    AutoUpdate,
    Configuration,
    ConfiguratorSchema,
    CustomRequiredChecks,
    Labels,
    MergeCommit,
    MergeMode,
    MergeRules,
    MergeStrategy,
    OverrideLabels,
    PreconditionMatch,
    PreconditionMatchType,
    Preconditions,
    TitleRegex,
    Validations,
)
from core.models import (
    AcceptableTestStatus,
    BaseBranch,
    ConfigHistory,
    ConfigType,
    GithubLabel,
    GithubLabelPurpose,
    GithubRepo,
    GithubTest,
    RegexConfig,
    TestStatus,
    Validation,
)
from flaky.spec import TestDeck
from main import app, db
from pilot.spec.scenario import Scenario
from schedule.models import ScheduledCron
from util.request_util import safe_str

logger = structlog.stdlib.get_logger()


class Configurator:
    rules: Any
    raw_scenarios: Any
    raw_testdeck: Any
    test_map: dict[str, GithubTest]
    repo_validations: list[Validation]

    def __init__(self, repo: GithubRepo) -> None:
        self.repo = repo
        self.parsed = False
        self.rules = {}
        self.raw_scenarios = []
        self.raw_testdeck = {}

        dbtests: list[GithubTest] = (
            GithubTest.query.filter_by(repo_id=self.repo.id)
            .order_by(GithubTest.id.asc())
            .all()
        )
        self.test_map = {test.name: test for test in dbtests}
        self.repo_validations = Validation.query.filter_by(repo_id=self.repo.id).all()

    @property
    def merge_rules(self) -> MergeRules:
        """
        ** should only call after `parse()` has set up `self.rules` **

        :return: the stored configurator merge rules
        """
        assert self.parsed, (
            "expected `configurator.parse_and_validate()` method to have been called"
        )
        return MergeRules.model_validate(self.rules)

    @property
    def testdeck(self) -> TestDeck | None:
        """
        ** should only call after `parse()` has set up `self.raw_testdeck` **

        :return: the stored configurator testdeck config
        """
        assert self.parsed, (
            "expected `configurator.parse_and_validate()` method to have been called"
        )
        return TestDeck.model_validate(self.raw_testdeck) if self.raw_testdeck else None

    @property
    def scenarios(self) -> list[Scenario]:
        """
        ** should only call after `parse()` has set up `self.raw_scenarios` **

        :return: the stored configurator scenarios
        """
        assert self.parsed, (
            "expected `configurator.parse_and_validate()` method to have been called"
        )
        return [Scenario.model_validate(scenario) for scenario in self.raw_scenarios]

    def parse_and_validate(self, config: dict) -> tuple[bool, str]:
        """
        take in a dict representation of the config read from some source
        and set up the configurator internal state

        :param config: the dict we want to parse into configurator format
        :return: true if the `config` was parsed successfully
        """
        if not config.get("merge_rules"):
            logger.info("Repo %d, no merge_rules found in config file", self.repo.id)
            return False, "No merge_rules found in config file"

        self.rules = config["merge_rules"]
        self.raw_scenarios = config.get("scenarios", [])
        self.raw_testdeck = config.get("testdeck", {})
        labels = self.rules.get("labels", {})
        if not labels or not labels.get("trigger"):
            logger.info(
                "Repo %d, no label for trigger found in config file", self.repo.id
            )
            return False, "No label found for trigger found in config file."
        if not isinstance(self.raw_scenarios, list):
            logger.info("Repo %d, scenarios was not a list", self.repo.id)
            return False, "Scenarios was not parsed correctly, expected list."
        if self.raw_testdeck and not isinstance(self.raw_testdeck, dict):
            logger.info("Repo %d, testdeck was not a dict", self.repo.id)
            return False, "Testdeck was not parsed correctly, expected dict."
        self.parsed = True
        return True, ""

    def update_config(self) -> None:
        """
        update database config objects associated with merge rules
        must run this function *after* setting up the configurator
        either manually or by calling the `parse()` function

        :return:
        """
        labels = self.rules.get("labels", {})
        trigger_label = safe_str(labels, "trigger")
        self.update_labels([trigger_label], GithubLabelPurpose.Queue)

        blocked_label = safe_str(labels, "merge_failed", "blocked")
        self.update_labels([blocked_label], GithubLabelPurpose.Blocked)

        skip_delete_label = safe_str(labels, "skip_delete_branch")
        if skip_delete_label:
            self.update_labels([skip_delete_label], GithubLabelPurpose.SkipDelete)

        self.update_branches()
        self.update_merge_strategy()
        self.update_preconditions()
        self.update_merge_commit()
        self.update_merge_mode()
        self.update_validations()
        self.update_auto_update()

        skip_line_label = safe_str(labels, "skip_line")
        if skip_line_label:
            self.update_labels([skip_line_label], GithubLabelPurpose.SkipLine)

        self.update_schedules()
        db.session.commit()

    def update_merge_strategy(self) -> None:
        if "merge_strategy" in self.rules:
            strategy = self.rules["merge_strategy"] or {}

            override_labels = strategy.get("override_labels", {})
            merge_label_squash = safe_str(override_labels, "squash")
            merge_label_merge = safe_str(override_labels, "merge")
            merge_label_rebase = safe_str(override_labels, "rebase")
            if merge_label_squash:
                self.update_labels([merge_label_squash], GithubLabelPurpose.Squash)
            if merge_label_merge:
                self.update_labels([merge_label_merge], GithubLabelPurpose.Merge)
            if merge_label_rebase:
                self.update_labels([merge_label_rebase], GithubLabelPurpose.Rebase)

    def validate_acceptable_statuses(self, statuses: str) -> list[str]:
        acceptable_statuses = []
        for s in statuses:
            status = s.strip()
            try:
                # Make sure the status is valid
                TestStatus(status)
                acceptable_statuses.append(status)
            except Exception:
                logger.error(
                    "found unacceptable status %s for repo %d", status, self.repo.id
                )
        return acceptable_statuses

    def update_preconditions(self) -> None:
        if "preconditions" in self.rules:
            conditions = self.rules["preconditions"] or {}
            required_checks_union = conditions.get("required_checks") or []
            self.update_required_checks(required_checks_union, for_override=False)

    def update_merge_commit(self) -> None:
        if "merge_commit" in self.rules:
            commit_config = self.rules["merge_commit"] or {}
            if "apply_title_regexes" in commit_config:
                # Remove all configs before adding current ones
                RegexConfig.query.filter_by(repo_id=self.repo.id).delete()
                regex_list = commit_config.get("apply_title_regexes") or []
                for title_regex in regex_list:
                    regex_config = RegexConfig(
                        repo_id=self.repo.id,
                        pattern=title_regex.get("pattern"),
                        replace=title_regex.get("replace"),
                    )
                    db.session.add(regex_config)

    def update_merge_mode(self) -> None:
        if "merge_mode" in self.rules:
            merge_mode = self.rules["merge_mode"] or {}
            parallel_mode = merge_mode.get("parallel_mode")
            if parallel_mode:
                if "override_required_checks" in parallel_mode:
                    # Intentionally not using the default value of [] for get() since
                    # override_required_checks can be set to None even though the key
                    # exists.
                    override_checks_union = (
                        parallel_mode.get("override_required_checks") or []
                    )
                    self.update_required_checks(
                        override_checks_union, for_override=True
                    )
                else:
                    for dbtest in self.test_map.values():
                        dbtest.is_required_bot_pr_check = False
                stuck_pr_label = safe_str(parallel_mode, "stuck_pr_label")
                stuck_pr_labels = [stuck_pr_label] if stuck_pr_label else []
                self.update_labels(stuck_pr_labels, GithubLabelPurpose.ParallelStuck)

                parallel_barrier_label = safe_str(
                    parallel_mode, "block_parallel_builds_label"
                )
                parallel_barrier_labels = (
                    [parallel_barrier_label] if parallel_barrier_label else []
                )
                self.update_labels(
                    parallel_barrier_labels, GithubLabelPurpose.ParallelBarrier
                )

    def update_validations(self) -> None:
        preconditions = self.rules.get("preconditions") or {}
        regex_validations: list[Any] = preconditions.get("validations") or []
        if not regex_validations:
            if self.repo_validations:
                for (
                    existing_validation
                ) in self.repo_validations:  # Delete all regex_validations
                    existing_validation.deleted = True
            return

        new_validations = {}
        for regex_validation in regex_validations:
            regex = regex_validation.get("match", {}).get("regex", [])
            if regex:
                if isinstance(regex, str):
                    regex = [regex]
                new_validations[regex_validation.get("name")] = regex
        # Go through each existing validation in the DB
        # and remove any name-regex pair that does not exist in the new set.
        for existing_validation in self.repo_validations:
            if existing_validation.value not in new_validations.get(
                existing_validation.name, []
            ):
                existing_validation.deleted = True
        for regex_validation in regex_validations:
            regex = regex_validation.get("match", {}).get("regex", [])
            if regex:
                if isinstance(regex, str):
                    regex = [regex]
                regex_validations = (
                    Validation.query.filter_by(
                        repo_id=self.repo.id,
                        name=regex_validation.get("name"),
                    )
                    .with_entities(Validation.value)
                    .all()
                )
                # Add new validation if not already exist
                for validation in regex:
                    if (validation,) not in regex_validations:
                        new_validation = Validation(
                            value=validation,
                            name=regex_validation.get("name"),
                            type=regex_validation.get("match").get("type").lower(),
                            repo_id=self.repo.id,
                        )
                        db.session.add(new_validation)

    def update_auto_update(self) -> None:
        if "auto_update" in self.rules:
            auto_update_config = self.rules["auto_update"] or {}
            as_label = safe_str(auto_update_config, "label")
            auto_sync_labels = [as_label] if as_label else []
            self.update_labels(auto_sync_labels, GithubLabelPurpose.AutoSync)

    def update_labels(self, labels: list[str], purpose: GithubLabelPurpose) -> None:
        dblabels = GithubLabel.query.filter_by(
            repo_id=self.repo.id, purpose=purpose
        ).all()
        dblabel_names = [l.name for l in dblabels]
        for dblabel in dblabels:
            if dblabel.name not in labels:
                dblabel.deleted = True

        for label in labels:
            if label and label not in dblabel_names:
                new_label = GithubLabel(
                    name=label,
                    repo_id=self.repo.id,
                    purpose=purpose,
                )
                db.session.add(new_label)

    def update_branches(self) -> None:
        base_branches = self.rules.get("base_branches") or []
        db_branches = BaseBranch.query.filter_by(repo_id=self.repo.id).all()
        db_branch_names = [b.name for b in db_branches]
        for db_branch in db_branches:
            if db_branch.name not in base_branches:
                db_branch.deleted = True

        for branch in base_branches:
            if branch and branch not in db_branch_names:
                new_branch = BaseBranch(name=branch, repo_id=self.repo.id)
                db.session.add(new_branch)

    def update_schedules(self) -> None:
        """
        Find any schedules in these scenarios and ensure that they are in the DB.
        """
        if not self.raw_scenarios or not self.scenarios:
            return
        schedules = set()
        for scenario in self.scenarios:
            if not scenario.trigger.schedule or not scenario.trigger.schedule.cron_utc:
                continue
            schedules.add(scenario.trigger.schedule.cron_utc)

        db_schedules = ScheduledCron.query.filter_by(repo_id=self.repo.id).all()
        for db_schedule in db_schedules:
            if db_schedule.cron_utc not in schedules:
                db_schedule.deleted = True
            else:
                schedules.remove(db_schedule.cron_utc)
        for schedule in schedules:
            new_schedule = ScheduledCron(cron_utc=schedule, repo_id=self.repo.id)
            db.session.add(new_schedule)
        db.session.commit()

    def update_config_history(
        self,
        applied: bool,
        *,
        scenarios: list[Scenario],
        merge_rules: MergeRules,
        testdeck: TestDeck | None = None,
        aviator_user_id: int | None = None,
        github_author_id: int | None = None,
        head_commit_sha: str | None = None,
        text: str | None = None,
    ) -> ConfigHistory | None:
        """
        add a record to the config history if a change has been made

        :param text: the current YAML string representation of the config
        :param aviator_user_id: the user making the change if from the UI
        :param github_author_id: the user authoring the change if from GitHub
        :param head_commit_sha: the sha where the change happened if from GitHub
        :param merge_rules: the current config merge rules - if not supplied, will be autogenerated
        :param scenarios: the current config scenarios
        :param testdeck: the current config testdeck
        :param applied: whether the config was already applied

        :return: the ConfigHistory object if a change was made, None otherwise
        """
        logger.info(
            "Updating config history called for repo %d applied %s",
            self.repo.id,
            applied,
        )
        if not aviator_user_id and not github_author_id:
            logger.error(
                "config history must be associated with either an aviator or github user"
            )
        lock_name = "config-history-repo-%d" % self.repo.id
        with locks.lock("config-history", lock_name, expire=60):
            configurator_schema = ConfiguratorSchema(
                version="1.1.0",
                merge_rules=merge_rules,
                scenarios=scenarios,
                testdeck=testdeck,
            )
            configuration = Configuration(merge_rules=merge_rules, scenarios=scenarios)
            history = ConfigHistory(
                repo_id=self.repo.id,
                account_id=self.repo.account_id,
                config_type=ConfigType.Main,
                aviator_user_id=aviator_user_id,
                github_author_id=github_author_id,
                commit_sha=head_commit_sha,
                config_data=configuration.model_dump(),
                config_text=text
                or configurator_schema.toyaml(exclude_none=True, exclude_unset=True),
                applied_at=datetime.datetime.now(datetime.UTC) if applied else None,
            )
            if not self.has_config_changed(potential_change=history):
                logger.info(
                    "no change was found in the config for repo %d", self.repo.id
                )
                return None
            db.session.add(history)
            db.session.commit()
            return history

    def current_testdeck(self) -> TestDeck | None:
        config_history_record: None | (ConfigHistory) = self.repo.current_config_history
        if not config_history_record:
            return None
        config = Configuration.model_validate(config_history_record.config_data)
        return config.testdeck

    def current_scenarios(self) -> list[Scenario]:
        """
        Get the scenarios from the latest config history record in db

        :return: the latest scenarios
        """
        config_history_record: None | (ConfigHistory) = self.repo.current_config_history
        if not config_history_record:
            return []
        config = Configuration.model_validate(config_history_record.config_data)
        return config.scenarios

    def generate_merge_rules(self) -> MergeRules:
        """
        Build merge rules from various related db objects (labels, validations, etc.).
        This should only be used if no config history exist. Generating rules through
        this method otherwise is likely to drop some of the rules in the config.

        currently, the incoming config changes either updates the full config
        in which case all of various values get updated via `update_config()` function
        or the db objects themselves get updated and then the latest merge rules needs
        to be built after the individual database changes have been committed

        :return: the current merge rules
        """
        # read through the configs currently set in DB
        github_labels = GithubLabel.query.filter_by(
            repo_id=self.repo.id, deleted=False
        ).all()
        github_labels_info = {label.purpose: label.name for label in github_labels}
        dbchecks = GithubTest.query.filter_by(
            repo_id=self.repo.id, enabled=True, ignore=False
        ).all()
        precondition_match, validations = None, []
        required_checks = []
        for check in dbchecks:
            # mypy doesn't know about AcceptableTestStatus.query
            # since it doesn't inherit from BaseModel
            ats = AcceptableTestStatus.query.filter_by(
                github_test_id=check.id, for_override=False
            ).all()
            if ats:
                statuses = [s.status.value for s in ats]
                required_checks.append(
                    CustomRequiredChecks(name=check.name, acceptable_statuses=statuses)
                )
            else:
                required_checks.append(check.name)
        labels = Labels(
            trigger=github_labels_info.get(
                GithubLabelPurpose.Queue, app.config["DEFAULT_READY_LABEL"]
            ),
            skip_line=github_labels_info.get(
                GithubLabelPurpose.SkipLine, app.config["DEFAULT_SKIP_LABEL"]
            ),
            merge_failed=github_labels_info.get(
                GithubLabelPurpose.Blocked, app.config["DEFAULT_FAILED_LABEL"]
            ),
            skip_delete_branch=github_labels_info.get(
                GithubLabelPurpose.SkipDelete, ""
            ),
        )
        if self.repo_validations:
            existed_data: dict[Any, Any] = {}
            for existing_validation in self.repo_validations:
                regex_list = [existing_validation.value]
                if existed_data.get(
                    existing_validation.name
                ):  # check if validation name is already in existing data
                    regex_list.extend(existed_data[existing_validation.name]["regex"])
                existed_data[existing_validation.name] = {"regex": regex_list}
                precondition_match = PreconditionMatch(
                    type=PreconditionMatchType(existing_validation.type.value),
                    regex=regex_list,
                )
                validations_name = Validations(
                    name=existing_validation.name, match=precondition_match
                )
                existed_data[existing_validation.name]["validation_object"] = (
                    validations_name
                )
            for new_validations in existed_data.values():
                validations.append(new_validations.get("validation_object"))
        preconditions = Preconditions(
            validations=validations,
            required_checks=required_checks,  # type: ignore[arg-type]
        )
        auto_update = AutoUpdate(
            label=github_labels_info.get(GithubLabelPurpose.AutoSync, ""),
        )
        apply_title_regexes = None
        regex_configs = RegexConfig.query.filter_by(repo_id=self.repo.id).all()
        if regex_configs:
            apply_title_regexes = []
            for regex_config in regex_configs:
                apply_title_regexes.append(
                    TitleRegex(
                        pattern=regex_config.pattern, replace=regex_config.replace
                    )
                )
        merge_commit = MergeCommit(
            apply_title_regexes=apply_title_regexes,
        )

        merge_mode = MergeMode(type="default")

        override_labels = OverrideLabels(
            squash=github_labels_info.get(GithubLabelPurpose.Squash, ""),
            merge=github_labels_info.get(GithubLabelPurpose.Merge, ""),
            rebase=github_labels_info.get(GithubLabelPurpose.Rebase, ""),
        )
        merge_strategy = MergeStrategy(
            override_labels=override_labels,
        )
        base_branches = BaseBranch.query.filter_by(repo_id=self.repo.id).all()

        return MergeRules(
            labels=labels,
            preconditions=preconditions,
            auto_update=auto_update,
            merge_commit=merge_commit,
            merge_mode=merge_mode,
            merge_strategy=merge_strategy,
            base_branches=[branch.name for branch in base_branches],
        )

    def generate_new_yaml(self) -> str:
        """
        This should only be used if no config history exist. Exporting config using
        this method would likely drop some information if the config has already been generated.
        """
        configurator_schema = ConfiguratorSchema(
            merge_rules=self.generate_merge_rules(),
            scenarios=self.current_scenarios(),
            testdeck=self.current_testdeck(),
        )
        return configurator_schema.toyaml(exclude_none=True, exclude_unset=True)

    def has_config_changed(self, *, potential_change: ConfigHistory) -> bool:
        latest_config_record: None | (ConfigHistory) = self.repo.current_config_history
        # if there is no latest record then the config has always changed
        if not latest_config_record:
            return True
        # if we switch from UI to GitHub or vice versa then config has always changed
        if latest_config_record.aviator_user_id and potential_change.github_author_id:
            return True
        if latest_config_record.github_author_id and potential_change.aviator_user_id:
            return True
        differ = difflib.Differ()
        diff = differ.compare(
            latest_config_record.config_text.splitlines(),
            potential_change.config_text.splitlines(),
        )
        diff_list = list(diff)
        added = list(filter(lambda a: a.startswith("+ "), diff_list))
        removed = list(filter(lambda a: a.startswith("- "), diff_list))
        if not added and not removed:
            return False
        return True

    def update_required_checks(
        self,
        checks_union: list[str | CustomRequiredChecks],
        for_override: bool,
    ) -> None:
        simple_checks = []
        customized_checks = []
        check_names = []

        # parse names from union of checks
        for check in checks_union:
            if isinstance(check, str):
                check_name = check.strip()
                simple_checks.append(check_name)
            else:
                # TODO: mypy thinks this is impossible? is this dead code?
                check_name = check.get("name").strip()  # type: ignore[attr-defined]
                customized_checks.append(check)
            check_names.append(check_name.strip())

        # add any new GithubTests and set ignore OR is_required_bot_pr_check flag
        for dbtest in self.test_map.values():
            if for_override:
                dbtest.is_required_bot_pr_check = dbtest.name in check_names
            else:
                dbtest.ignore = dbtest.name not in check_names
        for check in check_names:
            if check not in self.test_map and not common.is_av_custom_check(check):
                if for_override:
                    test = GithubTest(
                        name=check, repo_id=self.repo.id, is_required_bot_pr_check=True
                    )
                else:
                    test = GithubTest(name=check, repo_id=self.repo.id, ignore=False)
                db.session.add(test)
                self.test_map[check] = test

        # if test previously existed and is now a simple check, remove any existing acceptable statuses
        for check in simple_checks:
            if check in self.test_map:
                test = self.test_map[check]
                AcceptableTestStatus.query.filter_by(
                    github_test_id=test.id,
                    for_override=for_override,
                ).delete()

        db.session.commit()

        # update statuses for any customized checks
        for check in customized_checks:
            # TODO: mypy thinks this is impossible? is this dead code?
            check_name = check.get("name").strip()  # type: ignore[attr-defined]
            statuses = check.get("acceptable_statuses")  # type: ignore[attr-defined]
            acceptable_statuses = self.validate_acceptable_statuses(statuses)
            gh_test = self.test_map[check_name]
            # remove any current acceptable test statuses that are no longer in the config
            all_ats = AcceptableTestStatus.query.filter_by(
                github_test_id=gh_test.id,
                for_override=for_override,
            ).all()
            for current_ats in all_ats:
                if current_ats.status not in acceptable_statuses:
                    db.session.delete(current_ats)
                else:
                    acceptable_statuses.remove(current_ats.status)
            for s in acceptable_statuses:
                ats = AcceptableTestStatus(
                    github_test_id=gh_test.id,
                    status=TestStatus(s),
                    for_override=for_override,
                )
                db.session.add(ats)
