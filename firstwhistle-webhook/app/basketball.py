"""Basketball intake path — full Claude + GitHub + email pipeline.

Mirrors the lacrosse module exactly: the intake is stamped with
`sport=basketball` (so the master system prompt's Part 0 routes to
SECTION C — BASKETBALL), then handed off to the shared `run_pipeline`.
The same end-to-end stages run for every sport (preflight → Claude →
parse → GitHub → email); only the system-prompt section, the visible
sheet name (`Court Sheet` per Part 10.2), and the surface phrases on the
coach email change.

Game prep is **not** wired for basketball yet — the master prompt's
Part 0 says basketball game-prep submissions fall back to the weekly
flow with a coaching-notes mismatch flag. The stub
``run_basketball_gameprep_pipeline`` below makes that fallback explicit
so a future game-prep route can swap it out without touching the
webhook dispatcher.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from .pipeline import run_pipeline

log = logging.getLogger("firstwhistle.basketball")


def run_basketball_pipeline(intake: Dict[str, Any]) -> dict:
    """Background task: run the full Claude pipeline for a basketball intake.

    Mirrors water polo + lacrosse exactly — the only difference is the
    `sport` field on the intake, which the master system prompt uses to
    route to SECTION C.
    """
    # Defensive: stamp sport even if the caller forgot. `main.py` sets
    # this already, but a replay script invoking this module directly
    # must not silently produce a water-polo plan.
    intake = dict(intake)  # don't mutate caller's dict
    intake["sport"] = "basketball"

    log.info(
        "basketball pipeline starting intake_id=%s slug=%s coach=%s",
        intake.get("intake_id"), intake.get("slug"), intake.get("coach_name"),
    )
    result = run_pipeline(intake)
    if result.get("ok"):
        log.info(
            "basketball pipeline ok intake_id=%s week=%s plan=%s",
            result.get("intake_id"),
            result.get("week_number"),
            result.get("plan_url"),
        )
    else:
        log.error(
            "basketball pipeline failed intake_id=%s stage=%s error=%s",
            result.get("intake_id"),
            result.get("stage_failed"),
            result.get("error"),
        )
    return result


def run_basketball_gameprep_pipeline(intake: Dict[str, Any]) -> dict:
    """Stub: basketball game prep falls back to the weekly basketball
    pipeline.

    Game prep for basketball is not yet built — the master system prompt
    (Part 0) explicitly tells the model to fall back to the weekly flow
    and note the mismatch in the coaching-notes section. This wrapper
    keeps the dispatcher symmetrical with water polo / lacrosse so a
    future "real" basketball game-prep pipeline can be swapped in
    without touching `main.py`.
    """
    log.info(
        "basketball gameprep stub — falling back to weekly pipeline "
        "intake_id=%s slug=%s",
        intake.get("intake_id"), intake.get("slug"),
    )
    return run_basketball_pipeline(intake)
