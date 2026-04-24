"""Game-prep intake path — single-document pipeline (multi-sport).

Introduced Session 7 for water polo; generalized Session 12 to cover
lacrosse as a second sport. Where the weekly pipelines (`run_pipeline`,
`run_lacrosse_pipeline`) produce two documents per intake — a full plan
and a one-page deck/field sheet — the game-prep pipeline produces ONE
self-contained HTML document per opponent, deployed to:

    coaches/<slug>/gameprep-<opponent-slug>.html            # water polo
    coaches/<slug>/lacrosse-gameprep-<opponent-slug>.html   # lacrosse

No deck/field sheet, no week number. The pipeline is a thin orchestration
layer around: `generate_gameprep` → `parse_gameprep` → `deploy_gameprep`
→ `send_gameprep_email`. Stage-level failures are caught and reported to
ops via the shared `send_ops_failure_email` path, matching the weekly
pipeline.

The coach-code returning-coach upsert is performed on every intake that
carries a code — identical to the weekly pipeline's pre-flight behavior —
so returning coaches get pre-fills on their next visit regardless of
which form they use.

Public entry points:
  * `run_gameprep_pipeline(intake)` — water-polo game prep.
    Kept as a single-argument function for backward compatibility with the
    Session 7 wiring; it delegates to the shared core with
    ``sport="waterpolo"``.
  * `run_lacrosse_gameprep_pipeline(intake)` — lacrosse game prep.
    Stamps ``sport="lacrosse"`` before delegating to the shared core.
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


# Per-sport file-prefix for the deployed HTML. Drives the GitHub path:
#   water polo  → coaches/<slug>/gameprep-<opp-slug>.html
#   lacrosse    → coaches/<slug>/lacrosse-gameprep-<opp-slug>.html
# Anything not listed here falls back to the water-polo prefix so an
# unexpected sport string can't lose the file on a typo.
_FILE_PREFIX_BY_SPORT: dict[str, str] = {
    "waterpolo": "gameprep",
    "water_polo": "gameprep",
    "lacrosse": "lacrosse-gameprep",
}


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


def _run_gameprep_core(intake: Mapping[str, Any], sport: str) -> dict:
    """Shared game-prep orchestration used by every sport's wrapper.

    Stamps ``sport`` + ``form_type="gameprep"`` on a defensive copy of the
    intake, runs the four pipeline stages (Claude → parse → deploy →
    email), and wraps stage-level failures in an ops-notify path.

    ``sport`` determines:
      * the Claude user message's pointer to the right master-prompt
        section (WP-G vs LAX-G),
      * the GitHub file-prefix (`gameprep` vs `lacrosse-gameprep`),
      * the per-sport wording of the coach email body,
      * the ops-failure stage label (so log greps can distinguish
        water-polo from lacrosse game-prep failures).
    """
    # Defensive copy so we can stamp fields without mutating the caller's
    # dict.
    intake_working: dict[str, Any] = dict(intake)
    intake_working["sport"] = sport
    intake_working["form_type"] = "gameprep"

    intake_id = str(intake_working.get("intake_id", "?"))
    slug = str(intake_working.get("slug", "?"))
    coach_name = str(intake_working.get("coach_name", ""))
    coach_email = str(intake_working.get("coach_email", ""))
    opponent = _extract_opponent(intake_working) or "Opponent"

    file_prefix = _FILE_PREFIX_BY_SPORT.get(
        (sport or "").strip().lower(), "gameprep"
    )
    stage_prefix = "lax-gameprep" if sport == "lacrosse" else "gameprep"

    stage = "start"

    log.info(
        "gameprep pipeline starting sport=%s intake_id=%s slug=%s coach=%s "
        "opponent=%s",
        sport, intake_id, slug, coach_name, opponent,
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
                    sport=sport,
                )
                log.info(
                    "gameprep coach profile upserted sport=%s code=%s slug=%s",
                    sport, coach_code, slug,
                )
            except Exception:
                log.exception(
                    "gameprep coach profile upsert failed sport=%s code=%s "
                    "slug=%s (continuing)",
                    sport, coach_code, slug,
                )

        # 1. Generate — single HTML document per the sport-specific game-prep
        #    section of the master prompt. `generate_gameprep` reads the
        #    sport off the intake dict (which we just stamped above) and
        #    picks the right section of the master prompt accordingly.
        stage = "claude"
        response_text = generate_gameprep(intake_working)

        # 2. Parse — single marker-wrapped HTML block. The marker pair is
        #    shared across sports; `parse_gameprep` keys on GAME PREP
        #    START/END regardless of whether the response came from WP-G
        #    or LAX-G.
        stage = "parse"
        gameprep_html = parse_gameprep(response_text)

        # 3. Deploy — one file, no week number. The file_prefix namespaces
        #    the filename so water-polo and lacrosse game-prep files never
        #    collide for the same coach.
        stage = "github"
        deploy = deploy_gameprep(
            slug=slug,
            opponent=opponent,
            gameprep_html=gameprep_html,
            coach_name=coach_name,
            intake_id=intake_id,
            file_prefix=file_prefix,
        )

        # 4. Email — single-link coach email, sport-aware copy.
        stage = "email"
        email = send_gameprep_email(
            coach_name=coach_name,
            coach_email=coach_email,
            opponent=opponent,
            gameprep_url=deploy.url,
            sport=sport,
        )

        log.info(
            "gameprep pipeline ok sport=%s intake_id=%s slug=%s opponent=%s "
            "url=%s email_id=%s",
            sport, intake_id, slug, deploy.opponent_slug, deploy.url,
            email.message_id,
        )

        return {
            "ok": True,
            "intake_id": intake_id,
            "slug": slug,
            "sport": sport,
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
            "gameprep pipeline failed sport=%s intake_id=%s stage=%s error=%s",
            sport, intake_id, stage, exc,
        )
        try:
            send_ops_failure_email(
                intake_id=intake_id,
                coach_name=coach_name,
                coach_email=coach_email,
                stage=f"{stage_prefix}:{stage}",
                error=str(exc),
                details=tb,
            )
        except Exception:
            log.exception("ops failure email also failed (gameprep sport=%s)", sport)

        return {
            "ok": False,
            "intake_id": intake_id,
            "slug": slug,
            "sport": sport,
            "opponent": opponent,
            "stage_failed": stage,
            "error": str(exc),
        }


def run_gameprep_pipeline(intake: Mapping[str, Any]) -> dict:
    """Background task: run the water-polo game-prep pipeline for one intake.

    Preserved as a single-argument function for backward compatibility with
    the Session 7 wiring. Stamps ``sport="waterpolo"`` (defensively, even
    if the caller forgot) and delegates to the shared core.
    """
    return _run_gameprep_core(intake, sport="waterpolo")


def run_lacrosse_gameprep_pipeline(intake: Mapping[str, Any]) -> dict:
    """Background task: run the lacrosse game-prep pipeline for one intake.

    Mirrors `run_gameprep_pipeline` but stamps ``sport="lacrosse"`` so:
      * Claude is pointed at SECTION LAX-G (Parts LG1–LG10 + LG-Field).
      * The deployed file lives at
        ``coaches/<slug>/lacrosse-gameprep-<opponent-slug>.html``.
      * The coach email uses lacrosse terminology (EMO/EMD, goalie, the
        field) rather than water-polo terminology.
    """
    return _run_gameprep_core(intake, sport="lacrosse")
