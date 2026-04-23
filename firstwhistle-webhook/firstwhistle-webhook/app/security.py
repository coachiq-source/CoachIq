"""Formspree HMAC signature verification.

Formspree sends webhook requests with a `Formspree-Signature` header of the form

    t=<unix_timestamp>,v1=<hex_hmac_sha256>

The signature is computed as

    HMAC-SHA256(signing_secret, f"{timestamp}.{raw_body}")

and emitted as lowercase hex. We verify the signature and also enforce a
timestamp tolerance to defeat replay attacks.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Tuple


class SignatureError(ValueError):
    """Raised when a Formspree signature cannot be verified."""


def _parse_signature_header(header_value: str) -> Tuple[str, str]:
    """Parse `t=<ts>,v1=<hex>` into (timestamp_str, signature_hex).

    Ignores unknown prefixes so future Formspree additions (v2, etc.) don't break us.
    """
    if not header_value:
        raise SignatureError("missing formspree-signature header")
    timestamp = ""
    signature = ""
    for part in header_value.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, _, value = part.partition("=")
        key = key.strip().lower()
        value = value.strip()
        if key == "t":
            timestamp = value
        elif key == "v1":
            signature = value
    if not timestamp or not signature:
        raise SignatureError(
            "malformed formspree-signature header: missing t= or v1="
        )
    return timestamp, signature


def verify_formspree_signature(
    raw_body: bytes,
    header_value: str,
    signing_secret: str,
    tolerance_seconds: int = 300,
    now_fn=time.time,
) -> None:
    """Verify a Formspree HMAC signature. Raises SignatureError on failure.

    Args:
        raw_body: The raw HTTP request body bytes, *exactly* as received.
        header_value: The value of the `Formspree-Signature` header.
        signing_secret: The shared signing secret Formspree generated when
            the webhook was connected.
        tolerance_seconds: Reject signatures whose timestamp is older or
            newer than now by more than this many seconds. Defaults to 5
            minutes.
        now_fn: Overridable time source for tests.
    """
    if not signing_secret:
        raise SignatureError("signing secret is not configured")

    timestamp_str, signature_hex = _parse_signature_header(header_value)

    # Replay-protection window.
    try:
        timestamp = int(timestamp_str)
    except ValueError as exc:
        raise SignatureError(f"invalid timestamp in signature header: {timestamp_str!r}") from exc

    delta = int(now_fn()) - timestamp
    if abs(delta) > tolerance_seconds:
        raise SignatureError(
            f"signature timestamp outside tolerance window: delta={delta}s"
        )

    # Signed payload is the literal ASCII timestamp, then a ".", then the raw body bytes.
    signed_payload = timestamp_str.encode("ascii") + b"." + raw_body
    expected = hmac.new(
        signing_secret.encode("utf-8"),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, signature_hex.lower()):
        raise SignatureError("signature mismatch")
