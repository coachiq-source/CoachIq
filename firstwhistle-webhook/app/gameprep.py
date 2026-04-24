"""Water-polo game-prep intake path — single-document pipeline.

Introduced Session 7. Where the weekly water-polo pipeline (run_pipeline) and
the lacrosse pipeline (run_lacrosse_pipeline) produce two documents per
intake — a full plan and a one-page deck sheet — the game-prep pipeline
produces ONE self-contained HTML document per opponent, deployed to:

    coaches/<slug>/gameprep-<opponent-slug>.html

No deck sheet, no week number. The pipeline is a thin orchestration layer
around: `generate_gameprep` → `parse_gameprep` → `deploy_gameprep` →
`send_gameprep_email`. Stage-level failures are caught and reported to ops
via the shared `send_ops_failure_email` path, matching the weekly pipeline.

The coach-code returning-coach upsert is performed on every intake that
carries a code — identical to the weekly pipeline's pre-flight behavior —
so returning coaches get pre-fills on their next visit regardless of which
form they use.
"""
from __future__ import annotations

import logging
import traceback
from typing import Any, Mapping

from .claude_client import ClaudeGenerationError, generate_gameprep
from .coach_store import upsert_coach_profile
from .email_send import (
    EmailSendError,
    send_gameprep_email,
    send_ops_failure_email,
)
from .github_deploy import GitHubDeployError, deploy_gameprep
from .parser import PlanParseError, parse_gameprep

log = logging.getLogger("firstwhistle.gameprep")


def _extract_opponent(intake: Mapping[str, Any]) -> str:
    """Pull the opponent name out of wherever the form landed it.

    Game-prep forms always submit `opponent` as a top-level field, which the
    intake parser (app/intake.py) stores in `extras["opponent"]` because it
    isn't one of the weekly-plan canonical aliases. We also check the top
    level defensively so replay scripts or hand-crafted intakes work.
    """
    opp = intake.get("opponent")
    if isinstance(opp, str) and opp.strip():
        return opp.strip()
    extras = intake.get("extras") or {}
    if isinstance(extras, Mapping):
        val = extras.get("opponent")
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def run_gameprep_pipeline(intake: Mapping[str, Any]) -> dict:
    """Background task: run the full game-prep pipeline for one intake.

    Mirrors `run_pipeline` in shape (returns a result dict, never raises
    unless ops notification itself fails) so the webhook `BackgroundTasks`
    wrapper doesn't need to care which pipeline fired.
    """
    # Defensive copy so we can stamp fields without mutating the caller's dict.
    intake_working: dict[str, Any] = dict(intake)
    intake_working["sport"] = "waterpolo"
    intake_working["form_type"] = "gameprep"

    intake_id = str(intake_working.get("intake_id", "?"))
    slug = str(intake_working.get("slug", "?"))
    coach_name = str(intake_working.get("coach_name", ""))
    coach_email = str(intake_working.get("coach_email", ""))
    opponent = _extract_opponent(intake_working) or "Opponent"

    stage = "start"

    log.info(
        "gameprep pipeline starting intake_id=%s slug=%s coach=%s opponent=%s",
        intake_id, slug, coach_name, opponent,
    )

    try:
        # 0. Pre-flight: coach-code upsert (best-effort). No week-number
        #    discovery for game prep — file naming is opponent-based.
        stage = "preflight"
        coach_code = str(intake_working.get("coach_code") or "").strip()
        if coach_code:
            try:
                upsert_coach_profile(
                    code=coach_code,
                    name=coach_name,
                    email=coach_email,
                    program=str(intake_working.get("team_name") or ""),
                    sport="waterpolo",
                )
                log.info(
                    "gameprep coach profile upserted code=%s slug=%s",
                    coach_code, slug,
                )
            except Exception:
                log.exception(
                    "gameprep coach profile upsert failed code=%s slug=%s "
                    "(continuing)",
                    coach_code, slug,
                )

        # 1. Generate — single HTML document per Part G.
        stage = "claude"
        response_text = generate_gameprep(intake_working)

        # 2. Parse — single marker-wrapped HTML block.
        stage = "parse"
        gameprep_html = parse_gameprep(response_text)

        # 3. Deploy — one file, no week number.
        stage = "github"
        deploy = deploy_gameprep(
            slug=slug,
            opponent=opponent,
            gameprep_html=gameprep_html,
            coach_name=coach_name,
            intake_id=intake_id,
        )

        # 4. Email — single-link coach email.
        stage = "email"
        email = send_gameprep_email(
            coach_name=coach_name,
            coach_email=coach_email,
            opponent=opponent,
            gameprep_url=deploy.url,
        )

        log.info(
            "gameprep pipeline ok intake_id=%s slug=%s opponent=%s url=%s "
            "email_id=%s",
            intake_id, slug, deploy.opponent_slug, deploy.url, email.message_id,
        )

        return {
            "ok": True,
            "intake_id": intake_id,
            "slug": slug,
            "opponent": opponent,
            "opponent_slug": deploy.opponent_slug,
            "gameprep_url": deploy.url,
            "commit_sha": deploy.commit_sha,
            "email_message_id": email.message_id,
        }

    except (
        ClaudeGenerationError,
        PlanParseError,
        GitHubDeployError,
        EmailSendError,
        Exception,  # final safety net — background tasks must never raise.
    ) as exc:
        tb = traceback.format_exc()
        log.exception(
            "gameprep pipeline failed intake_id=%s stage=%s error=%s",
            intake_id, stage, exc,
        )
        try:
            send_ops_failure_email(
                intake_id=intake_id,
                coach_name=coach_name,
                coach_email=coach_email,
                stage=f"gameprep:{stage}",
                error=str(exc),
                details=tb,
            )
        except Exception:
            log.exception("ops failure email also failed (gameprep)")

        return {
            "ok": False,
            "intake_id": intake_id,
            "slug": slug,
            "opponent": opponent,
            "stage_failed": stage,
            "error": str(exc),
        }
