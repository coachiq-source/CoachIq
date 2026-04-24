"""Regression: the pipeline must resolve the week number BEFORE Claude runs
and must stamp it onto the intake dict Claude sees (master prompt Part 10.1
reads `intake.week`). It must also forward the same number to deploy_plans so
the HTML filename and the embedded week title line up.

We stub every external call (Claude, GitHub listing, GitHub PUT, coach email)
so the test runs offline and deterministically.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic")
os.environ.setdefault("GITHUB_TOKEN", "test-token")
os.environ.setdefault("GITHUB_REPO", "coachiq-source/CoachIq")
os.environ.setdefault("RESEND_API_KEY", "test-resend")
os.environ.setdefault("COACH_EMAIL_FROM", "coach@example.com")

# Import after env stubs.
from app import pipeline  # noqa: E402
from app.github_deploy import DeployResult  # noqa: E402


def _stub_claude_response(week: int, sport: str = "waterpolo") -> str:
    """Build a minimal well-formed Claude response for a given week."""
    sheet = {
        "waterpolo": "Deck Sheet",
        "lacrosse": "Field Sheet",
        "basketball": "Court Sheet",
    }.get(sport, "Deck Sheet")
    return (
        "<!-- ===== FULL PLAN START ===== -->\n"
        f"<!DOCTYPE html><html><head><title>CoachPrep — Week {week} Practice Plan</title>"
        f"</head><body><h1>Week {week} Practice Plan</h1></body></html>\n"
        "<!-- ===== FULL PLAN END ===== -->\n\n"
        "<!-- ===== DECK SHEET START ===== -->\n"
        f"<!DOCTYPE html><html><head><title>CoachPrep — Week {week} {sheet}</title>"
        f"</head><body><h1>Week {week} {sheet}</h1></body></html>\n"
        "<!-- ===== DECK SHEET END ===== -->"
    )


def test_week_is_resolved_before_claude_and_stamped_on_intake():
    """generate_plan must receive an intake with `week` already set.

    We assert on the dict generate_plan sees, not on what the caller passed —
    that's the contract the master prompt relies on.
    """
    seen_intake: dict = {}

    def fake_generate_plan(intake, postgame_context=None):
        # Session 10: claude_client.generate_plan now takes an optional
        # postgame_context kwarg. This test doesn't care about the value —
        # it's asserting the week-resolution contract — so just accept it.
        seen_intake.update(dict(intake))
        return _stub_claude_response(week=int(intake["week"]))

    fake_deploy = DeployResult(
        plan_url="https://example.invalid/plan.html",
        deck_url="https://example.invalid/deck.html",
        commit_sha="abc1234",
        plan_path="coaches/test-coach/week3-plan.html",
        deck_path="coaches/test-coach/week3-deck.html",
        week_number=3,
    )

    with patch.object(pipeline, "discover_next_week_number", return_value=3), \
         patch.object(pipeline, "generate_plan", side_effect=fake_generate_plan), \
         patch.object(pipeline, "deploy_plans", return_value=fake_deploy) as mock_deploy, \
         patch.object(pipeline, "send_coach_email") as mock_email:
        # The email mock's return_value.message_id is accessed by pipeline.py.
        mock_email.return_value.message_id = "msg-abc"

        result = pipeline.run_pipeline({
            "intake_id": "abc123",
            "slug": "test-coach",
            "coach_name": "Test Coach",
            "coach_email": "test@example.com",
            "sport": "waterpolo",
        })

    assert result["ok"] is True
    assert seen_intake.get("week") == 3, (
        "master prompt Part 10.1 depends on intake.week being set pre-Claude"
    )
    # deploy_plans must be called with the SAME week number the intake saw.
    assert mock_deploy.call_args.kwargs["week_number"] == 3


def test_week_discovery_failure_defaults_to_1():
    """A transient GitHub issue must not block the pipeline — fall back to 1."""
    from app import github_deploy

    def explode(*_a, **_kw):
        raise RuntimeError("github unreachable")

    # discover_next_week_number internally catches this and returns 1.
    with patch.object(github_deploy, "httpx") as mock_httpx:
        mock_httpx.Client.side_effect = explode
        assert github_deploy.discover_next_week_number("some-slug") == 1


def test_coach_code_triggers_upsert_in_pipeline():
    """If the intake carries a `coach_code`, the pipeline should write it to
    the returning-coach store as part of pre-flight."""
    import tempfile
    from app.coach_store import (
        get_coach_profile,
        reset_coach_store_for_tests,
    )

    with tempfile.TemporaryDirectory() as td:
        reset_coach_store_for_tests(Path(td) / "coach_store.sqlite3")
        try:
            with patch.object(pipeline, "discover_next_week_number", return_value=1), \
                 patch.object(pipeline, "generate_plan",
                              return_value=_stub_claude_response(week=1)), \
                 patch.object(pipeline, "deploy_plans",
                              return_value=DeployResult(
                                  "u", "u", "sha", "a", "b", 1)), \
                 patch.object(pipeline, "send_coach_email") as mock_email:
                mock_email.return_value.message_id = "msg-x"

                result = pipeline.run_pipeline({
                    "intake_id": "i1",
                    "slug": "returning-coach",
                    "coach_name": "Returning Coach",
                    "coach_email": "ret@example.com",
                    "team_name": "Episcopal Academy",
                    "coach_code": "RC2026",
                    "sport": "waterpolo",
                })

            assert result["ok"] is True
            stored = get_coach_profile("RC2026")
            assert stored is not None
            assert stored.name == "Returning Coach"
            assert stored.email == "ret@example.com"
            assert stored.program == "Episcopal Academy"
            assert stored.sport == "waterpolo"
        finally:
            reset_coach_store_for_tests(None)


if __name__ == "__main__":  # pragma: no cover
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
