"""Wrapper around the Anthropic SDK for the FirstWhistle pipeline."""
from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

from anthropic import Anthropic, APIError
# `DefaultHttpxClient` is used so we can set a longer timeout for long plan generations.
import httpx

from .config import get_settings, load_system_prompt
from .intake import intake_to_prompt_json

log = logging.getLogger("firstwhistle.claude")

# Plan generation can run long — give the HTTP client plenty of headroom.
HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=30.0, pool=10.0)


class ClaudeGenerationError(RuntimeError):
    """Raised when the model fails to return usable content."""


def _client() -> Anthropic:
    settings = get_settings()
    return Anthropic(
        api_key=settings.anthropic_api_key,
        timeout=HTTP_TIMEOUT,
        max_retries=2,
    )


def _extract_text(msg) -> str:
    """Concatenate all text blocks in a Messages API response."""
    parts: list[str] = []
    for block in msg.content or []:
        btype = getattr(block, "type", None)
        if btype == "text":
            parts.append(getattr(block, "text", "") or "")
    return "\n".join(p for p in parts if p)


def _fmt(value: Any) -> str:
    """Render a post-game field for inclusion in the prompt block.

    Lists become comma-joined strings; everything else is stringified and
    stripped. Empty / missing values collapse to the literal string "—"
    so the prompt block is visually consistent instead of dropping lines.
    """
    if value is None:
        return "—"
    if isinstance(value, list):
        parts = [str(x).strip() for x in value if str(x).strip()]
        return ", ".join(parts) if parts else "—"
    s = str(value).strip()
    return s if s else "—"


def _had_game(postgame: Mapping[str, Any]) -> bool:
    """Did the coach have a game this week? Returns True for yes, False for
    no / unknown. Accepts a few common truthy strings the post-game form
    could send ('Yes', 'yes', 'Y', 'true', '1')."""
    raw = postgame.get("hadGame")
    if raw is None:
        return False
    return str(raw).strip().lower() in {"yes", "y", "true", "1"}


def _first_nonempty_fmt(postgame: Mapping[str, Any], *keys: str) -> str:
    """Return the first non-empty value among `keys`, formatted via `_fmt`.

    Handy when a field is emitted under different camelCase names across
    forms — e.g. lacrosse_postgame.html sends `bestMoments` (plural,
    multi-select) while waterpolo_postgame.html sends `bestMoment`
    (singular). The block stays consistent regardless of which form fed
    the record.
    """
    for k in keys:
        v = postgame.get(k)
        if v is None:
            continue
        if isinstance(v, list):
            if any(str(x).strip() for x in v):
                return _fmt(v)
            continue
        if str(v).strip():
            return _fmt(v)
    return "—"


def _build_waterpolo_postgame_block(
    postgame: Mapping[str, Any],
    week_number: int,
) -> str:
    """Water-polo-specific Week N-1 retrospective block.

    Uses water-polo terminology (6x5 / 5x6 / ejections / steals) and the
    field names emitted by `waterpolo_postgame.html`.
    """
    prev = week_number - 1
    had_game = _had_game(postgame)

    lines: list[str] = [
        f"WEEK {prev} REVIEW — use this to inform the focal theme, KPIs, "
        f"and decision points for Week {week_number}:",
        f"- Had a game this week: {'Yes' if had_game else 'No'}",
    ]

    if had_game:
        goals_for = _fmt(postgame.get("goalsFor"))
        goals_against = _fmt(postgame.get("goalsAgainst"))
        if goals_for == "—" and goals_against == "—":
            score_line = "—"
        else:
            score_line = f"{goals_for}\u2013{goals_against}"  # en-dash
        lines.extend([
            f"- Opponent: {_fmt(postgame.get('opponent'))}",
            f"- Result (W/L/T): {_fmt(postgame.get('result'))}",
            f"- Score (GF\u2013GA): {score_line}",
            f"- Shots (total): {_fmt(postgame.get('shotTotal'))}",
            f"- Ejections drawn for: {_fmt(postgame.get('ejectionsDrawnFor'))}",
            f"- Ejections against (drawn by opponent): "
            f"{_fmt(postgame.get('ejectionsDrawnAgainst'))}",
            f"- Steals: {_fmt(postgame.get('steals'))}",
            f"- Turnovers: {_fmt(postgame.get('turnovers'))}",
            f"- 6x5 conversion (goals / attempts): "
            f"{_fmt(postgame.get('pp6Goals'))} / "
            f"{_fmt(postgame.get('pp6Attempts'))}",
            f"- 5x6 stops (stops / attempts): "
            f"{_fmt(postgame.get('md5Stops'))} / "
            f"{_fmt(postgame.get('md5Attempts'))}",
            f"- Result feel: {_fmt(postgame.get('resultFeel'))}",
        ])

    # These fields are injected regardless of whether a game was played —
    # for a no-game week they capture what was worked on in practice.
    lines.extend([
        f"- Best moment of the week: {_fmt(postgame.get('bestMoment'))}",
        f"- What didn't land: {_fmt(postgame.get('didntLand'))}",
        f"- Player who stood out: {_fmt(postgame.get('standoutPlayer'))}",
        f"- Confidence going into Week {week_number} (1\u20135): "
        f"{_fmt(postgame.get('confidenceNextWeek'))}",
        f"- One thing to fix: {_fmt(postgame.get('oneThingToFix'))}",
        f"- One thing to protect: {_fmt(postgame.get('oneThingToProtect'))}",
        f"- Extra notes: {_fmt(postgame.get('extraNotes'))}",
    ])
    return "\n".join(lines)


