"""End-to-end orchestration: intake -> Claude -> parse -> GitHub -> email."""
from __future__ import annotations

import logging
import traceback
from typing import Any, Mapping

from .claude_client import ClaudeGenerationError, generate_plan
from .coach_store import upsert_coach_profile
from .email_send import (
    EmailSendError,
    send_coach_email,
    send_ops_failure_email,
)
from .github_deploy import GitHubDeployError, deploy_plans, discover_next_week_number
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

    # Mutable working copy so we can stamp the resolved week number onto
    # the intake before Claude sees it.
    intake_working: dict[str, Any] = dict(intake)

    try:
        # 0. Pre-flight: discover which week this is for this coach so Claude
        #    can title the documents correctly (Part 10.1 of the master prompt).
        #    Also upsert the coach profile into the CoachPrep returning-coach
        #    store if the intake included a code.
        stage = "preflight"
        week_number = discover_next_week_number(slug)
        intake_working["week"] = week_number
        log.info(
            "preflight intake_id=%s slug=%s week=%d sport=%s",
            intake_id, slug, week_number, sport or "(none)",
        )

        coach_code = str(intake_working.get("coach_code") or "").strip()
        if coach_code:
            try:
                upsert_coach_profile(
                    code=coach_code,
                    name=coach_name,
                    email=coach_email,
                    program=str(intake_working.get("team_name") or ""),
                    sport=sport or "waterpolo",
                )
                log.info(
                    "coach profile upserted code=%s slug=%s", coach_code, slug,
                )
            except Exception:
                # Never block the pipeline on the profile store — log and
                # continue. The worst case is a returning coach has to retype
                # their info next time.
                log.exception(
                    "coach profile upsert failed code=%s slug=%s (continuing)",
                    coach_code, slug,
                )

        # 1. Generate
        stage = "claude"
        response_text = generate_plan(intake_working)

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
            week_number=week_number,
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
            coach_code=coach_code or None,
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
