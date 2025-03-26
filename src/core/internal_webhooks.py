from __future__ import annotations

from typing import Any

import structlog

from auth.models import Account
from core.models import BotPr, GithubRepo, PullRequest
from main import app, celery
from util import req

logger = structlog.stdlib.get_logger()


@celery.task
def slack_notify_new_registration(
    *, webhook: str, company_name: str, domain: str, duration: str, plan: str
) -> None:
    req.post(
        url=webhook,
        json={
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f":tada:  someone from {company_name} registered  :tada:",
                        "emoji": True,
                    },
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Domain:*\n{domain}"},
                        {"type": "mrkdwn", "text": f"*Plan:*\n{plan}"},
                        {"type": "mrkdwn", "text": f"*Duration:*\n{duration}"},
                    ],
                },
            ]
        },
        timeout=app.config["REQUEST_TIMEOUT_SEC"],
    )


@celery.task
def slack_notify_self_serve_exceeded(
    *,
    webhook_url: str,
    account_id: int,
) -> None:
    account = Account.get_by_id(account_id)
    if not account:
        logger.error("Account %d not found but self serve users exceeded", account_id)
        return
    company_name = account.company_name if account.company_name else "<unknown company>"
    req.post(
        url=webhook_url,
        json={
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f":dollar:  Account exceeded 50 users: {company_name}  :dollar:",
                        "emoji": True,
                    },
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Account:*\n{account.id}"},
                        {"type": "mrkdwn", "text": f"*Company Name:*\n{company_name}"},
                        {
                            "type": "mrkdwn",
                            "text": f"*Email Contact:*\n{account.email}",
                        },
                    ],
                },
            ]
        },
        timeout=app.config["REQUEST_TIMEOUT_SEC"],
    )


@celery.task
def send_slack_notification(
    webhook: str,
    text: str,
    blocks: list[dict[str, Any]] | None = None,
) -> None:
    req.post(
        webhook,
        json={"text": text, "blocks": blocks},
        timeout=app.config["REQUEST_TIMEOUT_SEC"],
    )


def slack_notify_stuck_top_pr(
    *,
    repo: GithubRepo,
    pr: PullRequest,
    bot_pr: BotPr,
    pr_time: int,
    timeout: int,
) -> None:
    send_slack_notification.delay(
        app.config["SLACK_NOTIFY_PROD_ALERTS_WEBHOOK"],
        f":rotating_light:  Repo {repo.name} PR {pr.number} stuck at top of queue  :rotating_light:",
        [
            {
                "type": "section",
                "text": {
                    "type": "plain_text",
                    "text": f":rotating_light:  Repo {repo.name} PR {pr.number} stuck at top of queue  :rotating_light:",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Repo:*\n{repo.name} ({repo.id})"},
                    {"type": "mrkdwn", "text": f"*Bot PR:*\n{bot_pr.number}"},
                    {
                        "type": "mrkdwn",
                        "text": f"*Time:*\n{pr_time} minutes (exceeds timeout of {timeout} minutes)",
                    },
                ],
            },
        ],
    )