def _build_lacrosse_postgame_block(
    postgame: Mapping[str, Any],
    week_number: int,
) -> str:
    """Lacrosse-specific Week N-1 retrospective block.

    Uses lacrosse terminology (EMO / EMD / ground balls / clearing /
    face-offs for boys, draw controls for girls) and the field names
    emitted by `lacrosse_postgame.html`. Face-off vs. draw-control line
    is picked based on the `gender` the coach selected on the form
    (Boys / Girls); if neither is set we show both rather than drop
    them, on the theory that having a bit of extra stat context is
    better than silently losing it.
    """
    prev = week_number - 1
    had_game = _had_game(postgame)

    lines: list[str] = [
        f"WEEK {prev} REVIEW — use this to inform the focal theme, KPIs, "
        f"and decision points for Week {week_number}:",
        f"- Had a game this week: {'Yes' if had_game else 'No'}",
    ]

    if had_game:
        goals_for = _fmt(postgame.get("goalsFor"))
        goals_against = _fmt(postgame.get("goalsAgainst"))
        if goals_for == "—" and goals_against == "—":
            score_line = "—"
        else:
            score_line = f"{goals_for}\u2013{goals_against}"  # en-dash
        lines.extend([
            f"- Opponent: {_fmt(postgame.get('opponent'))}",
            f"- Result (W/L/T): {_fmt(postgame.get('result'))}",
            f"- Score (GF\u2013GA): {score_line}",
            # Water-polo emits `shotTotal`; lacrosse emits `shots`. Read
            # both so any store-layer drift doesn't silently lose shots.
            f"- Shots (total): "
            f"{_first_nonempty_fmt(postgame, 'shots', 'shotTotal')}",
            f"- Ground balls won: {_fmt(postgame.get('groundBallsWon'))}",
            f"- Turnovers: {_fmt(postgame.get('turnovers'))}",
            f"- Clearing (successful / attempted): "
            f"{_fmt(postgame.get('clearsSuccessful'))} / "
            f"{_fmt(postgame.get('clearsAttempted'))}",
            f"- EMO (man-up) conversion (goals / attempts): "
            f"{_fmt(postgame.get('emoGoals'))} / "
            f"{_fmt(postgame.get('emoAttempts'))}",
            f"- EMD (man-down) stops (stops / attempts): "
            f"{_fmt(postgame.get('emdStops'))} / "
            f"{_fmt(postgame.get('emdAttempts'))}",
        ])

        gender = str(postgame.get("gender") or "").strip().lower()
        if gender == "boys":
            lines.append(
                f"- Face-offs (won / lost): "
                f"{_fmt(postgame.get('faceoffsWon'))} / "
                f"{_fmt(postgame.get('faceoffsLost'))}"
            )
        elif gender == "girls":
            lines.append(
                f"- Draw controls (won / lost): "
                f"{_fmt(postgame.get('drawsWon'))} / "
                f"{_fmt(postgame.get('drawsLost'))}"
            )
        else:
            # Unknown / unset gender: include both lines so Claude still
            # sees whichever numbers the coach actually filled in.
            lines.extend([
                f"- Face-offs (won / lost): "
                f"{_fmt(postgame.get('faceoffsWon'))} / "
                f"{_fmt(postgame.get('faceoffsLost'))}",
                f"- Draw controls (won / lost): "
                f"{_fmt(postgame.get('drawsWon'))} / "
                f"{_fmt(postgame.get('drawsLost'))}",
            ])

        lines.append(f"- Result feel: {_fmt(postgame.get('resultFeel'))}")

    # Practice-focused fields injected regardless of whether a game was
    # played. Read both singular and plural variants of each key so a
    # record authored before the lacrosse form existed (singular) and
    # one authored after (plural, multi-select) both render cleanly.
    lines.extend([
        f"- Best moment of the week: "
        f"{_first_nonempty_fmt(postgame, 'bestMoments', 'bestMoment')}",
        f"- What didn't land: {_fmt(postgame.get('didntLand'))}",
        f"- Player who stood out: "
        f"{_first_nonempty_fmt(postgame, 'playerStandout', 'standoutPlayer')}",
        f"- Confidence going into Week {week_number} (1\u20135): "
        f"{_first_nonempty_fmt(postgame, 'confidenceLevel', 'confidenceNextWeek')}",
        f"- One thing to fix: {_fmt(postgame.get('oneThingToFix'))}",
        f"- One thing to protect: {_fmt(postgame.get('oneThingToProtect'))}",
        f"- Extra notes: {_fmt(postgame.get('extraNotes'))}",
    ])
    return "\n".join(lines)


