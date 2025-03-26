from __future__ import annotations

import difflib

from core.models import ConfigHistory, ConfigType


def get_diff_by_history(config_history: ConfigHistory) -> str:
    prev_record: ConfigHistory | None = (
        ConfigHistory.query.filter_by(
            repo_id=config_history.repo_id, config_type=ConfigType.Main
        )
        .filter(ConfigHistory.applied_at.is_(not None))
        .filter(ConfigHistory.id < config_history.id)
        .order_by(ConfigHistory.created.desc())
        .first()
    )
    if not prev_record:
        return ""
    return generate_diff(prev_record, config_history)


def generate_diff(old_config: ConfigHistory, new_config: ConfigHistory) -> str:
    differ = difflib.Differ()
    diff = differ.compare(
        old_config.config_text.splitlines(), new_config.config_text.splitlines()
    )
    return "\n".join(filter(lambda a: a.startswith("+ ") or a.startswith("- "), diff))