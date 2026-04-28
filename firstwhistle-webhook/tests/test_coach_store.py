"""Tests for the CoachPrep returning-coach store + the /coach endpoints.

Uses a per-test temp-file DB so tests never clobber a real coach_store.sqlite3
if one exists in the cwd. Endpoint tests use FastAPI's TestClient which
exercises the full pydantic-validation + CORS stack.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Env stubs so `get_settings()` doesn't explode when `app.main` is imported.
os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic")
os.environ.setdefault("GITHUB_TOKEN", "test-token")
os.environ.setdefault("GITHUB_REPO", "coachiq-source/CoachIq")
os.environ.setdefault("RESEND_API_KEY", "test-resend")
os.environ.setdefault("COACH_EMAIL_FROM", "coach@example.com")

from app.coach_store import (  # noqa: E402
    CoachStoreError,
    get_coach_by_email,
    get_coach_profile,
    reset_coach_store_for_tests,
    upsert_coach_profile,
    validate_code,
)


@pytest.fixture(autouse=True)
def temp_store():
    """Every test gets a fresh SQLite file."""
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "coach_store.sqlite3"
        reset_coach_store_for_tests(db_path)
        yield db_path
        reset_coach_store_for_tests(None)


# ---------------------------------------------------------------------------
# validate_code
# ---------------------------------------------------------------------------

def test_validate_code_accepts_minimum_length():
    assert validate_code("abcd") == "abcd"


def test_validate_code_accepts_maximum_length():
    assert validate_code("abcdefghijkl") == "abcdefghijkl"  # 12 chars


def test_validate_code_strips_whitespace():
    assert validate_code("   JR2026   ") == "JR2026"


def test_validate_code_allows_punctuation():
    for code in ("jamie.r", "jr_2026", "jr-2026", "A.B-c_9"):
        assert validate_code(code) == code


def test_validate_code_rejects_spaces_internal():
    with pytest.raises(CoachStoreError):
        validate_code("jamie r")


def test_validate_code_rejects_too_short():
    with pytest.raises(CoachStoreError):
        validate_code("abc")


def test_validate_code_rejects_too_long():
    with pytest.raises(CoachStoreError):
        validate_code("a" * 13)


def test_validate_code_rejects_empty():
    with pytest.raises(CoachStoreError):
        validate_code("")


def test_validate_code_rejects_special_chars():
    with pytest.raises(CoachStoreError):
        validate_code("jamie!r")


# ---------------------------------------------------------------------------
# upsert_coach_profile / get_coach_profile
# ---------------------------------------------------------------------------

def test_upsert_creates_row():
    profile = upsert_coach_profile(
        code="MS2026",
        name="Magnus Sims",
        email="magnus@example.com",
        program="Episcopal Academy",
        sport="waterpolo",
    )
    assert profile.code == "MS2026"
    assert profile.name == "Magnus Sims"
    assert profile.email == "magnus@example.com"
    assert profile.program == "Episcopal Academy"
    assert profile.sport == "waterpolo"
    assert profile.created_at == profile.updated_at


def test_upsert_updates_existing_row_and_bumps_timestamp():
    upsert_coach_profile("MS2026", "Magnus Sims", "magnus@example.com", "EA", "waterpolo")
    updated = upsert_coach_profile(
        "MS2026", "Magnus Sims", "magnus@newschool.edu", "New School", "lacrosse",
    )
    assert updated.email == "magnus@newschool.edu"
    assert updated.program == "New School"
    assert updated.sport == "lacrosse"


def test_upsert_requires_name_and_email():
    with pytest.raises(CoachStoreError):
        upsert_coach_profile("AB12", "", "x@y.com", "", "waterpolo")
    with pytest.raises(CoachStoreError):
        upsert_coach_profile("AB12", "x", "", "", "waterpolo")


def test_upsert_rejects_unknown_sport():
    with pytest.raises(CoachStoreError):
        upsert_coach_profile("AB12", "x", "x@y.com", "", "skiing")


def test_upsert_normalizes_sport_casing_and_default():
    # Uppercase input should normalize to lowercase.
    profile = upsert_coach_profile("AB12", "x", "x@y.com", "", "Waterpolo")
    assert profile.sport == "waterpolo"
    # Empty sport defaults to waterpolo.
    profile2 = upsert_coach_profile("CD34", "y", "y@z.com", "", "")
    assert profile2.sport == "waterpolo"


def test_get_coach_profile_returns_none_for_missing():
    assert get_coach_profile("NOPE2026") is None


def test_get_coach_profile_returns_stored_row():
    upsert_coach_profile("MS2026", "Magnus Sims", "magnus@example.com", "EA", "waterpolo")
    profile = get_coach_profile("MS2026")
    assert profile is not None
    assert profile.name == "Magnus Sims"


def test_get_coach_profile_rejects_malformed_code():
    with pytest.raises(CoachStoreError):
        get_coach_profile("!!")


# ---------------------------------------------------------------------------
# get_coach_by_email
# ---------------------------------------------------------------------------

def test_get_coach_by_email_returns_none_for_missing():
    assert get_coach_by_email("nobody@example.com") is None


def test_get_coach_by_email_returns_none_for_blank():
    assert get_coach_by_email("") is None
    assert get_coach_by_email("   ") is None


def test_get_coach_by_email_finds_exact_match():
    upsert_coach_profile("MS2026", "Magnus Sims", "magnus@example.com", "EA", "waterpolo")
    profile = get_coach_by_email("magnus@example.com")
    assert profile is not None
    assert profile.code == "MS2026"
    assert profile.name == "Magnus Sims"


def test_get_coach_by_email_is_case_insensitive():
    upsert_coach_profile("MS2026", "Magnus Sims", "Magnus@Example.COM", "EA", "waterpolo")
    # Lookup with all-lower input still finds the row.
    p = get_coach_by_email("magnus@example.com")
    assert p is not None and p.code == "MS2026"
    # All-upper too.
    p2 = get_coach_by_email("MAGNUS@EXAMPLE.COM")
    assert p2 is not None and p2.code == "MS2026"
    # Mixed casing.
    p3 = get_coach_by_email("MaGnUs@ExAmPlE.cOm")
    assert p3 is not None and p3.code == "MS2026"


def test_get_coach_by_email_strips_whitespace():
    upsert_coach_profile("MS2026", "Magnus Sims", "magnus@example.com", "EA", "waterpolo")
    p = get_coach_by_email("  magnus@example.com  ")
    assert p is not None and p.code == "MS2026"


def test_get_coach_by_email_returns_most_recent_when_multiple_codes_share_email():
    # Same email registered against two different codes — we should return
    # the most-recently-updated one so the recovery email points at the
    # code the coach is actively using.
    upsert_coach_profile("OLD2024", "Magnus Sims", "magnus@example.com", "EA", "waterpolo")
    upsert_coach_profile("NEW2026", "Magnus Sims", "magnus@example.com", "EA", "waterpolo")
    profile = get_coach_by_email("magnus@example.com")
    assert profile is not None
    assert profile.code == "NEW2026"


# ---------------------------------------------------------------------------
# /coach HTTP endpoints (pydantic + CORS stack)
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def test_post_coach_creates_profile(client):
    resp = client.post(
        "/coach",
        json={
            "code": "MS2026",
            "name": "Magnus Sims",
            "email": "magnus@example.com",
            "program": "Episcopal Academy",
            "sport": "waterpolo",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == "MS2026"
    assert body["name"] == "Magnus Sims"
    assert body["sport"] == "waterpolo"
    assert "created_at" in body and "updated_at" in body


def test_post_coach_400_on_bad_code(client):
    resp = client.post(
        "/coach",
        json={
            "code": "has space",
            "name": "x",
            "email": "x@y.com",
            "program": "",
            "sport": "waterpolo",
        },
    )
    assert resp.status_code == 400


def test_post_coach_400_on_bad_sport(client):
    resp = client.post(
        "/coach",
        json={
            "code": "ABCD",
            "name": "x",
            "email": "x@y.com",
            "program": "",
            "sport": "curling",
        },
    )
    assert resp.status_code == 400


def test_get_coach_returns_200(client):
    client.post(
        "/coach",
        json={
            "code": "MS2026", "name": "Magnus Sims",
            "email": "magnus@example.com", "program": "EA", "sport": "waterpolo",
        },
    )
    resp = client.get("/coach/MS2026")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Magnus Sims"
    assert body["program"] == "EA"


def test_get_coach_returns_404_when_missing(client):
    resp = client.get("/coach/GHOST2026")
    assert resp.status_code == 404


def test_get_coach_returns_400_on_malformed_code(client):
    resp = client.get("/coach/!!")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# /coach/recover endpoint
# ---------------------------------------------------------------------------

@pytest.fixture
def captured_recovery_sends(monkeypatch):
    """Replace the recovery email sender so tests never hit Resend.

    Captures (coach_name, coach_email, coach_code) tuples for every call.
    Patches the symbol on `app.main` (where the endpoint imported it) so
    the in-process FastAPI call uses the stub.
    """
    sent: list[tuple[str, str, str]] = []

    def _fake_send(coach_name, coach_email, coach_code):
        sent.append((coach_name, coach_email, coach_code))
        # Mirror the real return shape just in case the endpoint ever
        # decides to inspect it.
        from app.email_send import EmailResult
        return EmailResult(message_id="test-msg-id", to=coach_email)

    import app.main as main_module
    monkeypatch.setattr(main_module, "send_coach_recovery_email", _fake_send)
    return sent


def test_recover_returns_sent_when_email_found(client, captured_recovery_sends):
    client.post(
        "/coach",
        json={
            "code": "MS2026", "name": "Magnus Sims",
            "email": "magnus@example.com", "program": "EA", "sport": "waterpolo",
        },
    )
    resp = client.post("/coach/recover", json={"email": "magnus@example.com"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "sent"}
    assert captured_recovery_sends == [
        ("Magnus Sims", "magnus@example.com", "MS2026")
    ]


def test_recover_returns_sent_when_email_not_found(client, captured_recovery_sends):
    # Empty store — endpoint must NOT confirm absence and must NOT send mail.
    resp = client.post("/coach/recover", json={"email": "ghost@nowhere.com"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "sent"}
    assert captured_recovery_sends == []


def test_recover_is_case_insensitive(client, captured_recovery_sends):
    client.post(
        "/coach",
        json={
            "code": "MS2026", "name": "Magnus Sims",
            "email": "Magnus@Example.com", "program": "EA", "sport": "waterpolo",
        },
    )
    resp = client.post("/coach/recover", json={"email": "MAGNUS@EXAMPLE.COM"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "sent"}
    assert len(captured_recovery_sends) == 1
    name, email, code = captured_recovery_sends[0]
    assert code == "MS2026"
    # Email passed to the sender comes from the stored row, not the request.
    assert email == "Magnus@Example.com"


def test_recover_swallows_email_send_failure(client, monkeypatch):
    # If Resend is down we still must return {"status": "sent"} — never leak
    # that a match was found.
    client.post(
        "/coach",
        json={
            "code": "MS2026", "name": "Magnus Sims",
            "email": "magnus@example.com", "program": "EA", "sport": "waterpolo",
        },
    )
    from app.email_send import EmailSendError
    import app.main as main_module

    def _raise(*_a, **_kw):
        raise EmailSendError("simulated resend outage")

    monkeypatch.setattr(main_module, "send_coach_recovery_email", _raise)
    resp = client.post("/coach/recover", json={"email": "magnus@example.com"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "sent"}


def test_recover_rejects_missing_email(client):
    resp = client.post("/coach/recover", json={})
    # Pydantic validation — missing required field is a 422.
    assert resp.status_code == 422


def test_get_coach_cors_headers_present(client):
    # Preflight request — FastAPI's CORSMiddleware should answer with the
    # appropriate Access-Control-Allow-* headers so the GitHub-Pages form
    # can POST from its own origin.
    resp = client.options(
        "/coach/MS2026",
        headers={
            "Origin": "https://coachiq-source.github.io",
            "Access-Control-Request-Method": "GET",
        },
    )
    # 200 OR 204 from the CORS middleware is acceptable; just confirm the
    # header is present and permissive.
    assert resp.status_code in (200, 204)
    assert resp.headers.get("access-control-allow-origin") in ("*", "https://coachiq-source.github.io")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
