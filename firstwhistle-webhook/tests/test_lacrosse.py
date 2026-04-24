"""Unit tests for the lacrosse pipeline wiring.

As of Session 5, lacrosse runs the same end-to-end pipeline as water polo.
These tests cover:

  1. `run_lacrosse_pipeline` delegates to the shared `run_pipeline`.
  2. It stamps `sport=lacrosse` on the intake before generation, even if the
     caller forgot to — the master system prompt's Part 0 branches on this.
  3. The legacy `run_lacrosse_holding` path is preserved and still sends the
     holding email on the ops path.
  4. Master system prompt contains the Section B lacrosse content we expect
     the model to route to (cheap guardrail against accidental prompt
     regressions).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _clear_config_cache():
    from app import config
    config.get_settings.cache_clear()
    config.load_system_prompt.cache_clear()


def _five_var_env(**overrides) -> dict[str, str]:
    base = {
        "ANTHROPIC_API_KEY": "sk-ant-test",
        "GITHUB_TOKEN": "ghp_test",
        "GITHUB_REPO": "coachiq-source/CoachIq",
        "RESEND_API_KEY": "re_test",
        "COACH_EMAIL_FROM": "johnmaxwell.kelly@gmail.com",
    }
    base.update(overrides)
    return base


def test_run_lacrosse_pipeline_delegates_to_run_pipeline():
    """`run_lacrosse_pipeline` must call through to the shared `run_pipeline`
    with the intake intact — same dispatch water polo uses."""
    with patch.dict(os.environ, _five_var_env(), clear=True):
        _clear_config_cache()
        from app import lacrosse

        fake_result = {
            "ok": True,
            "intake_id": "abc123",
            "slug": "jamie-rivera",
            "week_number": 1,
            "plan_url": "https://example.com/plan",
            "deck_url": "https://example.com/deck",
            "commit_sha": "deadbeef",
            "email_message_id": "msg_1",
        }
        with patch.object(lacrosse, "run_pipeline", return_value=fake_result) as rp:
            intake = {
                "intake_id": "abc123",
                "slug": "jamie-rivera",
                "coach_name": "Jamie Rivera",
                "coach_email": "jamie@example.com",
                "sport": "lacrosse",
                "level": "U12",
            }
            out = lacrosse.run_lacrosse_pipeline(intake)
            rp.assert_called_once()
            called_intake = rp.call_args[0][0]
            assert called_intake["coach_name"] == "Jamie Rivera"
            assert called_intake["sport"] == "lacrosse"
            assert out == fake_result


def test_run_lacrosse_pipeline_stamps_sport_defensively():
    """If a caller forgets to set sport=lacrosse on the intake (e.g. a replay
    script or a future code path), `run_lacrosse_pipeline` must stamp it
    itself. The master system prompt's Part 0 branches on this field."""
    with patch.dict(os.environ, _five_var_env(), clear=True):
        _clear_config_cache()
        from app import lacrosse

        with patch.object(lacrosse, "run_pipeline", return_value={"ok": True}) as rp:
            intake_missing_sport = {
                "intake_id": "xyz",
                "slug": "some-coach",
                "coach_name": "Some Coach",
                "coach_email": "s@x.com",
                # no 'sport' field on purpose
            }
            lacrosse.run_lacrosse_pipeline(intake_missing_sport)
            called = rp.call_args[0][0]
            assert called["sport"] == "lacrosse"
        # The caller's dict must NOT be mutated — lacrosse.py copies it.
        assert "sport" not in intake_missing_sport


def test_run_lacrosse_pipeline_propagates_failure_result():
    """When the shared pipeline reports a failure, `run_lacrosse_pipeline`
    must surface the same result dict (not swallow it)."""
    with patch.dict(os.environ, _five_var_env(), clear=True):
        _clear_config_cache()
        from app import lacrosse

        fail = {
            "ok": False,
            "intake_id": "abc",
            "slug": "jr",
            "stage_failed": "claude",
            "error": "Anthropic 500",
        }
        with patch.object(lacrosse, "run_pipeline", return_value=fail):
            out = lacrosse.run_lacrosse_pipeline({
                "intake_id": "abc",
                "slug": "jr",
                "coach_name": "J R",
                "coach_email": "j@r.com",
            })
            assert out["ok"] is False
            assert out["stage_failed"] == "claude"


def test_run_lacrosse_holding_still_works_for_rollback():
    """Legacy path preserved for emergency rollback. Sends ops notify + coach
    holding email, swallows errors."""
    with patch.dict(os.environ, _five_var_env(), clear=True):
        _clear_config_cache()
        from app import lacrosse
        from app.email_send import EmailResult

        intake = {
            "intake_id": "abc",
            "slug": "jamie-rivera",
            "coach_name": "Jamie Rivera",
            "coach_email": "jamie@example.com",
        }
        with patch.object(
            lacrosse, "send_ops_lacrosse_manual_email",
            return_value=EmailResult(message_id="ops", to="ops@x.com"),
        ) as ops, patch.object(
            lacrosse, "send_lacrosse_holding_email",
            return_value=EmailResult(message_id="coach", to="jamie@example.com"),
        ) as coach:
            lacrosse.run_lacrosse_holding(intake)
            ops.assert_called_once()
            coach.assert_called_once()


