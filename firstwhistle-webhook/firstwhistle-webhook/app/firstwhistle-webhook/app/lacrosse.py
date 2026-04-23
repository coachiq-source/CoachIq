"""Lacrosse intake path — holding email only.

Auto-generation for lacrosse is not yet live. On a lacrosse webhook we:

1. Send the coach a "we've got it, plan within {N} hours" email.
2. Notify ops that a manual plan needs to be prepared.

We do NOT call Claude, and we do NOT touch GitHub. This keeps the lacrosse
endpoint cheap and deterministic until the lacrosse master prompt lands
in a later session.
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

log = logging.getLogger("firstwhistle.lacrosse")


def _intake_summary(intake: Mapping[str, Any]) -> str:
    """Human-readable summary of the intake for the ops notification."""
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
    # Include extras (raw unknown fields) so ops sees lacrosse-specific
    # answers like fogo/goalie/circuit that aren't in the canonical schema.
    extras = intake.get("extras") or {}
    if isinstance(extras, dict) and extras:
        lines.append("  extras:")
        for k, v in extras.items():
            lines.append(f"    {k}: {v}")
    return "\n".join(lines) if lines else json.dumps(dict(intake), default=str, indent=2)[:2000]


def run_lacrosse_holding(intake: Dict[str, Any]) -> None:
    """Background task: send holding email to coach + manual-fulfillment email to ops.

    Swallows errors — on failure, best-effort email to ops with the stack.
    """
    s = get_settings()
    coach_name = intake.get("coach_name") or "Coach"
    coach_email = intake["coach_email"]  # validated upstream
    intake_id = intake.get("intake_id", "?")
    summary = _intake_summary(intake)

    # 1. Ops notification FIRST — we want to know even if the coach email fails.
    try:
        send_ops_lacrosse_manual_email(intake_id, coach_name, coach_email, summary)
    except Exception:
        log.exception("ops lacrosse notify failed (continuing)")

    # 2. Coach holding email.
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
        # Best-effort ops alert so the failure doesn't drop on the floor.
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
