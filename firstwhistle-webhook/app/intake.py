"""Normalize a Formspree POST body into a clean intake dict."""
from __future__ import annotations

import json
import re
import uuid
from typing import Any, Dict, Mapping

from .config import slugify

# Formspree submits either form-url-encoded or JSON depending on how the
# form is configured. Either way we get a flat-ish dict of string values.
# Field names below are the "preferred" canonical keys; the parser will
# accept several aliases for each so the Formspree form's field names don't
# have to match exactly.

FIELD_ALIASES: Dict[str, tuple[str, ...]] = {
    "coach_name":    ("coach_name", "name", "full_name", "coach"),
    "coach_email":   ("coach_email", "email", "e-mail"),
    "team_name":     ("team_name", "team", "club", "program"),
    "level":         ("level", "age_group", "age_level", "division"),
    "athlete_count": ("athlete_count", "athletes", "roster_size", "num_athletes"),
    "session_count": ("session_count", "sessions_per_week", "weekly_sessions"),
    "session_length":("session_length", "session_duration", "minutes_per_session"),
    "pool_setup":    ("pool_setup", "pool", "facility", "pool_size"),
    "focus_areas":   ("focus_areas", "focus", "priorities", "goals"),
    "constraints":   ("constraints", "notes", "injuries", "limitations"),
    "week_of":       ("week_of", "week", "start_date", "date"),
    "extra":         ("extra", "additional", "anything_else", "message"),
}

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class IntakeValidationError(ValueError):
    """Raised when required fields are missing or malformed."""


def _first_nonempty(data: Mapping[str, Any], keys: tuple[str, ...]) -> str:
    for k in keys:
        v = data.get(k)
        if v is None:
            continue
        if isinstance(v, list):
            v = ", ".join(str(x).strip() for x in v if str(x).strip())
        s = str(v).strip()
        if s:
            return s
    return ""


def _strip_formspree_meta(raw: Mapping[str, Any]) -> Dict[str, Any]:
    """Drop Formspree-internal fields (_captcha, _gotcha, _replyto handled separately)."""
    drop_prefixes = ("_",)
    drop_keys = {"g-recaptcha-response", "h-captcha-response"}
    out: Dict[str, Any] = {}
    for k, v in raw.items():
        if k in drop_keys:
            continue
        if any(k.startswith(p) for p in drop_prefixes) and k != "_replyto":
            continue
        out[k] = v
    return out


def parse_formspree_payload(raw: Mapping[str, Any]) -> Dict[str, Any]:
    """Turn a Formspree POST dict into a canonical intake dict.

    Returns a dict with at minimum `coach_name`, `coach_email`, `slug`, `intake_id`,
    and any additional fields the form supplied.
    """
    cleaned = _strip_formspree_meta(raw)

    # Formspree sometimes wraps the payload in a top-level "form" or "data" key
    # when using webhook forwarders. Flatten that if present.
    for wrap_key in ("form", "data", "fields"):
        if wrap_key in cleaned and isinstance(cleaned[wrap_key], Mapping):
            nested = _strip_formspree_meta(cleaned.pop(wrap_key))
            cleaned = {**nested, **cleaned}

    canonical: Dict[str, Any] = {}
    for canonical_key, aliases in FIELD_ALIASES.items():
        canonical[canonical_key] = _first_nonempty(cleaned, aliases)

    # Fall back to Formspree's _replyto for email if we didn't find one.
    if not canonical["coach_email"]:
        reply_to = str(cleaned.get("_replyto", "")).strip()
        if reply_to:
            canonical["coach_email"] = reply_to

    # Validation
    if not canonical["coach_name"]:
        raise IntakeValidationError("coach_name is required")
    if not canonical["coach_email"]:
        raise IntakeValidationError("coach_email is required")
    if not EMAIL_RE.match(canonical["coach_email"]):
        raise IntakeValidationError(
            f"coach_email is not a valid email: {canonical['coach_email']}"
        )

    # Keep any unknown-but-non-empty fields alongside canonical ones so the
    # model has access to everything the coach wrote.
    known_aliases = {a for aliases in FIELD_ALIASES.values() for a in aliases}
    extras: Dict[str, Any] = {}
    for k, v in cleaned.items():
        if k in known_aliases or k == "_replyto":
            continue
        if isinstance(v, (dict, list)):
            extras[k] = v
        elif str(v).strip():
            extras[k] = str(v).strip()

    intake_id = uuid.uuid4().hex[:12]
    # Slug is derived from the coach's name only (spec: "Magnus Sims" -> "magnus-sims").
    # Team name is kept in the intake payload for the model but not in the URL.
    slug = slugify(canonical["coach_name"], fallback=f"coach-{intake_id}")

    return {
        "intake_id": intake_id,
        "slug": slug,
        **canonical,
        "extras": extras,
        "raw": dict(cleaned),  # keep full original for the model
    }


def intake_to_prompt_json(intake: Mapping[str, Any]) -> str:
    """Serialize the intake in the exact shape the master system prompt expects."""
    payload = {k: v for k, v in intake.items() if k != "raw"}
    return json.dumps(payload, indent=2, ensure_ascii=False)
