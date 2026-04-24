"""Lacrosse intake path — full Claude + GitHub + email pipeline.

As of Session 5 the lacrosse master prompt section (Section B) is live and
publishable-quality, so lacrosse intakes run the SAME end-to-end pipeline as
water polo: Claude generates → parser extracts both HTML docs → GitHub commits
them under `coaches/<slug>/week<n>-{plan,deck}.html` → coach receives the
branded email with both links.

This module is intentionally thin: it ensures the intake is flagged with
`sport=lacrosse` (so the master system prompt routes to Section B — see
Part 0 of `firstwhistle_master_system_prompt.md`), logs under the
`firstwhistle.lacrosse` logger so lacrosse traffic is filterable in Railway
logs, then delegates to the shared `run_pipeline`.

The legacy holding-email path is preserved as `run_lacrosse_holding()` in case
Max ever needs to quickly disable auto-generation (e.g., a model regression)
without rolling back code — flip the dispatch in `app.main` to call it
instead. It is no longer wired to the webhook by default.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Mapping

from .config import get_settings
from .email_send import (
    EmailSendError,
    send_lacrosse_holding_email,
    send_ops_failure_email,
    send_ops_lacrosse_manual_email,
)
from .pipeline import run_pipeline

log = logging.getLogger("firstwhistle.lacrosse")


def _intake_summary(intake: Mapping[str, Any]) -> str:
    """Human-readable summary of the intake (used by ops emails on the
    holding-email fallback path)."""
    picks = (
        "coach_name", "coach_email", "team_name", "level", "athlete_count",
        "session_count", "session_length", "pool_setup", "focus_areas",
        "constraints", "week_of", "extra",
    )
    lines = []
    for k in picks:
        v = intake.get(k)
        if v in (None, "", [], {}):
            continue
        lines.append(f"  {k}: {v}")
    extras = intake.get("extras") or {}
    if isinstance(extras, dict) and extras:
        lines.append("  extras:")
        for k, v in extras.items():
            lines.append(f"    {k}: {v}")
    return "\n".join(lines) if lines else json.dumps(dict(intake), default=str, indent=2)[:2000]


def run_lacrosse_pipeline(intake: Dict[str, Any]) -> dict:
    """Background task: run the full Claude pipeline for a lacrosse intake.

    Mirrors water polo exactly — the only difference is the `sport` field on
    the intake, which the master system prompt uses to route to Section B.
    Returns the same result summary shape `run_pipeline` returns.
    """
    # Defensive: make absolutely sure the model sees sport=lacrosse. `main.py`
    # sets this already, but if anyone invokes this module directly (e.g.
    # from a replay script), we don't want silent water-polo drift.
    intake = dict(intake)  # don't mutate caller's dict
    intake["sport"] = "lacrosse"

    log.info(
        "lacrosse pipeline starting intake_id=%s slug=%s coach=%s",
        intake.get("intake_id"), intake.get("slug"), intake.get("coach_name"),
    )
    result = run_pipeline(intake)
    if result.get("ok"):
        log.info(
            "lacrosse pipeline ok intake_id=%s week=%s plan=%s",
            result.get("intake_id"),
            result.get("week_number"),
            result.get("plan_url"),
        )
    else:
        log.error(
            "lacrosse pipeline failed intake_id=%s stage=%s error=%s",
            result.get("intake_id"),
            result.get("stage_failed"),
            result.get("error"),
        )
    return result


def run_lacrosse_holding(intake: Dict[str, Any]) -> None:
    """LEGACY: holding-email-only path.

    Kept for emergency rollback. No longer wired by default — `main.py` now
    dispatches lacrosse intakes to `run_lacrosse_pipeline`. Send a holding
    email to the coach + manual-fulfillment email to ops.
    """
    s = get_settings()
    coach_name = intake.get("coach_name") or "Coach"
    coach_email = intake["coach_email"]
    intake_id = intake.get("intake_id", "?")
    summary = _intake_summary(intake)

    try:
        send_ops_lacrosse_manual_email(intake_id, coach_name, coach_email, summary)
    except Exception:
        log.exception("ops lacrosse notify failed (continuing)")

    try:
        result = send_lacrosse_holding_email(
            coach_name=coach_name,
            coach_email=coach_email,
            holding_hours=s.lacrosse_holding_hours,
        )
        log.info(
            "lacrosse holding pipeline ok intake_id=%s coach_email=%s email_id=%s",
            intake_id, coach_email, result.message_id,
        )
    except EmailSendError as exc:
        log.error("lacrosse holding email failed intake_id=%s: %s", intake_id, exc)
        try:
            send_ops_failure_email(
                intake_id=intake_id,
                coach_name=coach_name,
                coach_email=coach_email,
                stage="lacrosse_holding_email",
                error=str(exc),
                details=summary,
            )
        except Exception:
            log.exception("ops failure email also failed (pipeline fully dropped)")
