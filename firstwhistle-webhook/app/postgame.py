"""Post-game (Week-in-Review) intake handler — capture-only for now.

Introduced Session 9. The post-game form (`waterpolo_postgame.html`,
`lacrosse_postgame.html`) lets coaches file a short retrospective each week:
what happened in the game (if any), what landed, what didn't, confidence
going into next week. The eventual plan is to feed those responses into the
master prompt so the next weekly plan is informed by what actually happened.

For now, though, we just need to:

    1. Accept the submission without rejecting it (the weekly-plan strict
       parser 422s on missing `coach_name`, and the post-game form uses
       `coachName` / `name`, which is a different schema).
    2. Log a single line — `postgame intake accepted coach=<name> sport=<sport>`
       — so we can see the traffic in Railway logs.
    3. Persist the full payload to disk for later triage / replay.
    4. Let the webhook return 202 so Formspree doesn't retry.

Storage is deliberately dumb: one JSON line per intake, appended to a file
in the Railway volume (or local cwd for tests). A JSONL store is more than
enough for the ~one submission per coach per week traffic this endpoint
sees, and it keeps the raw payload around for when we build the real
pipeline — no schema migration to plan for now.

Storage location precedence:

    $POSTGAME_STORE_PATH                           explicit override
    $RAILWAY_VOLUME_MOUNT_PATH/postgame_intakes.jsonl
    ./postgame_intakes.jsonl                       local / CI fallback
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Mapping, Optional

log = logging.getLogger("firstwhistle.postgame")

_lock = threading.Lock()
_store_path: Optional[Path] = None


class PostgameStoreError(RuntimeError):
    """Raised when we physically cannot append to the postgame store."""


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _default_store_path() -> Path:
    override = os.environ.get("POSTGAME_STORE_PATH")
    if override:
        return Path(override)
    railway_volume = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    if railway_volume:
        return Path(railway_volume) / "postgame_intakes.jsonl"
    return Path.cwd() / "postgame_intakes.jsonl"


def _resolve_store_path() -> Path:
    global _store_path
    if _store_path is None:
        _store_path = _default_store_path()
        _store_path.parent.mkdir(parents=True, exist_ok=True)
    return _store_path


def reset_postgame_store_for_tests(path: Optional[Path]) -> None:
    """Point the JSONL store at a different file (or None to re-resolve)."""
    global _store_path
    _store_path = Path(path) if path is not None else None


def store_postgame_intake(intake: Mapping[str, Any]) -> Path:
    """Append a post-game intake as one JSON line. Returns the store path.

    The record contains the full canonical intake (including `raw` — the
    original Formspree body) plus a `stored_at` timestamp so later triage
    can order rows chronologically without re-parsing Formspree dates.
    """
    record = dict(intake)
    record.setdefault(
        "stored_at",
        _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
    )

    path = _resolve_store_path()
    line = json.dumps(record, ensure_ascii=False, default=str)
    with _lock:
        try:
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as exc:
            raise PostgameStoreError(
                f"failed to append postgame intake to {path}: {exc}"
            ) from exc
    return path


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def run_postgame_handler(intake: Mapping[str, Any]) -> dict:
    """Background task: log the postgame intake + persist it for triage.

    Mirrors the shape of `run_pipeline` / `run_gameprep_pipeline` (returns a
    result dict, never raises) so `BackgroundTasks` doesn't have to care
    which handler fired. No Claude call, no GitHub deploy, no coach email —
    those land when we build the real post-game pipeline.
    """
    intake_working: dict[str, Any] = dict(intake)
    intake_working.setdefault("form_type", "postgame")
    sport = str(intake_working.get("sport") or "waterpolo")
    intake_working["sport"] = sport

    intake_id = str(intake_working.get("intake_id", "?"))
    slug = str(intake_working.get("slug", "?"))
    # Prefer the canonical coach_name (the lenient parser aliases `name` and
    # `coachName` into it); fall back to the raw body so a truly unlabeled
    # submission at least logs something the ops inbox can identify.
    coach_name = str(intake_working.get("coach_name") or "").strip()
    if not coach_name:
        raw_body = intake_working.get("raw") or {}
        if isinstance(raw_body, Mapping):
            for key in ("coachName", "name", "full_name", "coach"):
                val = raw_body.get(key)
                if isinstance(val, str) and val.strip():
                    coach_name = val.strip()
                    break
    coach_display = coach_name or "(unknown)"

    # The log line the ops team greps for in Railway. Keep the shape stable.
    log.info(
        "postgame intake accepted coach=%s sport=%s intake_id=%s slug=%s",
        coach_display, sport, intake_id, slug,
    )

    try:
        store_path = store_postgame_intake(intake_working)
    except PostgameStoreError as exc:
        # Don't re-raise from a background task — the webhook already 202'd.
        # Log loudly so Railway alerts fire; the Formspree form was accepted
        # and the coach sees success, so we can triage from the logs.
        log.exception(
            "postgame store failed intake_id=%s coach=%s error=%s",
            intake_id, coach_display, exc,
        )
        return {
            "ok": False,
            "intake_id": intake_id,
            "slug": slug,
            "coach_name": coach_name,
            "sport": sport,
            "stage_failed": "store",
            "error": str(exc),
        }

    return {
        "ok": True,
        "intake_id": intake_id,
        "slug": slug,
        "coach_name": coach_name,
        "sport": sport,
        "stored_at_path": str(store_path),
    }