def _build_postgame_context_block(
    postgame: Mapping[str, Any],
    week_number: int,
    sport: str = "waterpolo",
) -> str:
    """Format the Week N-1 retrospective as a context block for Claude.

    Dispatches by sport so the injected terminology and stat lines match
    what the coach actually submitted:

      * `waterpolo` (default) — 6x5 / 5x6 / ejections / steals.
      * `lacrosse` — EMO / EMD / ground balls / clearing / face-offs
        (boys) or draw controls (girls).

    Unknown sports fall through to the water-polo shape so we never
    fail to inject a block on a typo — the downside of a few unfamiliar
    stat lines beats dropping a whole retro on the floor.
    """
    s = (sport or "").strip().lower()
    if s == "lacrosse":
        return _build_lacrosse_postgame_block(postgame, week_number)
    return _build_waterpolo_postgame_block(postgame, week_number)


def generate_plan(
    intake: Mapping[str, object],
    postgame_context: Optional[Mapping[str, Any]] = None,
) -> str:
    """Call Claude with the master system prompt + intake JSON, return raw assistant text.

    The caller is responsible for parsing the two HTML documents out of the
    returned string (see app.parser).

    If `postgame_context` is provided (pipeline passes it for Week 2+ when a
    post-game retrospective from the prior week is on file), a "WEEK N-1
    POST-GAME REVIEW" block is prepended to the user message so Claude can
    reference it when picking the focal theme, KPIs, and decision points.
    """
    settings = get_settings()
    system_prompt = load_system_prompt()
    intake_json = intake_to_prompt_json(intake)

    postgame_block = ""
    if postgame_context:
        try:
            week_number = int(intake.get("week") or 1)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            week_number = 1
        if week_number > 1:
            sport = str(intake.get("sport") or "waterpolo").strip().lower()
            postgame_block = (
                _build_postgame_context_block(
                    postgame_context, week_number, sport=sport,
                )
                + "\n\n"
            )

    user_msg = (
        f"{postgame_block}"
        "A new coach intake has been submitted. Produce the two HTML deliverables "
        "described in the system prompt (Full Practice Plan and One-Page Deck Sheet). "
        "Return them using the exact HTML comment markers specified in Part 10 of "
        "the system prompt — no JSON wrapper, no markdown fences, no preamble, "
        "no trailing commentary.\n\n"
        "INTAKE JSON:\n"
        f"```json\n{intake_json}\n```"
    )

    log.info(
        "calling Claude model=%s intake_id=%s slug=%s",
        settings.claude_model,
        intake.get("intake_id"),
        intake.get("slug"),
    )

    try:
        msg = _client().messages.create(
            model=settings.claude_model,
            max_tokens=settings.claude_max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
        )
    except APIError as exc:
        log.exception("anthropic API error: %s", exc)
        raise ClaudeGenerationError(f"Anthropic API error: {exc}") from exc

    text = _extract_text(msg)
    if not text.strip():
        raise ClaudeGenerationError("Claude returned empty content")

    log.info(
        "claude response ok intake_id=%s chars=%d stop_reason=%s",
        intake.get("intake_id"),
        len(text),
        getattr(msg, "stop_reason", "?"),
    )
    return text


