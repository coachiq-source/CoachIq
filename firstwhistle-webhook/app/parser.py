"""Extract the two HTML documents from Claude's response.

Supports three delimiter conventions, in this priority order:

  1. Explicit HTML comment markers (most reliable if the system prompt enforces them):
       <!-- ===== FULL PLAN START ===== -->
       ...
       <!-- ===== FULL PLAN END ===== -->
       <!-- ===== DECK SHEET START ===== -->
       ...
       <!-- ===== DECK SHEET END ===== -->

  2. Labelled fenced code blocks, where a line like `### Full Plan` or
     `**Deck Sheet**` precedes a ```html ... ``` block.

  3. Positional fenced code blocks: first ```html block = full plan,
     second ```html block = deck sheet.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


class PlanParseError(ValueError):
    """Raised when we can't identify both HTML documents in the response."""


@dataclass(frozen=True)
class ParsedPlans:
    full_plan_html: str
    deck_sheet_html: str


_MARKER_RE_FULL = re.compile(
    r"<!--\s*=+\s*FULL\s*PLAN\s*START\s*=+\s*-->(.*?)<!--\s*=+\s*FULL\s*PLAN\s*END\s*=+\s*-->",
    re.IGNORECASE | re.DOTALL,
)
_MARKER_RE_DECK = re.compile(
    r"<!--\s*=+\s*DECK\s*SHEET\s*START\s*=+\s*-->(.*?)<!--\s*=+\s*DECK\s*SHEET\s*END\s*=+\s*-->",
    re.IGNORECASE | re.DOTALL,
)

# ```html ... ``` or ``` ... ``` (optionally with a language tag)
_FENCE_RE = re.compile(
    r"```(?P<lang>[a-zA-Z0-9_-]*)[ \t]*\r?\n(?P<body>.*?)```",
    re.DOTALL,
)

# Heading/label just before a fence that tells us which document it is.
_LABEL_FULL = re.compile(r"(full\s*practice\s*plan|full\s*plan)", re.IGNORECASE)
_LABEL_DECK = re.compile(r"(one[-\s]?page\s*deck\s*sheet|deck\s*sheet|deck)", re.IGNORECASE)


def _looks_like_html(s: str) -> bool:
    s = s.lstrip().lower()
    return s.startswith("<!doctype") or s.startswith("<html") or s.startswith("<!--") or s.startswith("<section") or s.startswith("<div")


def _extract_by_markers(text: str) -> ParsedPlans | None:
    full = _MARKER_RE_FULL.search(text)
    deck = _MARKER_RE_DECK.search(text)
    if full and deck:
        return ParsedPlans(
            full_plan_html=full.group(1).strip(),
            deck_sheet_html=deck.group(1).strip(),
        )
    return None


def _extract_by_fences(text: str) -> ParsedPlans | None:
    fences = [
        (m.start(), m.group("lang").lower(), m.group("body").strip())
        for m in _FENCE_RE.finditer(text)
    ]
    # Only consider fences that hold HTML-looking content.
    html_fences = [
        (start, lang, body)
        for (start, lang, body) in fences
        if lang in ("html", "htm", "") and _looks_like_html(body)
    ]
    if len(html_fences) < 2:
        return None

    # Pass 1: label-based. Look at the ~200 chars immediately preceding each fence.
    labelled: dict[str, str] = {}
    for start, _lang, body in html_fences:
        context = text[max(0, start - 200):start]
        # Prefer whichever label is *closest* to the fence.
        full_match = list(_LABEL_FULL.finditer(context))
        deck_match = list(_LABEL_DECK.finditer(context))
        full_pos = full_match[-1].end() if full_match else -1
        deck_pos = deck_match[-1].end() if deck_match else -1
        if deck_pos > full_pos and "deck" not in labelled:
            labelled["deck"] = body
        elif full_pos > deck_pos and "full" not in labelled:
            labelled["full"] = body
    if "full" in labelled and "deck" in labelled:
        return ParsedPlans(
            full_plan_html=labelled["full"],
            deck_sheet_html=labelled["deck"],
        )

    # Pass 2: positional fallback — first html block is full plan, second is deck.
    first_body = html_fences[0][2]
    second_body = html_fences[1][2]
    # Heuristic: deck sheets are typically shorter than full plans. Swap if the
    # first block looks meaningfully shorter than the second.
    if len(first_body) < len(second_body) * 0.6:
        first_body, second_body = second_body, first_body
    return ParsedPlans(full_plan_html=first_body, deck_sheet_html=second_body)


def parse_plans(response_text: str) -> ParsedPlans:
    """Pull both HTML documents out of a Claude response. Raise PlanParseError on failure."""
    if not response_text or not response_text.strip():
        raise PlanParseError("empty response")

    parsed = _extract_by_markers(response_text) or _extract_by_fences(response_text)
    if parsed is None:
        raise PlanParseError(
            "could not locate both HTML documents in response "
            "(expected comment markers or two ```html fenced blocks)"
        )

    # Final sanity check: each document should look like HTML.
    if not _looks_like_html(parsed.full_plan_html):
        raise PlanParseError("full plan does not look like HTML")
    if not _looks_like_html(parsed.deck_sheet_html):
        raise PlanParseError("deck sheet does not look like HTML")

    return parsed
