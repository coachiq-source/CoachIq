"""Reader for the post-game (Week-in-Review) JSONL archive.

Companion to `app.postgame`, which *writes* the store on every postgame
intake. This module *reads* it: given a coach slug, return the raw form
payload from the most recent post-game retrospective so the weekly
pipeline can inject last week's game context (result, stats, what worked,
one thing to fix, one thing to protect, …) into the Claude prompt.

Storage-path resolution mirrors `app.postgame` so the same test override
used by the writer (`reset_postgame_store_for_tests`) is honoured here —
tests that write and read through the same override see the same file.

Precedence:

    1. `app.postgame._store_path`       (in-process test override)
    2. `$POSTGAME_STORE_PATH`           (explicit production override)
    3. `$RAILWAY_VOLUME_MOUNT_PATH/postgame_intakes.jsonl`
    4. `./postgame_intakes.jsonl`       (local / CI fallback)

The store is append-only, so "most recent" == last matching line in file
order. Malformed JSON lines are skipped silently — one bad line must not
block a coach from getting their plan.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger("firstwhistle.postgame_store")


def _default_store_path() -> Path:
    override = os.environ.get("POSTGAME_STORE_PATH")
    if override:
        return Path(override)
    railway_volume = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    if railway_volume:
        return Path(railway_volume) / "postgame_intakes.jsonl"
    return Path.cwd() / "postgame_intakes.jsonl"


def _resolve_store_path() -> Path:
    """Resolve the JSONL path, honouring the writer's test override.

    `app.postgame.reset_postgame_store_for_tests(path)` sets a module-level
    path used by the writer. To keep reader + writer coherent in tests, we
    read that same variable before falling back to the env-based default.
    """
    try:
        # Local import so this module stays usable even if `postgame` hasn't
        # been imported yet (circular-import defence).
        from app import postgame as _postgame_writer  # type: ignore
        override = getattr(_postgame_writer, "_store_path", None)
        if override is not None:
            return Path(override)
    except Exception:
        # If the writer module can't be loaded for any reason, fall through
        # to the env-based default — we're a reader, we don't want to take
        # the pipeline down over an import glitch.
        pass
    return _default_store_path()


def get_latest_postgame(
    slug: str,
    sport: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Return the most recent post-game raw payload for `slug`, or None.

    The JSONL store contains one record per post-game submission. Each
    record has at least `slug`, `coach_name`, `sport`, `form_type`, and
    `raw` (the full Formspree body). This function:

      * scans the store once;
      * keeps the last matching `slug` line (append-only store, so
        last-in-file == most recent);
      * if `sport` is provided, only considers records whose stored
        `sport` matches (case-insensitive) — this keeps a lacrosse
        coach from getting injected with a stale water-polo retro
        (and vice versa) if the same slug ever spans sports;
      * returns that row's `raw` dict so callers can read the game stats
        and free-text fields (result, goalsFor, goalsAgainst,
        bestMoment, didntLand, oneThingToFix, oneThingToProtect, …)
        exactly as the coach submitted them;
      * returns None if the store is missing, empty, or has no line
        matching `slug` (and, if supplied, `sport`).

    `sport` is optional so existing callers and fixtures continue to
    work unchanged; production pipeline code should always pass the
    sport from the intake so retrospectives are never mis-wired across
    sports.

    Malformed lines and non-dict records are skipped silently.
    """
    if not slug:
        return None

    # Normalise the requested sport once so we can do case-insensitive
    # comparison in the hot loop. Empty / None means "no filter".
    want_sport = (sport or "").strip().lower() or None

    path = _resolve_store_path()
    if not path.exists():
        return None

    latest: Optional[Dict[str, Any]] = None
    try:
        with path.open("r", encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    # One malformed line must not take the pipeline down.
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("slug") != slug:
                    continue
                if want_sport is not None:
                    rec_sport = str(record.get("sport") or "").strip().lower()
                    # If the record has no sport stamped (very old row), we
                    # skip it rather than guess — safer than cross-wiring a
                    # lacrosse plan to a waterpolo retro.
                    if rec_sport != want_sport:
                        continue
                latest = record
    except OSError as exc:
        log.warning("could not read postgame store %s: %s", path, exc)
        return None

    if latest is None:
        return None

    raw = latest.get("raw")
    if isinstance(raw, dict) and raw:
        return raw
    # Older rows (or test fixtures) that don't carry a `raw` block: fall
    # back to handing the whole record back so callers still get the
    # coach-submitted fields that happen to live at the top level.
    return latest