# Per-sport routing hint embedded in the `generate_gameprep` user message.
# The master system prompt defines two game-prep sections — SECTION WP-G
# (water polo, Parts G1–G10 + G-Pool) and SECTION LAX-G (lacrosse, Parts
# LG1–LG10 + LG-Field). We mention the right section explicitly so the
# model can't drift into the wrong sport's terminology.
_GAMEPREP_SECTION_HINT_BY_SPORT: dict[str, str] = {
    "waterpolo": (
        "SECTION WP-G (water polo, Parts G1 through G10 + Part G-Pool)"
    ),
    "water_polo": (
        "SECTION WP-G (water polo, Parts G1 through G10 + Part G-Pool)"
    ),
    "lacrosse": (
        "SECTION LAX-G (lacrosse, Parts LG1 through LG10 + Part LG-Field)"
    ),
}


def generate_gameprep(intake: Mapping[str, object]) -> str:
    """Call Claude for a game-prep single-document output.

    Uses the same master system prompt as `generate_plan`, but the user
    message tells the model to return a SINGLE HTML document wrapped in the
    GAME PREP START / END markers — not the two-document FULL PLAN / DECK
    SHEET format used for the weekly pipeline.

    The pipeline is responsible for stamping ``intake["sport"]`` before
    calling this function (see `_run_gameprep_core` in app.gameprep). The
    `sport` value on the intake selects which game-prep section of the
    master prompt is authoritative for the response. Water polo points at
    Section WP-G (Parts G1–G10 + G-Pool). Lacrosse points at Section
    LAX-G (Parts LG1–LG10 + LG-Field). Both sections share the same
    output contract (Part 10.3) and the same marker pair —
    `parse_gameprep` handles either.

    Intakes that arrive with no sport (replay scripts, hand-rolled tests)
    default to water polo so Session 7's original behavior is preserved.
    """
    settings = get_settings()
    system_prompt = load_system_prompt()
    intake_json = intake_to_prompt_json(intake)

    sport_raw = intake.get("sport")
    sport_key = (str(sport_raw).strip().lower()
                 if sport_raw is not None else "") or "waterpolo"
    section_hint = _GAMEPREP_SECTION_HINT_BY_SPORT.get(
        sport_key, _GAMEPREP_SECTION_HINT_BY_SPORT["waterpolo"]
    )
    sport_label = "lacrosse" if sport_key == "lacrosse" else "water polo"

    user_msg = (
        f"A coach has submitted a {sport_label} game-prep intake. Produce "
        f"the single HTML game-prep document described in {section_hint} "
        "of the system prompt. Return it using the exact HTML comment "
        "markers `<!-- ===== GAME PREP START ===== -->` and "
        "`<!-- ===== GAME PREP END ===== -->` — no JSON wrapper, no "
        "markdown fences, no preamble, no trailing commentary. Do not "
        "produce a weekly practice plan or deck sheet for this intake. "
        f"Use {sport_label} terminology throughout — follow the appropriate "
        "terminology table in the system prompt for this sport.\n\n"
        "INTAKE JSON:\n"
        f"```json\n{intake_json}\n```"
    )

    log.info(
        "calling Claude (gameprep) model=%s sport=%s intake_id=%s slug=%s opponent=%s",
        settings.claude_model,
        sport_key,
        intake.get("intake_id"),
        intake.get("slug"),
        (intake.get("extras") or {}).get("opponent") if isinstance(intake.get("extras"), Mapping) else intake.get("opponent"),
    )

    try:
        msg = _client().messages.create(
            model=settings.claude_model,
            max_tokens=settings.claude_max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
        )
    except APIError as exc:
        log.exception("anthropic API error (gameprep): %s", exc)
        raise ClaudeGenerationError(f"Anthropic API error: {exc}") from exc

    text = _extract_text(msg)
    if not text.strip():
        raise ClaudeGenerationError("Claude returned empty content (gameprep)")

    log.info(
        "claude gameprep response ok intake_id=%s chars=%d stop_reason=%s",
        intake.get("intake_id"),
        len(text),
        getattr(msg, "stop_reason", "?"),
    )
    return text
