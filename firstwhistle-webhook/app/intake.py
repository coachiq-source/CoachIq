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
    # `coachName` / `coachEmail` are the camelCase keys the CoachPrep
    # postgame form emits (see `waterpolo_postgame.html`). Aliasing them
    # here means both the strict weekly parser and the lenient postgame
    # parser pick up the coach identity without each caller having to
    # remember the exact casing Formspree happens to send.
    "coach_name":    ("coach_name", "coachName", "name", "full_name", "coach"),
    "coach_email":   ("coach_email", "coachEmail", "email", "e-mail"),
    # CoachPrep returning-coach code: short opaque string the coach picked on
    # their first intake. Used to auto-fill later forms. Accepts a handful of
    # aliases so Formspree form authors don't have to stick to one field name.
    "coach_code":    ("coach_code", "coachCode", "coachprep_code", "cp_code", "code"),
    "team_name":     ("team_name", "team", "club", "program"),
    "level":         ("level", "age_group", "age_level", "division"),
    "athlete_count": ("athlete_count", "athletes", "roster_size", "num_athletes"),
    "session_count": ("session_count", "sessions_per_week", "weekly_sessions"),
    "session_length":("session_length", "session_duration", "minutes_per_session"),
    "pool_setup":    ("pool_setup", "pool", "facility", "pool_size"),
    "focus_areas":   ("focus_areas", "focus", "priorities", "goals"),
    "constraints":   ("constraints", "notes", "injuries", "limitations"),
    "week_of":       ("week_of", "week_label", "start_date", "date"),
    "extra":         ("extra", "additional", "anything_else", "message"),
    # Form-type selector. Set by the intake form itself to tell the webhook
    # which pipeline to run. Currently: "" or "week" → regular weekly-plan
    # pipeline; "gameprep" → the game-prep single-document pipeline;
    # "postgame" → the week-in-review / post-game capture pipeline (stores
    # the payload, no Claude run yet). Missing or unknown values default to
    # the weekly pipeline so existing intakes keep working. NOTE: we
    # intentionally do NOT alias the bare key "form" here because Formspree
    # posts `{"form": "myklwjnp", "submission": {...}}` where `form` is the
    # form id, not a form-type selector.
    "form_type":     ("form_type", "formType", "formtype"),
}

# Accepted coach-code shape: 4–12 chars, letters/digits/._- only. Whitespace
# is stripped before validation; case is preserved (coaches can pick MixedCase).
CODE_RE = re.compile(r"^[A-Za-z0-9._-]{4,12}$")

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

    # Formspree's actual webhook integration wraps the fields under a top-level
    # "submission" key (with "form" being the form id string). Older / alternate
    # forwarders use "form" / "data" / "fields" as the wrapper key, so we
    # accept any of them and flatten.
    for wrap_key in ("submission", "form", "data", "fields"):
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

    # coach_code is optional. If present, validate shape; if malformed, drop
    # it silently and log — a bad code shouldn't 422 the whole intake because
    # the coach misclicked somewhere. Whitespace is stripped.
    raw_code = canonical.get("coach_code") or ""
    if raw_code:
        stripped = raw_code.strip()
        if CODE_RE.match(stripped):
            canonical["coach_code"] = stripped
        else:
            canonical["coach_code"] = ""

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


def peek_form_type(raw: Mapping[str, Any]) -> str:
    """Extract the form_type selector from a Formspree body without validating.

    Called BEFORE strict intake validation so the webhook can route traffic
    to the right handler based on which form the coach submitted. Mirrors
    `parse_formspree_payload`'s wrapper-flattening and alias handling, but
    never raises — an empty string is returned if no form_type can be found
    (which the caller treats as "regular weekly pipeline").

    This exists because post-game submissions don't have a `coach_name` —
    they use `coachName` and we don't want to 422 them just because the
    weekly-plan parser is strict about which field names are required.
    """
    if not isinstance(raw, Mapping):
        return ""
    cleaned = _strip_formspree_meta(raw)
    for wrap_key in ("submission", "form", "data", "fields"):
        if wrap_key in cleaned and isinstance(cleaned[wrap_key], Mapping):
            nested = _strip_formspree_meta(cleaned.pop(wrap_key))
            cleaned = {**nested, **cleaned}
    return _first_nonempty(cleaned, FIELD_ALIASES["form_type"]).strip().lower()


def parse_postgame_payload(raw: Mapping[str, Any]) -> Dict[str, Any]:
    """Lenient parser for post-game (Week-in-Review) submissions.

    The post-game form is intentionally more forgiving than the weekly-plan
    intake: the coach may be submitting with a CoachPrep code only (no name
    typed again), may have skipped the entire game-stats block if there was
    no game this week, and we don't want to reject the submission just
    because a weekly-plan-only field (pool setup, focus areas, etc.) is
    missing.

    Contract:
      * Never raises IntakeValidationError. A missing name or email is
        represented as an empty string — the caller decides whether that's
        fatal for its use case (the postgame handler currently accepts it
        either way and stores the raw payload for triage).
      * Always returns an `intake_id` and a `slug`. If name is empty the
        slug falls back to `coach-<intake_id>` so downstream filenames are
        still unique.
      * Stamps `form_type = "postgame"` on the returned dict so logs and
        downstream consumers don't have to re-derive it.
      * Keeps every non-empty submitted field in `extras` (plus the full
        original body under `raw`) so the future post-game pipeline has
        access to everything the form sent, not just the canonical subset.
    """
    cleaned = _strip_formspree_meta(raw) if isinstance(raw, Mapping) else {}

    for wrap_key in ("submission", "form", "data", "fields"):
        if wrap_key in cleaned and isinstance(cleaned[wrap_key], Mapping):
            nested = _strip_formspree_meta(cleaned.pop(wrap_key))
            cleaned = {**nested, **cleaned}

    canonical: Dict[str, Any] = {}
    for canonical_key, aliases in FIELD_ALIASES.items():
        canonical[canonical_key] = _first_nonempty(cleaned, aliases)

    # _replyto fallback for email (same as the strict parser).
    if not canonical["coach_email"]:
        reply_to = str(cleaned.get("_replyto", "")).strip()
        if reply_to:
            canonical["coach_email"] = reply_to

    # coach_code: if present but malformed, silently drop (consistent with
    # the strict parser's behavior — a bad code shouldn't break triage).
    raw_code = canonical.get("coach_code") or ""
    if raw_code:
        stripped = raw_code.strip()
        canonical["coach_code"] = stripped if CODE_RE.match(stripped) else ""

    # Stamp form_type even if the coach's form forgot to set it — we already
    # know we're here because the caller routed on formType == "postgame".
    canonical["form_type"] = "postgame"

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
    slug = slugify(canonical["coach_name"], fallback=f"coach-{intake_id}")

    return {
        "intake_id": intake_id,
        "slug": slug,
        **canonical,
        "extras": extras,
        "raw": dict(cleaned),
    }


def intake_to_prompt_json(intake: Mapping[str, Any]) -> str:
    """Serialize the intake in the exact shape the master system prompt expects."""
    payload = {k: v for k, v in intake.items() if k != "raw"}
    return json.dumps(payload, indent=2, ensure_ascii=False)
