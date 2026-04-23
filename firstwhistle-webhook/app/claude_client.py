"""Wrapper around the Anthropic SDK for the FirstWhistle pipeline."""
from __future__ import annotations

import logging
from typing import Mapping

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


def generate_plan(intake: Mapping[str, object]) -> str:
    """Call Claude with the master system prompt + intake JSON, return raw assistant text.

    The caller is responsible for parsing the two HTML documents out of the
    returned string (see app.parser).
    """
    settings = get_settings()
    system_prompt = load_system_prompt()
    intake_json = intake_to_prompt_json(intake)

    user_msg = (
        "A new coach intake has been submitted. Produce the two HTML deliverables "
        "described in the system prompt (Full Practice Plan and One-Page Deck Sheet). "
        "Return each document either inside a ```html fenced code block or delimited "
        "with the exact comment markers specified in the system prompt.\n\n"
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
