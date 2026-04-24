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


def _build_postgame_context_block(
    postgame: Mapping[str, Any],
    week_number: int,
) -> str:
    """Format the Week N-1 retrospective as a context block for Claude.

    Pulls every field waterpolo_postgame.html emits (camelCase). If the
    coach didn't have a game, the game-stats block is omitted but the rest
    of the review (what worked, what didn't, confidence, one thing to fix /
    protect, extra notes) is still injected so Claude knows what was
    worked on and what landed in practice.

    Header names the prior week (N-1) and the current week (N) so Claude
    can anchor "this should inform the focal theme, KPIs, and decision
    points for the current plan."
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
            postgame_block = (
                _build_postgame_context_block(postgame_context, week_number)
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


def generate_gameprep(intake: Mapping[str, object]) -> str:
    """Call Claude for the water-polo game-prep single-document output (Part G).

    Uses the same master system prompt as `generate_plan`, but the user
    message tells the model to return a SINGLE HTML document wrapped in the
    GAME PREP START / END markers specified in Part G — not the two-document
    FULL PLAN / DECK SHEET format used for the weekly pipeline.
    """
    settings = get_settings()
    system_prompt = load_system_prompt()
    intake_json = intake_to_prompt_json(intake)

    user_msg = (
        "A coach has submitted a game-prep intake. Produce the single HTML "
        "game-prep document described in Part G of the system prompt (water "
        "polo only). Return it using the exact HTML comment markers "
        "`<!-- ===== GAME PREP START ===== -->` and "
        "`<!-- ===== GAME PREP END ===== -->` — no JSON wrapper, no markdown "
        "fences, no preamble, no trailing commentary. Do not produce a weekly "
        "practice plan or deck sheet for this intake.\n\n"
        "INTAKE JSON:\n"
        f"```json\n{intake_json}\n```"
    )

    log.info(
        "calling Claude (gameprep) model=%s intake_id=%s slug=%s opponent=%s",
        settings.claude_model,
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
