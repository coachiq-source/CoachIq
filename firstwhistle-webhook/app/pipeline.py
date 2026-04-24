"""End-to-end orchestration: intake -> Claude -> parse -> GitHub -> email."""
from __future__ import annotations

import logging
import traceback
from typing import Any, Mapping

from .claude_client import ClaudeGenerationError, generate_plan
from .email_send import (
    EmailSendError,
    send_coach_email,
    send_ops_failure_email,
)
from .github_deploy import GitHubDeployError, deploy_plans
from .parser import PlanParseError, parse_plans

log = logging.getLogger("firstwhistle.pipeline")


def run_pipeline(intake: Mapping[str, Any]) -> dict:
    """Run the full pipeline. Designed to be invoked from a FastAPI BackgroundTask.

    Returns a result summary. Raises only if even the ops-notification path
    fails — individual stage errors are caught, logged, and reported to ops.
    """
    intake_id = str(intake.get("intake_id", "?"))
    slug = str(intake.get("slug", "?"))
    coach_name = str(intake.get("coach_name", ""))
    coach_email = str(intake.get("coach_email", ""))
    sport = str(intake.get("sport", "") or "")

    stage = "start"

    try:
        # 1. Generate
        stage = "claude"
        response_text = generate_plan(intake)

        # 2. Parse
        stage = "parse"
        parsed = parse_plans(response_text)

        # 3. Deploy
        stage = "github"
        deploy = deploy_plans(
            slug=slug,
            full_plan_html=parsed.full_plan_html,
            deck_sheet_html=parsed.deck_sheet_html,
            coach_name=coach_name,
            intake_id=intake_id,
        )

        # 4. Email
        stage = "email"
        email = send_coach_email(
            coach_name=coach_name,
            coach_email=coach_email,
            week_number=deploy.week_number,
            plan_url=deploy.plan_url,
            deck_url=deploy.deck_url,
            sport=sport,
        )

        log.info(
            "pipeline ok intake_id=%s slug=%s week=%d plan=%s email_id=%s",
            intake_id, slug, deploy.week_number, deploy.plan_url, email.message_id,
        )

        return {
            "ok": True,
            "intake_id": intake_id,
            "slug": slug,
            "week_number": deploy.week_number,
            "plan_url": deploy.plan_url,
            "deck_url": deploy.deck_url,
            "commit_sha": deploy.commit_sha,
            "email_message_id": email.message_id,
        }

    except (
        ClaudeGenerationError,
        PlanParseError,
        GitHubDeployError,
        EmailSendError,
        Exception,  # last-resort catch so the background task never raises
    ) as exc:
        tb = traceback.format_exc()
        log.exception(
            "pipeline failed intake_id=%s stage=%s error=%s",
            intake_id, stage, exc,
        )
        try:
            send_ops_failure_email(
                intake_id=intake_id,
                coach_name=coach_name,
                coach_email=coach_email,
                stage=stage,
                error=str(exc),
                details=tb,
            )
        except Exception:
            log.exception("ops failure email also failed")

        return {
            "ok": False,
            "intake_id": intake_id,
            "slug": slug,
            "stage_failed": stage,
            "error": str(exc),
        }
