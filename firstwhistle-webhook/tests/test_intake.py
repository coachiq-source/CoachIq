"""Unit tests for intake + parser modules that don't need external services."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.intake import parse_formspree_payload, IntakeValidationError  # noqa: E402
from app.parser import parse_plans, PlanParseError  # noqa: E402


def test_intake_basic_json():
    body = {
        "name": "Jamie Rivera",
        "email": "jamie@example.com",
        "team": "Riverside 14U Girls",
        "age_group": "14U",
        "athletes": "16",
        "focus": "counter-attack speed, shot selection",
    }
    intake = parse_formspree_payload(body)
    assert intake["coach_name"] == "Jamie Rivera"
    assert intake["coach_email"] == "jamie@example.com"
    assert intake["team_name"] == "Riverside 14U Girls"
    assert intake["level"] == "14U"
    assert intake["athlete_count"] == "16"
    assert intake["slug"].startswith("jamie-rivera")
    assert len(intake["intake_id"]) == 12


def test_intake_missing_email_raises():
    try:
        parse_formspree_payload({"name": "x"})
    except IntakeValidationError as exc:
        assert "email" in str(exc).lower()
    else:
        raise AssertionError("should have raised")


def test_intake_uses_replyto_fallback():
    body = {"name": "Jamie", "_replyto": "jamie@example.com"}
    intake = parse_formspree_payload(body)
    assert intake["coach_email"] == "jamie@example.com"


def test_intake_invalid_email_rejected():
    try:
        parse_formspree_payload({"name": "x", "email": "not-an-email"})
    except IntakeValidationError:
        pass
    else:
        raise AssertionError("should have raised")


def test_intake_drops_formspree_meta():
    body = {
        "name": "x",
        "email": "x@y.com",
        "_subject": "New submission",
        "g-recaptcha-response": "token",
    }
    intake = parse_formspree_payload(body)
    assert "_subject" not in intake["extras"]
    assert "g-recaptcha-response" not in intake["extras"]


def test_parse_plans_with_markers():
    response = """Here are your plans.

<!-- ===== FULL PLAN START ===== -->
<!doctype html><html><body><h1>Full</h1></body></html>
<!-- ===== FULL PLAN END ===== -->

<!-- ===== DECK SHEET START ===== -->
<!doctype html><html><body><h1>Deck</h1></body></html>
<!-- ===== DECK SHEET END ===== -->
"""
    parsed = parse_plans(response)
    assert "<h1>Full</h1>" in parsed.full_plan_html
    assert "<h1>Deck</h1>" in parsed.deck_sheet_html


def test_parse_plans_with_labelled_fences():
    response = """### Full Practice Plan

```html
<!doctype html><html><body><h1>Full</h1></body></html>
```

### One-Page Deck Sheet

```html
<!doctype html><html><body><h1>Deck</h1></body></html>
```
"""
    parsed = parse_plans(response)
    assert "Full" in parsed.full_plan_html
    assert "Deck" in parsed.deck_sheet_html


def test_parse_plans_positional_fallback():
    response = """```html
<!doctype html><html><body><h1>A longer full plan with more content here for heuristic</h1><p>lots of stuff</p></body></html>
```

```html
<!doctype html><html><body><h1>Deck</h1></body></html>
```
"""
    parsed = parse_plans(response)
    assert "longer full plan" in parsed.full_plan_html
    assert "Deck" in parsed.deck_sheet_html


def test_parse_plans_empty_raises():
    try:
        parse_plans("")
    except PlanParseError:
        pass
    else:
        raise AssertionError("should have raised")


def test_parse_plans_one_block_raises():
    response = "```html\n<!doctype html><html></html>\n```"
    try:
        parse_plans(response)
    except PlanParseError:
        pass
    else:
        raise AssertionError("should have raised")


if __name__ == "__main__":
    # Crude runner so `python tests/test_intake.py` works without pytest.
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as exc:
            failed += 1
            print(f"FAIL {t.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(failed)
