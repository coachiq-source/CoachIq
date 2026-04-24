"""Tests for the post-game (Week-in-Review) intake handler (Session 9).

Covers:
  1. `peek_form_type` extracts the form_type selector without validating.
  2. `parse_postgame_payload` is lenient: no coach_name, no email → no raise.
  3. `parse_postgame_payload` maps camelCase `coachName` / `coachEmail`
     (which is what waterpolo_postgame.html actually sends).
  4. `run_postgame_handler` logs the ops grep-line and appends to the JSONL
     store.
  5. The waterpolo webhook routes `formType == "postgame"` to the post-game
     handler WITHOUT running the strict weekly parser (which would 422 on
     missing coach_name).
  6. The waterpolo webhook still 422s on a regular weekly intake that's
     missing coach_name — the lenient path is only for postgame.
  7. The waterpolo webhook still routes `formType == "gameprep"` correctly.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
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
# A known HMAC secret so we can sign test payloads deterministically.
os.environ.setdefault("FORMSPREE_SECRET_WATERPOLO", "test-waterpolo-secret")


# ---------------------------------------------------------------------------
# 1. peek_form_type
# ---------------------------------------------------------------------------

def test_peek_form_type_snake_case():
    from app.intake import peek_form_type
    assert peek_form_type({"form_type": "postgame"}) == "postgame"


def test_peek_form_type_camel_case():
    from app.intake import peek_form_type
    assert peek_form_type({"formType": "postgame"}) == "postgame"


def test_peek_form_type_lowercase_joined():
    from app.intake import peek_form_type
    assert peek_form_type({"formtype": "postgame"}) == "postgame"


def test_peek_form_type_missing_returns_empty_string():
    """Weekly-plan intakes don't send form_type. peek must return ""
    (not raise, not None) so the router falls through to the strict parser."""
    from app.intake import peek_form_type
    assert peek_form_type({"name": "x", "email": "x@y.com"}) == ""


def test_peek_form_type_unwraps_formspree_envelope():
    """Real Formspree posts `{"form": "<id>", "submission": {...}}`. The peek
    must look inside the envelope, not treat `form` as a form_type."""
    from app.intake import peek_form_type
    body = {
        "form": "myklwjnp",
        "submission": {"formType": "postgame", "name": "Jamie"},
    }
    assert peek_form_type(body) == "postgame"


def test_peek_form_type_lowercased_for_comparison():
    from app.intake import peek_form_type
    assert peek_form_type({"form_type": "POSTGAME"}) == "postgame"


def test_peek_form_type_handles_non_dict_gracefully():
    """Defensive: a weird non-dict body must not raise."""
    from app.intake import peek_form_type
    assert peek_form_type("not a dict") == ""  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 2. parse_postgame_payload — lenient
# ---------------------------------------------------------------------------

def test_parse_postgame_missing_name_and_email_does_not_raise():
    """The weekly-plan strict parser 422s on this body. The postgame parser
    must NOT — the retrospective is worth capturing even if fields are
    light."""
    from app.intake import parse_postgame_payload

    intake = parse_postgame_payload({
        "formType": "postgame",
        "hadGame": "No",
        "confidenceNextWeek": "7",
    })
    assert intake["form_type"] == "postgame"
    assert intake["coach_name"] == ""
    assert intake["coach_email"] == ""
    # intake_id is always present — the store uses it as the dedupe key.
    assert len(intake["intake_id"]) == 12
    # Fallback slug when no name was supplied.
    assert intake["slug"].startswith("coach-")


def test_parse_postgame_accepts_camelcase_coach_fields():
    """The real waterpolo_postgame.html submits coachName / coachEmail
    (camelCase). Verify we map them onto the canonical keys."""
    from app.intake import parse_postgame_payload

    intake = parse_postgame_payload({
        "formType": "postgame",
        "coachName": "Jamie Rivera",
        "coachEmail": "jamie@example.com",
        "coachCode": "JR2026",
    })
    assert intake["coach_name"] == "Jamie Rivera"
    assert intake["coach_email"] == "jamie@example.com"
    assert intake["coach_code"] == "JR2026"
    assert intake["slug"].startswith("jamie-rivera")


def test_parse_postgame_preserves_game_stats_in_extras():
    """Post-game-specific fields (hadGame, goalsFor, etc.) aren't canonical
    aliases, so they must land in `extras` untouched for the future pipeline."""
    from app.intake import parse_postgame_payload

    body = {
        "formType": "postgame",
        "name": "Jamie Rivera",
        "email": "jamie@example.com",
        "hadGame": "Yes",
        "opponent": "St. Mary's Prep",
        "goalsFor": "11",
        "goalsAgainst": "9",
        "confidenceNextWeek": "8",
        "bestMoment": "Counter-attack speed",
        "didntLand": "Hole-set entry",
    }
    intake = parse_postgame_payload(body)
    assert intake["extras"]["hadGame"] == "Yes"
    assert intake["extras"]["opponent"] == "St. Mary's Prep"
    assert intake["extras"]["goalsFor"] == "11"
    assert intake["extras"]["confidenceNextWeek"] == "8"


def test_parse_postgame_unwraps_formspree_envelope():
    from app.intake import parse_postgame_payload

    body = {
        "form": "myklwjnp",
        "submission": {
            "formType": "postgame",
            "coachName": "Jamie",
            "coachEmail": "jamie@example.com",
        },
    }
    intake = parse_postgame_payload(body)
    assert intake["coach_name"] == "Jamie"
    assert intake["coach_email"] == "jamie@example.com"


def test_parse_postgame_keeps_raw_body_for_triage():
    """The handler's whole job for now is archiving the raw payload — make
    sure the lenient parser holds onto it under `raw`."""
    from app.intake import parse_postgame_payload

    body = {
        "formType": "postgame",
        "coachName": "Jamie",
        "hadGame": "No",
        "extraNotes": "lost two players to flu",
    }
    intake = parse_postgame_payload(body)
    assert intake["raw"]["extraNotes"] == "lost two players to flu"


# ---------------------------------------------------------------------------
# 3. run_postgame_handler — log + store
# ---------------------------------------------------------------------------

def test_run_postgame_handler_stores_jsonl_line(caplog):
    """One submission → one JSONL line. The ops grep-line must appear in logs."""
    from app.postgame import (
        reset_postgame_store_for_tests,
        run_postgame_handler,
    )

    with tempfile.TemporaryDirectory() as td:
        store_path = Path(td) / "postgame_intakes.jsonl"
        reset_postgame_store_for_tests(store_path)
        try:
            with caplog.at_level("INFO", logger="firstwhistle.postgame"):
                result = run_postgame_handler({
                    "intake_id": "abc123def456",
                    "slug": "jamie-rivera",
                    "coach_name": "Jamie Rivera",
                    "coach_email": "jamie@example.com",
                    "sport": "waterpolo",
                    "form_type": "postgame",
                    "extras": {"hadGame": "Yes", "opponent": "St. Mary's"},
                    "raw": {"coachName": "Jamie Rivera"},
                })

            assert result["ok"] is True
            assert result["sport"] == "waterpolo"
            assert result["coach_name"] == "Jamie Rivera"

            # JSONL file written, exactly one line, valid JSON.
            content = store_path.read_text(encoding="utf-8").splitlines()
            assert len(content) == 1
            record = json.loads(content[0])
            assert record["coach_name"] == "Jamie Rivera"
            assert record["form_type"] == "postgame"
            assert "stored_at" in record  # handler stamps a timestamp

            # Ops grep-line present.
            assert any(
                "postgame intake accepted" in rec.message
                and "coach=Jamie Rivera" in rec.message
                and "sport=waterpolo" in rec.message
                for rec in caplog.records
            ), [r.message for r in caplog.records]
        finally:
            reset_postgame_store_for_tests(None)


def test_run_postgame_handler_tolerates_missing_name(caplog):
    """If the coach didn't supply a name (anonymous submission, bad form
    config, …) the handler must still accept and store the submission."""
    from app.postgame import (
        reset_postgame_store_for_tests,
        run_postgame_handler,
    )

    with tempfile.TemporaryDirectory() as td:
        store_path = Path(td) / "postgame_intakes.jsonl"
        reset_postgame_store_for_tests(store_path)
        try:
            with caplog.at_level("INFO", logger="firstwhistle.postgame"):
                result = run_postgame_handler({
                    "intake_id": "no_name_here",
                    "slug": "coach-no_name_here",
                    "coach_name": "",
                    "coach_email": "",
                    "sport": "waterpolo",
                    "extras": {"hadGame": "No"},
                    "raw": {},
                })
            assert result["ok"] is True
            assert store_path.exists()
            # Log line should still print — using "(unknown)" for coach.
            assert any(
                "postgame intake accepted" in rec.message
                and "coach=(unknown)" in rec.message
                for rec in caplog.records
            )
        finally:
            reset_postgame_store_for_tests(None)


# ---------------------------------------------------------------------------
# 4. Webhook routing — the bug the user reported
# ---------------------------------------------------------------------------

def _sign_body(secret: str, raw_body: bytes, ts: int | None = None) -> tuple[str, int]:
    timestamp = ts if ts is not None else int(time.time())
    signed_payload = str(timestamp).encode("ascii") + b"." + raw_body
    digest = hmac.new(
        secret.encode("utf-8"),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()
    return f"t={timestamp},v1={digest}", timestamp


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def test_waterpolo_webhook_accepts_postgame_without_coach_name(client):
    """Reproduction of the reported bug: a postgame submission with
    `formType: postgame` and `name` (not `coach_name`) must be accepted,
    not rejected by the weekly-plan strict validator."""
    from app import main as main_mod

    body_dict = {
        "sport": "waterpolo",
        "formType": "postgame",
        # NOTE: no `coach_name` — deliberately uses `name` per the user spec.
        "name": "Jamie Rivera",
        "email": "jamie@example.com",
        "hadGame": "No",
        "confidenceNextWeek": "7",
    }
    raw = json.dumps(body_dict).encode("utf-8")
    sig, _ = _sign_body("test-waterpolo-secret", raw)

    # Don't actually store — just assert routing.
    with tempfile.TemporaryDirectory() as td:
        from app.postgame import reset_postgame_store_for_tests
        reset_postgame_store_for_tests(Path(td) / "postgame_intakes.jsonl")
        try:
            with patch.object(main_mod, "run_postgame_handler") as mock_handler:
                resp = client.post(
                    "/webhook/formspree/waterpolo",
                    data=raw,
                    headers={
                        "Content-Type": "application/json",
                        "Formspree-Signature": sig,
                    },
                )
        finally:
            reset_postgame_store_for_tests(None)

    assert resp.status_code == 202, resp.text
    payload = resp.json()
    assert payload["accepted"] is True
    assert payload["sport"] == "waterpolo"
    assert payload["form_type"] == "postgame"
    # The background task was scheduled with the lenient intake.
    mock_handler.assert_called_once()
    scheduled_intake = mock_handler.call_args.args[0]
    assert scheduled_intake["form_type"] == "postgame"
    assert scheduled_intake["coach_name"] == "Jamie Rivera"
    assert scheduled_intake["sport"] == "waterpolo"


def test_waterpolo_webhook_accepts_postgame_even_without_name(client):
    """Belt-and-braces: even a postgame submission with NO name field at
    all must not 422. The whole point of the lenient path is that triage
    is worth more than strict validation here."""
    from app import main as main_mod

    body_dict = {
        "sport": "waterpolo",
        "formType": "postgame",
        "hadGame": "No",
    }
    raw = json.dumps(body_dict).encode("utf-8")
    sig, _ = _sign_body("test-waterpolo-secret", raw)

    with tempfile.TemporaryDirectory() as td:
        from app.postgame import reset_postgame_store_for_tests
        reset_postgame_store_for_tests(Path(td) / "postgame_intakes.jsonl")
        try:
            with patch.object(main_mod, "run_postgame_handler"):
                resp = client.post(
                    "/webhook/formspree/waterpolo",
                    data=raw,
                    headers={
                        "Content-Type": "application/json",
                        "Formspree-Signature": sig,
                    },
                )
        finally:
            reset_postgame_store_for_tests(None)

    assert resp.status_code == 202, resp.text
    assert resp.json()["form_type"] == "postgame"


def test_waterpolo_webhook_still_422s_weekly_intake_missing_coach_name(client):
    """Regression guard: the lenient path is strictly scoped to postgame.
    A weekly-plan intake missing coach_name must STILL be rejected."""
    body_dict = {
        "sport": "waterpolo",
        # No formType → weekly-plan path.
        "email": "jamie@example.com",
    }
    raw = json.dumps(body_dict).encode("utf-8")
    sig, _ = _sign_body("test-waterpolo-secret", raw)

    resp = client.post(
        "/webhook/formspree/waterpolo",
        data=raw,
        headers={
            "Content-Type": "application/json",
            "Formspree-Signature": sig,
        },
    )
    assert resp.status_code == 422
    assert "coach_name" in resp.text.lower() or "name" in resp.text.lower()


def test_waterpolo_webhook_still_routes_gameprep(client):
    """Regression guard for Session 7: gameprep routing must be untouched."""
    from app import main as main_mod

    body_dict = {
        "sport": "waterpolo",
        "formType": "gameprep",
        "name": "Jamie Rivera",
        "email": "jamie@example.com",
        "opponent": "St. Mary's Prep",
    }
    raw = json.dumps(body_dict).encode("utf-8")
    sig, _ = _sign_body("test-waterpolo-secret", raw)

    with patch.object(main_mod, "run_gameprep_pipeline") as mock_gameprep, \
         patch.object(main_mod, "run_postgame_handler") as mock_postgame, \
         patch.object(main_mod, "run_pipeline") as mock_weekly:
        resp = client.post(
            "/webhook/formspree/waterpolo",
            data=raw,
            headers={
                "Content-Type": "application/json",
                "Formspree-Signature": sig,
            },
        )
    assert resp.status_code == 202, resp.text
    assert resp.json()["form_type"] == "gameprep"
    mock_gameprep.assert_called_once()
    mock_postgame.assert_not_called()
    mock_weekly.assert_not_called()


def test_waterpolo_webhook_default_routes_to_weekly(client):
    """Regression guard: an intake with no formType still hits the weekly
    two-document pipeline — this is the original, unbroken path."""
    from app import main as main_mod

    body_dict = {
        "sport": "waterpolo",
        "name": "Jamie Rivera",
        "email": "jamie@example.com",
        "team": "Riverside 14U",
        "focus": "counter-attack speed",
    }
    raw = json.dumps(body_dict).encode("utf-8")
    sig, _ = _sign_body("test-waterpolo-secret", raw)

    with patch.object(main_mod, "run_pipeline") as mock_weekly, \
         patch.object(main_mod, "run_gameprep_pipeline") as mock_gameprep, \
         patch.object(main_mod, "run_postgame_handler") as mock_postgame:
        resp = client.post(
            "/webhook/formspree/waterpolo",
            data=raw,
            headers={
                "Content-Type": "application/json",
                "Formspree-Signature": sig,
            },
        )
    assert resp.status_code == 202, resp.text
    assert resp.json()["form_type"] == "week"
    mock_weekly.assert_called_once()
    mock_gameprep.assert_not_called()
    mock_postgame.assert_not_called()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
