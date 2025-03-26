from __future__ import annotations

import enum

from schema import StrictBaseModel


class MetricSeverity(enum.Enum):
    IGNORE = "ignore"
    INFO = "info"
    CRITICAL = "critical"


class TechDebtCategory(enum.Enum):
    SECURITY = "security"
    MIGRATION = "migration"
    TEST_QUALITY = "test_quality"
    COMPLEXITY = "complexity"
    CODE_SMELL = "code_smell"
    TODO = "todo"
    UNCATEGORIZED = "uncategorized"
    DEPRECATION = "deprecation"


class TechDebtPerFileData(StrictBaseModel):
    task_count_per_category: dict[TechDebtCategory, int]
    metric_count_per_category_per_severity: dict[
        TechDebtCategory,
        dict[MetricSeverity, int],
    ]
    tech_debt_score_per_category: dict[TechDebtCategory, float]

    # Number of modifications per week. 52 elements. 0th entry is this week.
    # i-th entry is i weeks ago.
    churn_history_52w: list[int]
    bugfix_count_history_52w: list[int]
    tech_debt_score_52w: list[float]
    tech_debt_score_per_category_52w: dict[TechDebtCategory, list[float]]

    @classmethod
    def empty(cls) -> TechDebtPerFileData:
        return TechDebtPerFileData(
            task_count_per_category={},
            metric_count_per_category_per_severity={},
            tech_debt_score_per_category={},
            churn_history_52w=[0] * 52,
            bugfix_count_history_52w=[0] * 52,
            tech_debt_score_52w=[0.0] * 52,
            tech_debt_score_per_category_52w={
                category: [0] * 52 for category in TechDebtCategory
            },
        )


class TechDebtPerDirData(StrictBaseModel):
    tech_debt_score_per_category_52w: dict[TechDebtCategory, list[float]]
    task_count_per_category_52w: dict[TechDebtCategory, list[int]]
    task_count_per_team: dict[int, int]

    @classmethod
    def empty(cls) -> TechDebtPerDirData:
        return cls(
            tech_debt_score_per_category_52w={
                category: [0] * 52 for category in TechDebtCategory
            },
            task_count_per_category_52w={
                category: [0] * 52 for category in TechDebtCategory
            },
            task_count_per_team={},
        )