def test_main_dispatches_lacrosse_to_full_pipeline():
    """Regression guard: `main.py` must import `run_lacrosse_pipeline`, not
    the legacy `run_lacrosse_holding`, and dispatch lacrosse intakes to the
    full pipeline."""
    main_src = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "from .lacrosse import run_lacrosse_pipeline" in main_src, (
        "main.py must import run_lacrosse_pipeline (not run_lacrosse_holding)"
    )
    assert "background.add_task(run_lacrosse_pipeline, intake)" in main_src, (
        "main.py must dispatch lacrosse to run_lacrosse_pipeline"
    )
    # Belt-and-suspenders: make sure the legacy holding dispatch is GONE from
    # the live dispatch path.
    assert "background.add_task(run_lacrosse_holding" not in main_src


def test_master_prompt_contains_lacrosse_section():
    """Cheap guardrail against accidental prompt regressions: confirm the
    master system prompt contains the structural anchors Section B requires.
    If any of these fail, the lacrosse pipeline will still run but the
    generated plan will likely drift to water-polo content."""
    prompt_path = ROOT / "firstwhistle_master_system_prompt.md"
    assert prompt_path.exists(), f"missing master prompt at {prompt_path}"
    text = prompt_path.read_text(encoding="utf-8")

    # Routing
    assert "PART 0 — Route by Sport" in text
    # Section headers
    assert "SECTION A — WATER POLO" in text
    assert "SECTION B — LACROSSE" in text
    # Required lacrosse parts
    for marker in (
        "PART L1 — Parse the Intake (Lacrosse)",
        "PART L2 — USA Lacrosse Age-Band Structure",
        "PART L3 — Lacrosse Session Block Structure",
        "PART L4 — Language Calibration",
        "PART L5 — Drill Progressions",
        "PART L6 — Focal Drills Table (Lacrosse)",
        "PART L7 — Week 1 Focus",
        "PART L8 — KPI Grid (Lacrosse Tracking Priorities)",
        "PART L9 — Coaching Notes (Lacrosse)",
        "Lacrosse Terminology — USA Lacrosse Canonical",
    ):
        assert marker in text, f"master prompt missing marker: {marker!r}"
    # Age bands
    for band in ("U10", "U12", "U14", "U16", "MS", "JV"):
        assert band in text, f"master prompt missing age band: {band}"
    # The Matrix — USL's six-stage skill ladder
    for stage in (
        "INTRODUCTION", "EXPLORATION", "DEVELOPING",
        "PROFICIENCY", "MASTERY", "EXTENSION",
    ):
        assert stage in text, f"master prompt missing Matrix stage: {stage}"
    # Drill progressions the user named in the Session 5 brief
    for topic in (
        "Ground Balls",
        "Clearing",
        "Settled Offense",
        "Man-Up",
        "Man-Down",
    ):
        assert topic in text, f"master prompt missing drill domain: {topic}"
    # Output contract is still universal
    assert "PART 10 — Output Format" in text
    assert "<!-- ===== FULL PLAN START ===== -->" in text
    assert "<!-- ===== DECK SHEET START ===== -->" in text


def test_lacrosse_intake_flows_through_intake_parser():
    """End-to-end (no network): a realistic Formspree lacrosse intake parses
    cleanly and ends up with the fields the master prompt's Part L1 expects."""
    with patch.dict(os.environ, _five_var_env(), clear=True):
        _clear_config_cache()
        from app.intake import parse_formspree_payload

        body = {
            "form": "myklwjnp",
            "submission": {
                "_date": "2026-04-22T15:00:00+00:00",
                "sport": "lacrosse",
                "name": "Jamie Rivera",
                "email": "jamie@example.com",
                "school": "Conestoga Youth Lacrosse",
                "gender": "boys",
                "level": "U12",
                "rosterSize": "18",
                "coachingYears": "1",
                "practiceFreq": "3x / week",
                "practiceLen": "75 min",
                "fieldAccess": "half field",
                "biggestGap": "we can't clear the ball — defense ends up chucking it",
                "goaliesAvailable": "1",
            },
        }
        intake = parse_formspree_payload(body)
        assert intake["coach_name"] == "Jamie Rivera"
        assert intake["coach_email"] == "jamie@example.com"
        assert intake["slug"] == "jamie-rivera"
        # Lacrosse-specific fields land in extras since they aren't canonical
        # water-polo aliases. The master prompt reads extras on Part L1.
        assert intake["extras"].get("sport") == "lacrosse"
        assert intake["extras"].get("gender") == "boys"
        # level IS aliased (age_group / level / division)
        assert intake["level"] == "U12"
        assert intake["extras"].get("coachingYears") == "1"
        assert intake["extras"].get("biggestGap", "").startswith("we can't clear")


if __name__ == "__main__":
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
