"""CoachPrep returning-coach profile storage.

When a coach submits their first intake they can pick a short code (4–12
chars, letters/digits/._-). The webhook stores their profile under that code
so that subsequent forms (game prep, post-game, week 2, …) can pre-fill the
coach's name, email, program, and sport — the coach just types their code
and hits "Apply."

Storage is intentionally tiny: a single SQLite file in the Railway volume.
No external DB, no ORM, no migrations framework. One table, three columns
of interest + timestamps. Concurrent access is fine because SQLite's own
locking handles the low-throughput traffic this endpoint sees (one write per
intake, a handful of reads per day).

DB location:

    $COACH_STORE_PATH                  explicit override
    $RAILWAY_VOLUME_MOUNT_PATH/coach_store.sqlite3  if Railway volume mounted
    ./coach_store.sqlite3               local fallback

The path is resolved once per process; `reset_coach_store_for_tests()` lets
tests swap to a temp file without reimporting the module.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger("firstwhistle.coach_store")

# Same shape contract as the intake parser (kept here so this module has no
# dependency on intake.py — it can be imported by main.py without any
# pipeline imports).
_CODE_RE = re.compile(r"^[A-Za-z0-9._-]{4,12}$")

# Approved sport values. Others are rejected at write time so a typo can't
# pollute the store.
_VALID_SPORTS = frozenset({"waterpolo", "lacrosse", "basketball"})

_lock = threading.Lock()
_db_path: Optional[Path] = None


class CoachStoreError(RuntimeError):
    """Raised for client-visible validation failures (bad code, bad sport, …)."""


@dataclass(frozen=True)
class CoachProfile:
    code: str
    name: str
    email: str
    program: str
    sport: str
    created_at: str
    updated_at: str


# ---------------------------------------------------------------------------
# Path resolution + schema
# ---------------------------------------------------------------------------

def _default_db_path() -> Path:
    override = os.environ.get("COACH_STORE_PATH")
    if override:
        return Path(override)
    railway_volume = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    if railway_volume:
        return Path(railway_volume) / "coach_store.sqlite3"
    # Local / CI fallback: next to the app.
    return Path.cwd() / "coach_store.sqlite3"


def _resolve_db_path() -> Path:
    global _db_path
    if _db_path is None:
        _db_path = _default_db_path()
        _db_path.parent.mkdir(parents=True, exist_ok=True)
    return _db_path


def reset_coach_store_for_tests(path: Optional[Path]) -> None:
    """Point the store at a different file (or None to re-resolve). Tests only."""
    global _db_path
    _db_path = Path(path) if path is not None else None


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_resolve_db_path()))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS coach_profile (
            code        TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            email       TEXT NOT NULL,
            program     TEXT NOT NULL DEFAULT '',
            sport       TEXT NOT NULL,
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_code(code: str) -> str:
    """Strip + validate a coach code. Returns the normalized code or raises."""
    stripped = (code or "").strip()
    if not stripped:
        raise CoachStoreError("code is required")
    if not _CODE_RE.match(stripped):
        raise CoachStoreError(
            "code must be 4–12 characters, letters/digits/._- only (no spaces)"
        )
    return stripped


def _validate_sport(sport: str) -> str:
    normalized = (sport or "").strip().lower()
    if not normalized:
        normalized = "waterpolo"  # pipeline default
    if normalized not in _VALID_SPORTS:
        raise CoachStoreError(
            f"sport must be one of {sorted(_VALID_SPORTS)}, got {sport!r}"
        )
    return normalized


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def upsert_coach_profile(
    code: str,
    name: str,
    email: str,
    program: str,
    sport: str,
) -> CoachProfile:
    """Create a new coach row or update an existing one.

    On upsert the `updated_at` timestamp is refreshed; `created_at` stays
    pinned to the first-submission time. This lets us see how long coaches
    stay on the platform (cheap analytics, no tracking cookies).
    """
    norm_code = validate_code(code)
    norm_sport = _validate_sport(sport)
    name = (name or "").strip()
    email = (email or "").strip()
    program = (program or "").strip()
    if not name:
        raise CoachStoreError("name is required")
    if not email:
        raise CoachStoreError("email is required")

    with _lock, _connect() as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO coach_profile (code, name, email, program, sport)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                name       = excluded.name,
                email      = excluded.email,
                program    = excluded.program,
                sport      = excluded.sport,
                updated_at = datetime('now');
            """,
            (norm_code, name, email, program, norm_sport),
        )
        conn.commit()
        row = conn.execute(
            "SELECT code, name, email, program, sport, created_at, updated_at "
            "FROM coach_profile WHERE code = ?",
            (norm_code,),
        ).fetchone()

    if row is None:  # pragma: no cover — INSERT+SELECT should always round-trip
        raise CoachStoreError("upsert failed: profile not found after write")

    return CoachProfile(
        code=row["code"],
        name=row["name"],
        email=row["email"],
        program=row["program"],
        sport=row["sport"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def get_coach_profile(code: str) -> Optional[CoachProfile]:
    """Look up a coach by code. Returns None if the code doesn't exist.

    Raises CoachStoreError if `code` is malformed — we treat a malformed
    lookup as a client error so the frontend knows to surface a validation
    message instead of 404.
    """
    norm_code = validate_code(code)
    with _lock, _connect() as conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT code, name, email, program, sport, created_at, updated_at "
            "FROM coach_profile WHERE code = ?",
            (norm_code,),
        ).fetchone()

    if row is None:
        return None
    return CoachProfile(
        code=row["code"],
        name=row["name"],
        email=row["email"],
        program=row["program"],
        sport=row["sport"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
