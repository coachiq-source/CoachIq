"""Tests for `app.postgame_store` and pipeline post-game context injection.

Covers:
  1. `get_latest_postgame` — basic slug match returns the raw payload.
  2. `get_latest_postgame` — when multiple entries exist for the same slug,
     the most recent (last-in-file) wins.
  3. `get_latest_postgame` — rows for other slugs are ignored.
  4. `get_latest_postgame` — returns None when no match.
  5. `get_latest_postgame` — returns None when the JSONL file doesn't exist.
  6. `get_latest_postgame` — malformed lines are skipped, good ones still win.
  7. `get_latest_postgame` — empty slug guard.
  8. Reader honours the writer's test override so write-then-read works in
     the same test.
  9. `get_latest_postgame` — falls back to the top-level record when `raw`
     is missing/empty (legacy row tolerance).
 10. Pipeline injection: when week > 1 AND a retrospective exists, Claude is
     called with a user message containing the WEEK N-1 POST-GAME REVIEW
     block and all the expected fields.
 11. Pipeline injection: Week 1 never pulls post-game context (even if a
     retrospective happens to exist), and the preflight log says so.
 12. Pipeline injection: Week 2+ with no retrospective on file logs "no
     post-game context for slug=..." and calls Claude with no block.
 13. Pipeline injection: a lookup error degrades gracefully — the pipeline
     still delivers the plan, it just skips the context block.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Stub the five required env vars so `get_settings()` works on import.
os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic")
os.environ.setdefault("GITHUB_TOKEN", "test-token")
os.environ.setdefault("GITHUB_REPO", "coachiq-source/CoachIq")
os.environ.setdefault("RESEND_API_KEY", "test-resend")
os.environ.setdefault("COACH_EMAIL_FROM", "coach@example.com")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_lines(path: Path, records) -> None:
    """Write a list of records as JSONL. Records may be dicts or raw strings
    (strings are written verbatim so we can inject malformed lines)."""
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            if isinstance(r, str):
                fh.write(r + "\n")
            else:
                fh.write(json.dumps(r) + "\n")


def _make_pg_record(slug: str, *, result: str = "W", gf: str = "10",
                    ga: str = "7", fix: str = "hole-set entry",
                    protect: str = "press defence", had_game: str = "Yes",
                    **extra_raw) -> dict:
    """Build a record in the exact shape `store_postgame_intake` writes."""
    raw = {
        "sport": "waterpolo",
        "formType": "postgame",
        "coachName": "Jamie Rivera",
        "coachEmail": "jamie@example.com",
        "hadGame": had_game,
        "opponent": "St. Mary's Prep",
        "result": result,
        "goalsFor": gf,
        "goalsAgainst": ga,
        "shotTotal": "28",
        "ejectionsDrawnFor": "4",
        "ejectionsDrawnAgainst": "2",
        "steals": "6",
        "turnovers": "9",
        "pp6Goals": "2",
        "pp6Attempts": "4",
        "md5Stops": "3",
        "md5Attempts": "4",
        "resultFeel": "earned",
        "bestMoment": "counter-attack speed",
        "didntLand": "hole-set entry",
        "standoutPlayer": "#7 Alex",
        "confidenceNextWeek": "4",
        "oneThingToFix": fix,
        "oneThingToProtect": protect,
        "extraNotes": "two starters back from flu",
    }
    raw.update(extra_raw)
    return {
        "intake_id": "abcdef012345",
        "slug": slug,
        "coach_name": "Jamie Rivera",
        "coach_email": "jamie@example.com",
        "sport": "waterpolo",
        "form_type": "postgame",
        "extras": {"hadGame": "Yes"},
        "raw": raw,
        "stored_at": "2026-04-24T12:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# 1. get_latest_postgame — happy path
# ---------------------------------------------------------------------------

def test_get_latest_postgame_returns_raw_payload():
    from app.postgame import reset_postgame_store_for_tests
    from app.postgame_store import get_latest_postgame

    with tempfile.TemporaryDirectory() as td:
        store_path = Path(td) / "postgame_intakes.jsonl"
        _write_lines(store_path, [_make_pg_record("jamie-rivera")])
        reset_postgame_store_for_tests(store_path)
        try:
            raw = get_latest_postgame("jamie-rivera")
        finally:
            reset_postgame_store_for_tests(None)

    assert raw is not None
    assert raw["result"] == "W"
    assert raw["goalsFor"] == "10"
    assert raw["goalsAgainst"] == "7"
    assert raw["oneThingToFix"] == "hole-set entry"
    assert raw["oneThingToProtect"] == "press defence"


# ---------------------------------------------------------------------------
# 2. Most recent wins
# ---------------------------------------------------------------------------

def test_get_latest_postgame_returns_most_recent_for_slug():
    from app.postgame import reset_postgame_store_for_tests
    from app.postgame_store import get_latest_postgame

    with tempfile.TemporaryDirectory() as td:
        store_path = Path(td) / "postgame_intakes.jsonl"
        _write_lines(store_path, [
            _make_pg_record("jamie-rivera", result="L", gf="6", ga="11"),
            _make_pg_record("jamie-rivera", result="W", gf="12", ga="9"),
            _make_pg_record("jamie-rivera", result="T", gf="8", ga="8"),
        ])
        reset_postgame_store_for_tests(store_path)
        try:
            raw = get_latest_postgame("jamie-rivera")
        finally:
            reset_postgame_store_for_tests(None)

    assert raw is not None
    # Last line wins — append-only store.
    assert raw["result"] == "T"
    assert raw["goalsFor"] == "8"
    assert raw["goalsAgainst"] == "8"


# ---------------------------------------------------------------------------
# 3. Other slugs ignored
# ---------------------------------------------------------------------------

def test_get_latest_postgame_ignores_other_slugs():
    from app.postgame import reset_postgame_store_for_tests
    from app.postgame_store import get_latest_postgame

    with tempfile.TemporaryDirectory() as td:
        store_path = Path(td) / "postgame_intakes.jsonl"
        _write_lines(store_path, [
            _make_pg_record("someone-else", result="L"),
            _make_pg_record("jamie-rivera", result="W"),
            _make_pg_record("third-coach", result="T"),
        ])
        reset_postgame_store_for_tests(store_path)
        try:
            raw = get_latest_postgame("jamie-rivera")
        finally:
            reset_postgame_store_for_tests(None)

    assert raw is not None
    assert raw["result"] == "W"


# ---------------------------------------------------------------------------
# 4. No match → None
# ---------------------------------------------------------------------------

def test_get_latest_postgame_returns_none_when_no_match():
    from app.postgame import reset_postgame_store_for_tests
    from app.postgame_store import get_latest_postgame

    with tempfile.TemporaryDirectory() as td:
        store_path = Path(td) / "postgame_intakes.jsonl"
        _write_lines(store_path, [_make_pg_record("someone-else")])
        reset_postgame_store_for_tests(store_path)
        try:
            assert get_latest_postgame("nobody-home") is None
        finally:
            reset_postgame_store_for_tests(None)


# ---------------------------------------------------------------------------
# 5. Missing file → None
# ---------------------------------------------------------------------------

def test_get_latest_postgame_returns_none_when_store_missing():
    from app.postgame import reset_postgame_store_for_tests
    from app.postgame_store import get_latest_postgame

    with tempfile.TemporaryDirectory() as td:
        store_path = Path(td) / "does-not-exist.jsonl"
        reset_postgame_store_for_tests(store_path)
        try:
            assert get_latest_postgame("anyone") is None
        finally:
            reset_postgame_store_for_tests(None)


# ---------------------------------------------------------------------------
# 6. Malformed lines skipped
# ---------------------------------------------------------------------------

def test_get_latest_postgame_skips_malformed_lines():
    from app.postgame import reset_postgame_store_for_tests
    from app.postgame_store import get_latest_postgame

    with tempfile.TemporaryDirectory() as td:
        store_path = Path(td) / "postgame_intakes.jsonl"
        _write_lines(store_path, [
            _make_pg_record("jamie-rivera", result="W", gf="10", ga="7"),
            "this is not valid json",            # malformed → skipped
            "[1, 2, 3]",                         # valid JSON but not a dict
            _make_pg_record("jamie-rivera", result="L", gf="4", ga="9"),
            "",                                  # blank → skipped
        ])
        reset_postgame_store_for_tests(store_path)
        try:
            raw = get_latest_postgame("jamie-rivera")
        finally:
            reset_postgame_store_for_tests(None)

    assert raw is not None
    # The good row after the bad ones still wins.
    assert raw["result"] == "L"
    assert raw["goalsFor"] == "4"


# ---------------------------------------------------------------------------
# 7. Empty slug guard
# ---------------------------------------------------------------------------

def test_get_latest_postgame_empty_slug_returns_none():
    from app.postgame_store import get_latest_postgame
    assert get_latest_postgame("") is None


# ---------------------------------------------------------------------------
# 8. Write-then-read through the shared override
# ---------------------------------------------------------------------------

def test_get_latest_postgame_reads_what_writer_just_wrote():
    from app.postgame import (
        reset_postgame_store_for_tests,
        run_postgame_handler,
    )
    from app.postgame_store import get_latest_postgame

    with tempfile.TemporaryDirectory() as td:
        store_path = Path(td) / "postgame_intakes.jsonl"
        reset_postgame_store_for_tests(store_path)
        try:
            run_postgame_handler({
                "intake_id": "111122223333",
                "slug": "magnus-sims",
                "coach_name": "Magnus Sims",
                "coach_email": "magnus@example.com",
                "sport": "waterpolo",
                "form_type": "postgame",
                "extras": {},
                "raw": {
                    "result": "W",
                    "goalsFor": "14",
                    "goalsAgainst": "8",
                    "oneThingToFix": "transition D",
                    "oneThingToProtect": "pool spacing",
                },
            })
            raw = get_latest_postgame("magnus-sims")
        finally:
            reset_postgame_store_for_tests(None)

    assert raw is not None
    assert raw["result"] == "W"
    assert raw["goalsFor"] == "14"
    assert raw["oneThingToProtect"] == "pool spacing"


# ---------------------------------------------------------------------------
# 9. Legacy row without `raw` — returns the whole record
# ---------------------------------------------------------------------------

def test_get_latest_postgame_falls_back_when_no_raw_block():
    from app.postgame import reset_postgame_store_for_tests
    from app.postgame_store import get_latest_postgame

    with tempfile.TemporaryDirectory() as td:
        store_path = Path(td) / "postgame_intakes.jsonl"
        legacy_row = {
            "slug": "legacy-coach",
            "coach_name": "Legacy Coach",
            "sport": "waterpolo",
            "form_type": "postgame",
            "result": "W",
            "goalsFor": "10",
            # No `raw` key.
        }
        _write_lines(store_path, [legacy_row])
        reset_postgame_store_for_tests(store_path)
        try:
            out = get_latest_postgame("legacy-coach")
        finally:
            reset_postgame_store_for_tests(None)

    assert out is not None
    assert out["result"] == "W"
    assert out["coach_name"] == "Legacy Coach"


# ---------------------------------------------------------------------------
# 10. Pipeline injection — week 2+ with a retrospective
# ---------------------------------------------------------------------------

def _stub_claude_response(week: int) -> str:
    return (
        "<!-- ===== FULL PLAN START ===== -->\n"
        f"<!DOCTYPE html><html><head><title>CoachPrep — Week {week} Practice Plan</title>"
        f"</head><body><h1>Week {week} Practice Plan</h1></body></html>\n"
        "<!-- ===== FULL PLAN END ===== -->\n\n"
        "<!-- ===== DECK SHEET START ===== -->\n"
        f"<!DOCTYPE html><html><head><title>CoachPrep — Week {week} Deck Sheet</title>"
        f"</head><body><h1>Week {week} Deck Sheet</h1></body></html>\n"
        "<!-- ===== DECK SHEET END ===== -->"
    )


def test_pipeline_injects_postgame_context_for_week_2(caplog):
    """When week > 1 AND a retrospective is on file, the pipeline must hand
    Claude the WEEK N-1 POST-GAME REVIEW block as part of the user message."""
    from app import pipeline
    from app.github_deploy import DeployResult
    from app.postgame import reset_postgame_store_for_tests

    captured = {}

    def fake_generate_plan(intake, postgame_context=None):
        captured["intake_week"] = intake.get("week")
        captured["postgame_context"] = postgame_context
        return _stub_claude_response(int(intake.get("week") or 1))

    with tempfile.TemporaryDirectory() as td:
        store_path = Path(td) / "postgame_intakes.jsonl"
        _write_lines(store_path, [_make_pg_record(
            "jamie-rivera", result="L", gf="6", ga="11",
            fix="counter-attack speed",
            protect="goalkeeper positioning",
        )])
        reset_postgame_store_for_tests(store_path)
        try:
            fake_deploy = DeployResult(
                plan_url="u", deck_url="u", commit_sha="sha",
                plan_path="a", deck_path="b", week_number=2,
            )
            with patch.object(pipeline, "discover_next_week_number", return_value=2), \
                 patch.object(pipeline, "generate_plan",
                              side_effect=fake_generate_plan), \
                 patch.object(pipeline, "deploy_plans", return_value=fake_deploy), \
                 patch.object(pipeline, "send_coach_email") as mock_email:
                mock_email.return_value.message_id = "msg-x"
                with caplog.at_level("INFO", logger="firstwhistle.pipeline"):
                    result = pipeline.run_pipeline({
                        "intake_id": "i1",
                        "slug": "jamie-rivera",
                        "coach_name": "Jamie Rivera",
                        "coach_email": "jamie@example.com",
                        "sport": "waterpolo",
                    })
        finally:
            reset_postgame_store_for_tests(None)

    assert result["ok"] is True
    assert captured["intake_week"] == 2
    pg = captured["postgame_context"]
    assert pg is not None, "generate_plan should receive the postgame context"
    assert pg["result"] == "L"
    assert pg["oneThingToFix"] == "counter-attack speed"
    assert pg["oneThingToProtect"] == "goalkeeper positioning"
    # And the pipeline should have logged the "found" line with slug+week.
    assert any(
        "post-game context found for slug=jamie-rivera" in r.message
        and "week=2" in r.message
        for r in caplog.records
    ), [r.message for r in caplog.records]


# ---------------------------------------------------------------------------
# 10b. User-message contains the exact block the master prompt expects
# ---------------------------------------------------------------------------

def test_generate_plan_user_message_contains_postgame_block():
    """Unit-test the claude_client shaping: when postgame_context is passed
    and intake.week > 1, the user message must contain the canonical header
    and every required field line."""
    from app import claude_client

    intake = {"intake_id": "i1", "slug": "jamie-rivera", "week": 2}
    postgame = {
        "hadGame": "Yes",
        "opponent": "St. Mary's Prep",
        "result": "L",
        "goalsFor": "6",
        "goalsAgainst": "11",
        "shotTotal": "28",
        "ejectionsDrawnFor": "3",
        "ejectionsDrawnAgainst": "2",
        "steals": "5",
        "turnovers": "10",
        "pp6Goals": "1",
        "pp6Attempts": "4",
        "md5Stops": "2",
        "md5Attempts": "5",
        "resultFeel": "frustrating",
        "bestMoment": "transition speed",
        "didntLand": "hole-set entry",
        "standoutPlayer": "#4 Kiran",
        "confidenceNextWeek": "3",
        "oneThingToFix": "counter-attack speed",
        "oneThingToProtect": "goalkeeper positioning",
        "extraNotes": "Two starters out next Tuesday.",
    }
    captured = {}

    class FakeMsg:
        content = [type("B", (), {"type": "text",
                                   "text": _stub_claude_response(2)})()]
        stop_reason = "end_turn"

    class FakeMessages:
        def create(self, **kwargs):
            captured["messages"] = kwargs["messages"]
            return FakeMsg()

    class FakeClient:
        messages = FakeMessages()

    with patch.object(claude_client, "_client", return_value=FakeClient()):
        claude_client.generate_plan(intake, postgame_context=postgame)

    user_msg = captured["messages"][0]["content"]
    # Canonical header with the right week numbers.
    assert "WEEK 1 REVIEW" in user_msg
    assert "Week 2" in user_msg
    # Every required field line is present.
    for needle in [
        "Had a game this week: Yes",
        "Opponent: St. Mary's Prep",
        "Result (W/L/T): L",
        "6\u201311",                         # en-dash score
        "Shots (total): 28",
        "Ejections drawn for: 3",
        "Ejections against (drawn by opponent): 2",
        "Steals: 5",
        "Turnovers: 10",
        "6x5 conversion (goals / attempts): 1 / 4",
        "5x6 stops (stops / attempts): 2 / 5",
        "Result feel: frustrating",
        "Best moment of the week: transition speed",
        "What didn't land: hole-set entry",
        "Player who stood out: #4 Kiran",
        "Confidence going into Week 2 (1\u20135): 3",
        "One thing to fix: counter-attack speed",
        "One thing to protect: goalkeeper positioning",
        "Extra notes: Two starters out next Tuesday.",
    ]:
        assert needle in user_msg, f"missing: {needle!r}"
    # Block precedes the weekly prompt preamble + intake JSON.
    assert user_msg.index("WEEK 1 REVIEW") < user_msg.index(
        "A new coach intake has been submitted"
    )
    assert user_msg.index("A new coach intake has been submitted") < user_msg.index(
        "INTAKE JSON:"
    )


def test_generate_plan_no_game_week_still_injects_practice_fields():
    """If the coach didn't have a game, the game-stats block is omitted but
    the practice-focused fields (best moment, what didn't land, confidence,
    one thing to fix, one thing to protect, extra notes) are still sent."""
    from app import claude_client

    intake = {"intake_id": "i1", "slug": "jamie-rivera", "week": 3}
    postgame = {
        "hadGame": "No",
        # None of the game-stat fields should appear in the block.
        "bestMoment": "two-hour conditioning block on Thursday",
        "didntLand": "press out of 3-3",
        "standoutPlayer": "#9 Sam (first time as set D)",
        "confidenceNextWeek": "4",
        "oneThingToFix": "spacing on 3-3",
        "oneThingToProtect": "Thursday conditioning intensity",
        "extraNotes": "Scrimmage cancelled; shifted to a skills block.",
    }
    captured = {}

    class FakeMsg:
        content = [type("B", (), {"type": "text",
                                   "text": _stub_claude_response(3)})()]
        stop_reason = "end_turn"

    class FakeMessages:
        def create(self, **kwargs):
            captured["messages"] = kwargs["messages"]
            return FakeMsg()

    class FakeClient:
        messages = FakeMessages()

    with patch.object(claude_client, "_client", return_value=FakeClient()):
        claude_client.generate_plan(intake, postgame_context=postgame)

    user_msg = captured["messages"][0]["content"]

    # Header present, no-game explicit.
    assert "WEEK 2 REVIEW" in user_msg
    assert "Had a game this week: No" in user_msg
    # Game-stat lines are OMITTED.
    for needle in [
        "Result (W/L/T)",
        "Score (GF",
        "Shots (total)",
        "Ejections drawn",
        "6x5 conversion",
        "5x6 stops",
        "Result feel",
        "Opponent:",
    ]:
        assert needle not in user_msg, f"unexpected game-stat line: {needle!r}"
    # Practice-focused lines ARE present.
    for needle in [
        "Best moment of the week: two-hour conditioning block on Thursday",
        "What didn't land: press out of 3-3",
        "Player who stood out: #9 Sam (first time as set D)",
        "Confidence going into Week 3 (1\u20135): 4",
        "One thing to fix: spacing on 3-3",
        "One thing to protect: Thursday conditioning intensity",
        "Extra notes: Scrimmage cancelled; shifted to a skills block.",
    ]:
        assert needle in user_msg, f"missing practice-focus line: {needle!r}"


def test_generate_plan_omits_postgame_block_when_no_context():
    """Back-compat: with no postgame_context, the user message must match the
    pre-Session-10 shape (no WEEK N-1 header at all)."""
    from app import claude_client

    captured = {}

    class FakeMsg:
        content = [type("B", (), {"type": "text",
                                   "text": _stub_claude_response(1)})()]
        stop_reason = "end_turn"

    class FakeMessages:
        def create(self, **kwargs):
            captured["messages"] = kwargs["messages"]
            return FakeMsg()

    class FakeClient:
        messages = FakeMessages()

    with patch.object(claude_client, "_client", return_value=FakeClient()):
        claude_client.generate_plan({"intake_id": "i1", "slug": "x", "week": 1})

    user_msg = captured["messages"][0]["content"]
    assert "POST-GAME REVIEW" not in user_msg
    assert user_msg.startswith("A new coach intake has been submitted")


def test_generate_plan_skips_postgame_block_in_week_1_even_if_context_passed():
    """Defence in depth: the pipeline already guards on week>1, but the
    client-side guard should too, so a bad caller can't smuggle a context
    block into a Week 1 prompt."""
    from app import claude_client

    captured = {}

    class FakeMsg:
        content = [type("B", (), {"type": "text",
                                   "text": _stub_claude_response(1)})()]
        stop_reason = "end_turn"

    class FakeMessages:
        def create(self, **kwargs):
            captured["messages"] = kwargs["messages"]
            return FakeMsg()

    class FakeClient:
        messages = FakeMessages()

    with patch.object(claude_client, "_client", return_value=FakeClient()):
        claude_client.generate_plan(
            {"intake_id": "i1", "slug": "x", "week": 1},
            postgame_context={"result": "W", "goalsFor": "12"},
        )

    user_msg = captured["messages"][0]["content"]
    assert "POST-GAME REVIEW" not in user_msg


# ---------------------------------------------------------------------------
# 11. Week 1 — no lookup, no injection
# ---------------------------------------------------------------------------

def test_pipeline_week1_skips_postgame_lookup(caplog):
    """Week 1 has no prior week to reference — the pipeline must log
    `no post-game context for slug=...` and call Claude with no block."""
    from app import pipeline
    from app.github_deploy import DeployResult
    from app.postgame import reset_postgame_store_for_tests

    captured = {}

    def fake_generate_plan(intake, postgame_context=None):
        captured["postgame_context"] = postgame_context
        return _stub_claude_response(1)

    with tempfile.TemporaryDirectory() as td:
        store_path = Path(td) / "postgame_intakes.jsonl"
        # Seed a retrospective — Week 1 should ignore it regardless.
        _write_lines(store_path, [_make_pg_record("jamie-rivera")])
        reset_postgame_store_for_tests(store_path)
        try:
            fake_deploy = DeployResult("u", "u", "sha", "a", "b", 1)
            with patch.object(pipeline, "discover_next_week_number", return_value=1), \
                 patch.object(pipeline, "generate_plan",
                              side_effect=fake_generate_plan), \
                 patch.object(pipeline, "deploy_plans", return_value=fake_deploy), \
                 patch.object(pipeline, "send_coach_email") as mock_email:
                mock_email.return_value.message_id = "msg-x"
                with caplog.at_level("INFO", logger="firstwhistle.pipeline"):
                    result = pipeline.run_pipeline({
                        "intake_id": "i1",
                        "slug": "jamie-rivera",
                        "coach_name": "Jamie Rivera",
                        "coach_email": "jamie@example.com",
                        "sport": "waterpolo",
                    })
        finally:
            reset_postgame_store_for_tests(None)

    assert result["ok"] is True
    assert captured["postgame_context"] is None
    # The "no post-game context" log line fires for week 1.
    assert any(
        "no post-game context for slug=jamie-rivera" in r.message
        for r in caplog.records
    ), [r.message for r in caplog.records]


# ---------------------------------------------------------------------------
# 12. Week 2+ but no retrospective on file
# ---------------------------------------------------------------------------

def test_pipeline_week2_no_retrospective_logs_none_and_skips_injection(caplog):
    from app import pipeline
    from app.github_deploy import DeployResult
    from app.postgame import reset_postgame_store_for_tests

    captured = {}

    def fake_generate_plan(intake, postgame_context=None):
        captured["postgame_context"] = postgame_context
        return _stub_claude_response(2)

    with tempfile.TemporaryDirectory() as td:
        store_path = Path(td) / "postgame_intakes.jsonl"
        # Only *other* coaches have retrospectives — none for jamie-rivera.
        _write_lines(store_path, [_make_pg_record("someone-else")])
        reset_postgame_store_for_tests(store_path)
        try:
            fake_deploy = DeployResult("u", "u", "sha", "a", "b", 2)
            with patch.object(pipeline, "discover_next_week_number", return_value=2), \
                 patch.object(pipeline, "generate_plan",
                              side_effect=fake_generate_plan), \
                 patch.object(pipeline, "deploy_plans", return_value=fake_deploy), \
                 patch.object(pipeline, "send_coach_email") as mock_email:
                mock_email.return_value.message_id = "msg-x"
                with caplog.at_level("INFO", logger="firstwhistle.pipeline"):
                    result = pipeline.run_pipeline({
                        "intake_id": "i1",
                        "slug": "jamie-rivera",
                        "coach_name": "Jamie Rivera",
                        "coach_email": "jamie@example.com",
                        "sport": "waterpolo",
                    })
        finally:
            reset_postgame_store_for_tests(None)

    assert result["ok"] is True
    assert captured["postgame_context"] is None
    assert any(
        "no post-game context for slug=jamie-rivera" in r.message
        for r in caplog.records
    ), [r.message for r in caplog.records]


# ---------------------------------------------------------------------------
# 13. Lookup error → graceful fallback, pipeline still succeeds
# ---------------------------------------------------------------------------

def test_pipeline_degrades_gracefully_when_postgame_lookup_raises(caplog):
    """A bad volume, a corrupted row, whatever — the coach still gets a
    plan. The lookup error must be logged but the pipeline must continue."""
    from app import pipeline
    from app.github_deploy import DeployResult

    captured = {}

    def fake_generate_plan(intake, postgame_context=None):
        captured["postgame_context"] = postgame_context
        return _stub_claude_response(3)

    def explode(_slug, _sport=None):  # pragma: no cover - signature only
        raise RuntimeError("volume not mounted")

    fake_deploy = DeployResult("u", "u", "sha", "a", "b", 3)
    with patch.object(pipeline, "discover_next_week_number", return_value=3), \
         patch.object(pipeline, "get_latest_postgame", side_effect=explode), \
         patch.object(pipeline, "generate_plan", side_effect=fake_generate_plan), \
         patch.object(pipeline, "deploy_plans", return_value=fake_deploy), \
         patch.object(pipeline, "send_coach_email") as mock_email:
        mock_email.return_value.message_id = "msg-x"
        with caplog.at_level("INFO", logger="firstwhistle.pipeline"):
            result = pipeline.run_pipeline({
                "intake_id": "i1",
                "slug": "jamie-rivera",
                "coach_name": "Jamie Rivera",
                "coach_email": "jamie@example.com",
                "sport": "waterpolo",
            })

    assert result["ok"] is True, (
        "pipeline must NOT fail on a post-game lookup error; "
        "context is a nice-to-have, not a blocker"
    )
    assert captured["postgame_context"] is None


# ---------------------------------------------------------------------------
# 14. Sport filtering — lacrosse/waterpolo must not cross-wire
# ---------------------------------------------------------------------------

def _make_lax_pg_record(slug: str, *, result: str = "W", gf: str = "11",
                        ga: str = "7", gender: str = "Boys",
                        fix: str = "Ground balls",
                        protect: str = "Ground ball culture",
                        had_game: str = "Yes", **extra_raw) -> dict:
    """Build a lacrosse post-game record in the shape lacrosse_postgame.html
    sends (camelCase keys from the form's submitForm payload)."""
    raw = {
        "sport": "lacrosse",
        "formType": "postgame",
        "name": "Jordan Alvarez",
        "email": "jordan@example.com",
        "program": "Conestoga Varsity",
        "gender": gender,
        "weekLabel": "Week 3 · April 13–19",
        "hadGame": had_game,
        "opponent": "Penn Charter",
        "result": result,
        "goalsFor": gf,
        "goalsAgainst": ga,
        "shots": "32",
        "goalsScored": gf,
        "groundBallsWon": "24",
        "turnovers": "12",
        "clearsSuccessful": "15",
        "clearsAttempted": "18",
        "emoGoals": "3",
        "emoAttempts": "5",
        "emdStops": "4",
        "emdAttempts": "6",
        "faceoffsWon": "13",
        "faceoffsLost": "9",
        "drawsWon": "",
        "drawsLost": "",
        "resultFeel": "Scoreline reflects the game",
        "bestMoments": "Ground ball culture improved, Clearing cleaned up",
        "didntLand": "EMO regressed",
        "playerStandout": "#14 — quiet week before, took over the second half",
        "confidenceLevel": "4",
        "oneThingToFix": fix,
        "oneThingToProtect": protect,
        "extraNotes": "Starting attackman tweaked a hamstring.",
    }
    raw.update(extra_raw)
    return {
        "intake_id": "lax0001",
        "slug": slug,
        "coach_name": "Jordan Alvarez",
        "coach_email": "jordan@example.com",
        "sport": "lacrosse",
        "form_type": "postgame",
        "extras": {"hadGame": had_game, "gender": gender},
        "raw": raw,
        "stored_at": "2026-04-24T12:00:00+00:00",
    }


def test_get_latest_postgame_without_sport_filter_is_backcompat():
    """No sport arg == legacy behaviour: last matching slug wins regardless
    of sport. Existing callers/fixtures that predate the sport arg must keep
    working unchanged."""
    from app.postgame import reset_postgame_store_for_tests
    from app.postgame_store import get_latest_postgame

    with tempfile.TemporaryDirectory() as td:
        store_path = Path(td) / "postgame_intakes.jsonl"
        _write_lines(store_path, [
            _make_pg_record("shared-slug", result="W", gf="10", ga="7"),
            _make_lax_pg_record("shared-slug", result="L", gf="6", ga="11"),
        ])
        reset_postgame_store_for_tests(store_path)
        try:
            raw = get_latest_postgame("shared-slug")  # no sport arg
        finally:
            reset_postgame_store_for_tests(None)

    assert raw is not None
    # Last-in-file wins and that's the lacrosse row.
    assert raw["result"] == "L"


def test_get_latest_postgame_filters_by_sport_lacrosse():
    """A lacrosse coach must NOT get a water-polo retro even if the slug
    matches — sport=lacrosse filters out the waterpolo rows."""
    from app.postgame import reset_postgame_store_for_tests
    from app.postgame_store import get_latest_postgame

    with tempfile.TemporaryDirectory() as td:
        store_path = Path(td) / "postgame_intakes.jsonl"
        _write_lines(store_path, [
            _make_pg_record("jamie-rivera", result="W", gf="10", ga="7"),
            _make_lax_pg_record("jamie-rivera", result="L", gf="6", ga="11"),
            _make_pg_record("jamie-rivera", result="T", gf="8", ga="8"),
        ])
        reset_postgame_store_for_tests(store_path)
        try:
            raw = get_latest_postgame("jamie-rivera", "lacrosse")
        finally:
            reset_postgame_store_for_tests(None)

    assert raw is not None
    # Only one lacrosse row exists — it must be the one returned regardless
    # of the trailing water-polo row.
    assert raw["result"] == "L"
    assert raw["goalsFor"] == "6"
    # Lacrosse-only field present.
    assert raw.get("groundBallsWon") == "24"


def test_get_latest_postgame_filters_by_sport_waterpolo():
    """A water-polo coach must NOT get a lacrosse retro — sport=waterpolo
    filters out the lacrosse rows."""
    from app.postgame import reset_postgame_store_for_tests
    from app.postgame_store import get_latest_postgame

    with tempfile.TemporaryDirectory() as td:
        store_path = Path(td) / "postgame_intakes.jsonl"
        _write_lines(store_path, [
            _make_pg_record("jamie-rivera", result="W", gf="10", ga="7"),
            _make_lax_pg_record("jamie-rivera", result="L", gf="6", ga="11"),
            _make_pg_record("jamie-rivera", result="T", gf="8", ga="8"),
        ])
        reset_postgame_store_for_tests(store_path)
        try:
            raw = get_latest_postgame("jamie-rivera", "waterpolo")
        finally:
            reset_postgame_store_for_tests(None)

    assert raw is not None
    # Last-in-file WATERPOLO row wins (T, 8-8), not the lacrosse L row.
    assert raw["result"] == "T"
    # Water-polo-only field present.
    assert raw.get("pp6Goals") == "2"
    # Lacrosse-only field absent.
    assert "groundBallsWon" not in raw


def test_get_latest_postgame_sport_filter_is_case_insensitive():
    from app.postgame import reset_postgame_store_for_tests
    from app.postgame_store import get_latest_postgame

    with tempfile.TemporaryDirectory() as td:
        store_path = Path(td) / "postgame_intakes.jsonl"
        _write_lines(store_path, [_make_lax_pg_record("coach-x")])
        reset_postgame_store_for_tests(store_path)
        try:
            raw_upper = get_latest_postgame("coach-x", "LACROSSE")
            raw_mixed = get_latest_postgame("coach-x", "Lacrosse")
        finally:
            reset_postgame_store_for_tests(None)

    assert raw_upper is not None and raw_upper.get("result") == "W"
    assert raw_mixed is not None and raw_mixed.get("result") == "W"


def test_get_latest_postgame_lacrosse_filter_returns_none_when_only_waterpolo():
    """If the store has water-polo rows only for this slug, a lacrosse
    lookup must return None — not silently fall back to water polo."""
    from app.postgame import reset_postgame_store_for_tests
    from app.postgame_store import get_latest_postgame

    with tempfile.TemporaryDirectory() as td:
        store_path = Path(td) / "postgame_intakes.jsonl"
        _write_lines(store_path, [
            _make_pg_record("jamie-rivera", result="W"),
            _make_pg_record("jamie-rivera", result="L"),
        ])
        reset_postgame_store_for_tests(store_path)
        try:
            raw = get_latest_postgame("jamie-rivera", "lacrosse")
        finally:
            reset_postgame_store_for_tests(None)

    assert raw is None


# ---------------------------------------------------------------------------
# 15. Pipeline: lacrosse intake passes sport to the store
# ---------------------------------------------------------------------------

def test_pipeline_passes_sport_to_postgame_store():
    """The pipeline must invoke `get_latest_postgame(slug, sport)` with the
    sport from the intake so cross-sport retros are never pulled in."""
    from app import pipeline
    from app.github_deploy import DeployResult

    captured_args = {}

    def fake_get_latest(slug, sport=None):
        captured_args["slug"] = slug
        captured_args["sport"] = sport
        return None  # no context — we only care about the call shape

    def fake_generate_plan(intake, postgame_context=None):
        return _stub_claude_response(int(intake.get("week") or 1))

    fake_deploy = DeployResult("u", "u", "sha", "a", "b", 2)
    with patch.object(pipeline, "discover_next_week_number", return_value=2), \
         patch.object(pipeline, "get_latest_postgame",
                      side_effect=fake_get_latest), \
         patch.object(pipeline, "generate_plan",
                      side_effect=fake_generate_plan), \
         patch.object(pipeline, "deploy_plans", return_value=fake_deploy), \
         patch.object(pipeline, "send_coach_email") as mock_email:
        mock_email.return_value.message_id = "msg-x"
        pipeline.run_pipeline({
            "intake_id": "lax-1",
            "slug": "jordan-alvarez",
            "coach_name": "Jordan Alvarez",
            "coach_email": "jordan@example.com",
            "sport": "lacrosse",
        })

    assert captured_args["slug"] == "jordan-alvarez"
    assert captured_args["sport"] == "lacrosse"


def test_pipeline_lacrosse_only_pulls_lacrosse_retro():
    """End-to-end through the real store: a lacrosse intake for a coach who
    has BOTH a water-polo retro and a lacrosse retro on file must only
    receive the lacrosse retro's fields in the prompt context."""
    from app import pipeline
    from app.github_deploy import DeployResult
    from app.postgame import reset_postgame_store_for_tests

    captured = {}

    def fake_generate_plan(intake, postgame_context=None):
        captured["postgame_context"] = postgame_context
        captured["sport"] = intake.get("sport")
        return _stub_claude_response(int(intake.get("week") or 1))

    with tempfile.TemporaryDirectory() as td:
        store_path = Path(td) / "postgame_intakes.jsonl"
        # Dual-sport slug collision — lacrosse row is NOT last, so the
        # sport filter (not recency) is what picks the right row.
        _write_lines(store_path, [
            _make_lax_pg_record(
                "dual-sport", result="W", gf="12", ga="9",
                fix="Clearing", protect="Transition speed",
            ),
            _make_pg_record("dual-sport", result="L", gf="4", ga="9"),
        ])
        reset_postgame_store_for_tests(store_path)
        try:
            fake_deploy = DeployResult("u", "u", "sha", "a", "b", 3)
            with patch.object(pipeline, "discover_next_week_number", return_value=3), \
                 patch.object(pipeline, "generate_plan",
                              side_effect=fake_generate_plan), \
                 patch.object(pipeline, "deploy_plans", return_value=fake_deploy), \
                 patch.object(pipeline, "send_coach_email") as mock_email:
                mock_email.return_value.message_id = "msg-x"
                result = pipeline.run_pipeline({
                    "intake_id": "i1",
                    "slug": "dual-sport",
                    "coach_name": "Dual Sport",
                    "coach_email": "d@x.com",
                    "sport": "lacrosse",
                })
        finally:
            reset_postgame_store_for_tests(None)

    assert result["ok"] is True
    assert captured["sport"] == "lacrosse"
    pg = captured["postgame_context"]
    assert pg is not None, "lacrosse pipeline must receive lacrosse retro"
    # Lacrosse-specific fields — proves we picked the lacrosse row.
    assert pg["groundBallsWon"] == "24"
    assert pg["oneThingToFix"] == "Clearing"
    assert pg["oneThingToProtect"] == "Transition speed"
    # And water-polo-only keys must not be present.
    assert "pp6Goals" not in pg
    assert "ejectionsDrawnFor" not in pg


def test_run_lacrosse_pipeline_gets_lacrosse_retro_injected():
    """The lacrosse entry point (`run_lacrosse_pipeline`) delegates to
    `run_pipeline`, so the end-to-end lacrosse path must receive the
    lacrosse retro on Week 2+ — same as water polo does for its sport."""
    from app import lacrosse, pipeline
    from app.github_deploy import DeployResult
    from app.postgame import reset_postgame_store_for_tests

    captured = {}

    def fake_generate_plan(intake, postgame_context=None):
        captured["postgame_context"] = postgame_context
        captured["intake_sport"] = intake.get("sport")
        return _stub_claude_response(int(intake.get("week") or 1))

    with tempfile.TemporaryDirectory() as td:
        store_path = Path(td) / "postgame_intakes.jsonl"
        _write_lines(store_path, [_make_lax_pg_record(
            "jordan-alvarez", result="L", gf="8", ga="10",
            fix="EMO", protect="Ground ball culture",
        )])
        reset_postgame_store_for_tests(store_path)
        try:
            fake_deploy = DeployResult("u", "u", "sha", "a", "b", 2)
            with patch.object(pipeline, "discover_next_week_number", return_value=2), \
                 patch.object(pipeline, "generate_plan",
                              side_effect=fake_generate_plan), \
                 patch.object(pipeline, "deploy_plans", return_value=fake_deploy), \
                 patch.object(pipeline, "send_coach_email") as mock_email:
                mock_email.return_value.message_id = "msg-x"
                result = lacrosse.run_lacrosse_pipeline({
                    "intake_id": "lax-2",
                    "slug": "jordan-alvarez",
                    "coach_name": "Jordan Alvarez",
                    "coach_email": "jordan@example.com",
                    # NOTE: sport missing on purpose — lacrosse.py must
                    # stamp it so the store filter still works.
                })
        finally:
            reset_postgame_store_for_tests(None)

    assert result["ok"] is True
    assert captured["intake_sport"] == "lacrosse"
    pg = captured["postgame_context"]
    assert pg is not None
    assert pg["result"] == "L"
    assert pg["oneThingToFix"] == "EMO"
    assert pg["oneThingToProtect"] == "Ground ball culture"


# ---------------------------------------------------------------------------
# 16. Lacrosse prompt block — terminology must be lacrosse-flavoured
# ---------------------------------------------------------------------------

def test_generate_plan_lacrosse_block_uses_lacrosse_terminology():
    """For sport=lacrosse, the WEEK N-1 REVIEW block must use lacrosse
    terminology (EMO, EMD, ground balls, clearing, face-offs) and NOT
    water-polo terminology (6x5, 5x6, ejections)."""
    from app import claude_client

    intake = {
        "intake_id": "lax-1",
        "slug": "jordan-alvarez",
        "week": 2,
        "sport": "lacrosse",
    }
    postgame = {
        "hadGame": "Yes",
        "gender": "Boys",
        "opponent": "Penn Charter",
        "result": "L",
        "goalsFor": "8",
        "goalsAgainst": "11",
        "shots": "32",
        "groundBallsWon": "18",
        "turnovers": "14",
        "clearsSuccessful": "12",
        "clearsAttempted": "17",
        "emoGoals": "1",
        "emoAttempts": "4",
        "emdStops": "2",
        "emdAttempts": "5",
        "faceoffsWon": "9",
        "faceoffsLost": "13",
        "resultFeel": "Scoreline flatters us",
        "bestMoments": "Ground ball culture improved, Clearing cleaned up",
        "didntLand": "EMO regressed",
        "playerStandout": "#14 Rivera — quiet week before, took over the second half",
        "confidenceLevel": "3",
        "oneThingToFix": "Ground balls",
        "oneThingToProtect": "Ground ball culture",
        "extraNotes": "Starting attackman tweaked a hamstring.",
    }
    captured = {}

    class FakeMsg:
        content = [type("B", (), {"type": "text",
                                   "text": _stub_claude_response(2)})()]
        stop_reason = "end_turn"

    class FakeMessages:
        def create(self, **kwargs):
            captured["messages"] = kwargs["messages"]
            return FakeMsg()

    class FakeClient:
        messages = FakeMessages()

    with patch.object(claude_client, "_client", return_value=FakeClient()):
        claude_client.generate_plan(intake, postgame_context=postgame)

    user_msg = captured["messages"][0]["content"]

    # Required lacrosse-flavoured lines.
    for needle in [
        "WEEK 1 REVIEW",
        "Had a game this week: Yes",
        "Opponent: Penn Charter",
        "Result (W/L/T): L",
        "8\u201311",  # en-dash score
        "Shots (total): 32",
        "Ground balls won: 18",
        "Turnovers: 14",
        "Clearing (successful / attempted): 12 / 17",
        "EMO (man-up) conversion (goals / attempts): 1 / 4",
        "EMD (man-down) stops (stops / attempts): 2 / 5",
        "Face-offs (won / lost): 9 / 13",
        "Result feel: Scoreline flatters us",
        "Best moment of the week: Ground ball culture improved, Clearing cleaned up",
        "What didn't land: EMO regressed",
        "Player who stood out: #14 Rivera",
        "Confidence going into Week 2 (1\u20135): 3",
        "One thing to fix: Ground balls",
        "One thing to protect: Ground ball culture",
        "Extra notes: Starting attackman tweaked a hamstring.",
    ]:
        assert needle in user_msg, f"missing lacrosse line: {needle!r}"

    # Water-polo terminology MUST NOT appear in a lacrosse block.
    for forbidden in [
        "6x5 conversion",
        "5x6 stops",
        "Ejections drawn",
        "Ejections against",
        "Steals:",
    ]:
        assert forbidden not in user_msg, (
            f"lacrosse block leaked water-polo terminology: {forbidden!r}"
        )


def test_generate_plan_lacrosse_girls_block_uses_draw_controls():
    """For a girls program, the block must show Draw controls (won/lost)
    instead of Face-offs."""
    from app import claude_client

    intake = {
        "intake_id": "lax-g-1",
        "slug": "sam-ortiz",
        "week": 4,
        "sport": "lacrosse",
    }
    postgame = {
        "hadGame": "Yes",
        "gender": "Girls",
        "opponent": "Notre Dame",
        "result": "W",
        "goalsFor": "14",
        "goalsAgainst": "9",
        "shots": "29",
        "groundBallsWon": "22",
        "turnovers": "11",
        "clearsSuccessful": "14",
        "clearsAttempted": "16",
        "emoGoals": "2",
        "emoAttempts": "3",
        "emdStops": "3",
        "emdAttempts": "4",
        "drawsWon": "15",
        "drawsLost": "9",
        "resultFeel": "Scoreline reflects the game",
        "bestMoments": "Team competed hard",
        "didntLand": "Nothing major",
        "playerStandout": "#3 Park",
        "confidenceLevel": "5",
        "oneThingToFix": "Settled offense",
        "oneThingToProtect": "Team energy",
        "extraNotes": "",
    }
    captured = {}

    class FakeMsg:
        content = [type("B", (), {"type": "text",
                                   "text": _stub_claude_response(4)})()]
        stop_reason = "end_turn"

    class FakeMessages:
        def create(self, **kwargs):
            captured["messages"] = kwargs["messages"]
            return FakeMsg()

    class FakeClient:
        messages = FakeMessages()

    with patch.object(claude_client, "_client", return_value=FakeClient()):
        claude_client.generate_plan(intake, postgame_context=postgame)

    user_msg = captured["messages"][0]["content"]
    assert "Draw controls (won / lost): 15 / 9" in user_msg
    # Face-offs line must not appear for a girls program.
    assert "Face-offs (won / lost)" not in user_msg


def test_generate_plan_lacrosse_no_game_week_still_injects_practice_fields():
    """Practice-only lacrosse week: the game-stats lines drop out but
    best-moments / didn't-land / confidence / fix / protect / notes remain,
    with lacrosse-form field names (`bestMoments`, `playerStandout`,
    `confidenceLevel`) honoured."""
    from app import claude_client

    intake = {
        "intake_id": "lax-1",
        "slug": "jordan-alvarez",
        "week": 5,
        "sport": "lacrosse",
    }
    postgame = {
        "hadGame": "No",
        "gender": "Boys",
        "bestMoments": "A drill clicked, Player breakthrough",
        "didntLand": "Ground balls poor",
        "playerStandout": "#9 Park (first time on clear)",
        "confidenceLevel": "4",
        "oneThingToFix": "Ground balls",
        "oneThingToProtect": "Ground ball culture",
        "extraNotes": "Scrimmage cancelled; shifted to a skills block.",
    }
    captured = {}

    class FakeMsg:
        content = [type("B", (), {"type": "text",
                                   "text": _stub_claude_response(5)})()]
        stop_reason = "end_turn"

    class FakeMessages:
        def create(self, **kwargs):
            captured["messages"] = kwargs["messages"]
            return FakeMsg()

    class FakeClient:
        messages = FakeMessages()

    with patch.object(claude_client, "_client", return_value=FakeClient()):
        claude_client.generate_plan(intake, postgame_context=postgame)

    user_msg = captured["messages"][0]["content"]

    assert "WEEK 4 REVIEW" in user_msg
    assert "Had a game this week: No" in user_msg

    # Game-stat lines OMITTED in a no-game block.
    for needle in [
        "Result (W/L/T)",
        "Score (GF",
        "Shots (total)",
        "Ground balls won:",
        "Clearing (successful",
        "EMO (man-up)",
        "EMD (man-down)",
        "Face-offs",
        "Draw controls",
        "Result feel",
    ]:
        assert needle not in user_msg, f"unexpected game-stat line: {needle!r}"

    # Practice-focused lines present, using the LACROSSE field names.
    for needle in [
        "Best moment of the week: A drill clicked, Player breakthrough",
        "What didn't land: Ground balls poor",
        "Player who stood out: #9 Park (first time on clear)",
        "Confidence going into Week 5 (1\u20135): 4",
        "One thing to fix: Ground balls",
        "One thing to protect: Ground ball culture",
        "Extra notes: Scrimmage cancelled; shifted to a skills block.",
    ]:
        assert needle in user_msg, f"missing practice-focus line: {needle!r}"


def test_waterpolo_block_unaffected_by_sport_dispatch():
    """Regression guard: when sport=waterpolo (or unset), the existing
    water-polo block must be identical to the Session 10 shape — no
    lacrosse terminology smuggled in."""
    from app import claude_client

    intake = {"intake_id": "wp-1", "slug": "x", "week": 2, "sport": "waterpolo"}
    postgame = {
        "hadGame": "Yes",
        "opponent": "St. Mary's Prep",
        "result": "W",
        "goalsFor": "12",
        "goalsAgainst": "9",
        "shotTotal": "34",
        "ejectionsDrawnFor": "5",
        "ejectionsDrawnAgainst": "3",
        "steals": "8",
        "turnovers": "11",
        "pp6Goals": "3",
        "pp6Attempts": "5",
        "md5Stops": "2",
        "md5Attempts": "4",
        "resultFeel": "earned",
        "bestMoment": "counter-attack speed",
        "didntLand": "hole-set entry",
        "standoutPlayer": "#7 Alex",
        "confidenceNextWeek": "4",
        "oneThingToFix": "hole-set entry",
        "oneThingToProtect": "press defence",
        "extraNotes": "two starters back from flu",
    }
    captured = {}

    class FakeMsg:
        content = [type("B", (), {"type": "text",
                                   "text": _stub_claude_response(2)})()]
        stop_reason = "end_turn"

    class FakeMessages:
        def create(self, **kwargs):
            captured["messages"] = kwargs["messages"]
            return FakeMsg()

    class FakeClient:
        messages = FakeMessages()

    with patch.object(claude_client, "_client", return_value=FakeClient()):
        claude_client.generate_plan(intake, postgame_context=postgame)

    user_msg = captured["messages"][0]["content"]
    # Water polo markers present.
    assert "6x5 conversion (goals / attempts): 3 / 5" in user_msg
    assert "5x6 stops (stops / attempts): 2 / 4" in user_msg
    assert "Ejections drawn for: 5" in user_msg
    # Lacrosse markers absent.
    for forbidden in [
        "EMO (man-up)",
        "EMD (man-down)",
        "Ground balls won",
        "Clearing (successful",
        "Face-offs (won / lost)",
        "Draw controls",
    ]:
        assert forbidden not in user_msg


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
