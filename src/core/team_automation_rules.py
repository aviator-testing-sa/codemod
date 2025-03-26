from __future__ import annotations

import sqlalchemy as sa

from core.models import GithubTeam, TeamAutomationRule, TeamAutomationRuleMapping
from main import db


def update_automation_rule_mappings_for_team(team: GithubTeam) -> None:
    team_rules = db.session.scalars(
        sa.select(TeamAutomationRule).where(
            TeamAutomationRule.github_team_id == team.id,
        ),
    ).all()
    _propagete_from_parent_teams(team, team_rules)
    _propagate_to_child_teams(team, team_rules)
    db.session.commit()


def _propagete_from_parent_teams(
    team: GithubTeam,
    team_rules: list[TeamAutomationRule],
) -> None:
    existing_mappings = db.session.scalars(
        sa.select(TeamAutomationRuleMapping).where(
            TeamAutomationRuleMapping.github_team_id == team.id,
        ),
    ).all()
    existing_rule_ids = {
        mapping.team_automation_rule_id for mapping in existing_mappings
    }

    # Adjust mappings of this team.
    parent_teams_cte = (
        sa.select(GithubTeam)
        .where(GithubTeam.id == team.id)
        .cte("parentteams", recursive=True)
    )
    parent_teams_cte = parent_teams_cte.union_all(
        sa.select(GithubTeam).join(
            parent_teams_cte,
            GithubTeam.id == parent_teams_cte.c.parent_id,
        ),
    )
    inheritable_rule_ids = db.session.scalars(
        sa.select(TeamAutomationRule.id).where(
            TeamAutomationRule.inheritable,
            TeamAutomationRule.github_team_id.in_(sa.select(parent_teams_cte.c.id)),
        ),
    )
    all_rule_ids = {rule.id for rule in team_rules} | set(inheritable_rule_ids)
    for mapping in existing_mappings:
        if mapping.team_automation_rule_id not in all_rule_ids:
            db.session.delete(mapping)
    for rule_id in all_rule_ids:
        if rule_id not in existing_rule_ids:
            db.session.add(
                TeamAutomationRuleMapping(
                    github_team_id=team.id,
                    team_automation_rule_id=rule_id,
                ),
            )


def _propagate_to_child_teams(
    team: GithubTeam,
    team_rules: list[TeamAutomationRule],
) -> None:
    subteams_cte = (
        sa.select(GithubTeam)
        .where(GithubTeam.id == team.id)
        .cte("subteams", recursive=True)
    )
    subteams_cte = subteams_cte.union_all(
        sa.select(GithubTeam).join(
            subteams_cte,
            GithubTeam.parent_id == subteams_cte.c.id,
        ),
    )
    child_teams = db.session.scalars(
        sa.select(GithubTeam).where(
            GithubTeam.id.in_(sa.select(subteams_cte.c.id)),
            GithubTeam.id != team.id,
        ),
    ).all()
    existing_mappings = db.session.scalars(
        sa.select(TeamAutomationRuleMapping).where(
            TeamAutomationRuleMapping.github_team_id
            == sa.func.any([t.id for t in child_teams]),
            TeamAutomationRuleMapping.team_automation_rule_id
            == sa.func.any([r.id for r in team_rules]),
        ),
    ).all()
    existing_pairs = {
        (mapping.github_team_id, mapping.team_automation_rule_id)
        for mapping in existing_mappings
    }
    for team_rule in team_rules:
        if not team_rule.inheritable:
            # Remove mappings for non-inheritable rules.
            for mapping in existing_mappings:
                if mapping.team_automation_rule_id == team_rule.id:
                    db.session.delete(mapping)
        else:
            # Add mappings for inheritable rules.
            for child_team in child_teams:
                if (child_team.id, team_rule.id) not in existing_pairs:
                    db.session.add(
                        TeamAutomationRuleMapping(
                            github_team_id=child_team.id,
                            team_automation_rule_id=team_rule.id,
                        ),
                    )
