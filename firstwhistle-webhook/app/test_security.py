"""Tests for Formspree HMAC signature verification."""
from __future__ import annotations

import hashlib
import hmac
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.security import SignatureError, verify_formspree_signature  # noqa: E402


SECRET = "1d6dd2509aa6536428cfe0a8a776d6ca64be34339942661cc0748d679b0f6648"


def _sign(body: bytes, timestamp: int, secret: str = SECRET) -> str:
    payload = f"{timestamp}.".encode("ascii") + body
    sig = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={sig}"


def test_valid_signature_passes():
    body = b'{"form":"xyz","submission":{"name":"jane","email":"j@x.com"}}'
    ts = 1_776_971_121
    header = _sign(body, ts)
    # Override `now` so the timestamp looks fresh.
    verify_formspree_signature(
        raw_body=body,
        header_value=header,
        signing_secret=SECRET,
        now_fn=lambda: ts + 5,
    )


def test_tampered_body_fails():
    body = b'{"form":"xyz"}'
    ts = 1_776_971_121
    header = _sign(body, ts)
    with pytest.raises(SignatureError, match="mismatch"):
        verify_formspree_signature(
            raw_body=b'{"form":"tampered"}',
            header_value=header,
            signing_secret=SECRET,
            now_fn=lambda: ts + 5,
        )


def test_wrong_secret_fails():
    body = b'hello'
    ts = 1_776_971_121
    header = _sign(body, ts)
    with pytest.raises(SignatureError, match="mismatch"):
        verify_formspree_signature(
            raw_body=body,
            header_value=header,
            signing_secret="not-the-right-secret",
            now_fn=lambda: ts + 5,
        )


def test_expired_timestamp_fails():
    body = b'hello'
    ts = 1_776_971_121
    header = _sign(body, ts)
    with pytest.raises(SignatureError, match="tolerance"):
        verify_formspree_signature(
            raw_body=body,
            header_value=header,
            signing_secret=SECRET,
            tolerance_seconds=60,
            now_fn=lambda: ts + 3600,  # 1 hour later
        )


def test_future_timestamp_fails():
    body = b'hello'
    ts = 1_776_971_121
    header = _sign(body, ts)
    with pytest.raises(SignatureError, match="tolerance"):
        verify_formspree_signature(
            raw_body=body,
            header_value=header,
            signing_secret=SECRET,
            tolerance_seconds=60,
            now_fn=lambda: ts - 3600,  # clock skew: we're 1h behind
        )


def test_missing_header_fails():
    with pytest.raises(SignatureError, match="missing"):
        verify_formspree_signature(
            raw_body=b'x',
            header_value="",
            signing_secret=SECRET,
        )


def test_malformed_header_fails():
    with pytest.raises(SignatureError, match="malformed"):
        verify_formspree_signature(
            raw_body=b'x',
            header_value="no-equals-here",
            signing_secret=SECRET,
        )


def test_header_with_only_timestamp_fails():
    with pytest.raises(SignatureError, match="malformed"):
        verify_formspree_signature(
            raw_body=b'x',
            header_value="t=123",
            signing_secret=SECRET,
        )


def test_missing_secret_fails_loudly():
    with pytest.raises(SignatureError, match="signing secret"):
        verify_formspree_signature(
            raw_body=b'x',
            header_value="t=1,v1=abc",
            signing_secret="",
        )


def test_unknown_version_ignored_if_v1_present():
    """If Formspree starts emitting v2=... alongside v1=..., we should still
    accept the request using v1."""
    body = b'hello'
    ts = 1_776_971_121
    v1 = hmac.new(SECRET.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    header = f"t={ts},v2=future-algo-value,v1={v1}"
    verify_formspree_signature(
        raw_body=body,
        header_value=header,
        signing_secret=SECRET,
        now_fn=lambda: ts + 5,
    )


def test_submission_wrapper_unwrap():
    """Real-world Formspree JSON body shape: fields nested under 'submission'."""
    from app.intake import parse_formspree_payload

    body = {
        "form": "myklwjnp",
        "submission": {
            "_date": "2026-04-20T20:22:43.449977+00:00",
            "sport": "lacrosse",
            "name": "John Binstock",
            "email": "binstockj@example.com",
            "school": "conestoga youth lacrosse",
            "rosterSize": "15",
        },
    }
    intake = parse_formspree_payload(body)
    assert intake["coach_name"] == "John Binstock"
    assert intake["coach_email"] == "binstockj@example.com"
    # Lacrosse-specific fields should land in extras since they're not aliased.
    assert intake["extras"].get("school") == "conestoga youth lacrosse"
    assert intake["extras"].get("sport") == "lacrosse"
