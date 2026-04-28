"""Unit tests for the basketball pipeline wiring (Session 17).

Basketball runs the same end-to-end pipeline as water polo and lacrosse
(intake → Claude → parse → GitHub → email). Game prep is not yet a
first-class basketball pipeline; the dispatcher routes basketball
gameprep submissions to a stub that falls back to the weekly flow,
matching Part 0 of the master system prompt.

Tests cover:

  1. `run_basketball_pipeline` delegates to the shared `run_pipeline`.
  2. It stamps `sport=basketball` on the intake before generation, even
     if the caller forgot to — Part 0 routes on this field.
  3. `run_basketball_gameprep_pipeline` falls back to the weekly
     basketball pipeline (no separate game-prep pipeline yet).
  4. `main.py` registers `/webhook/formspree/basketball`, dispatches on
     `form_type` to the weekly vs gameprep paths, and uses
     `FORMSPREE_SECRET_BASKETBALL` for HMAC verification.
  5. The end-to-end webhook routing through FastAPI's TestClient
     correctly dispatches basketball intakes (both weekly and gameprep)
     to the right background-task handler.
  6. `Settings` exposes `formspree_secret_basketball` and reads
     `FORMSPREE_SECRET_BASKETBALL` from the environment.
  7. Master system prompt contains the SECTION C anchors and the
     basketball-specific terminology / drill names the pipeline
     depends on.
  8. `email_send` already has Court Sheet / on the bench / to the bench
     entries; the gameprep button is suppressed for basketball
     (basketball has no game-prep form yet, so the link would point at
     the wrong sport's form).
  9. A realistic Formspree basketball intake parses cleanly through
     the shared intake parser, with all basketball-specific fields
     landing in `extras` for the master prompt's Part B1 to read.
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Stub the five required env vars so `get_settings()` works on import.
os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic")
os.environ.setdefault("GITHUB_TOKEN", "test-token")
os.environ.setdefault("GITHUB_REPO", "coachiq-source/CoachIq")
os.environ.setdefault("RESEND_API_KEY", "test-resend")
os.environ.setdefault("COACH_EMAIL_FROM", "coach@example.com")


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


@contextmanager
def _patched_env(**overrides):
    """Apply env overrides + clear `get_settings()` lru_cache on entry
    AND on exit, so per-test state never leaks into later tests via the
    cached `Settings` instance."""
    env = _five_var_env(**overrides)
    with patch.dict(os.environ, env, clear=True):
        _clear_config_cache()
        try:
            yield
        finally:
            _clear_config_cache()


# ---------------------------------------------------------------------------
# 1–3. basketball.py module — pipeline delegation + gameprep stub
# ---------------------------------------------------------------------------

def test_run_basketball_pipeline_delegates_to_run_pipeline():
    """`run_basketball_pipeline` must call through to the shared
    `run_pipeline` with the intake intact — same dispatch water polo and
    lacrosse use."""
    with _patched_env():
        from app import basketball

        fake_result = {
            "ok": True,
            "intake_id": "abc123",
            "slug": "casey-jones",
            "week_number": 1,
            "plan_url": "https://example.com/plan",
            "deck_url": "https://example.com/court",
            "commit_sha": "deadbeef",
            "email_message_id": "msg_1",
        }
        with patch.object(basketball, "run_pipeline", return_value=fake_result) as rp:
            intake = {
                "intake_id": "abc123",
                "slug": "casey-jones",
                "coach_name": "Casey Jones",
                "coach_email": "casey@example.com",
                "sport": "basketball",
                "level": "Middle School",
            }
            out = basketball.run_basketball_pipeline(intake)
            rp.assert_called_once()
            called_intake = rp.call_args[0][0]
            assert called_intake["coach_name"] == "Casey Jones"
            assert called_intake["sport"] == "basketball"
            assert out == fake_result


def test_run_basketball_pipeline_stamps_sport_defensively():
    """If a caller forgets to set sport=basketball on the intake (e.g. a
    replay script or a future code path), `run_basketball_pipeline` must
    stamp it itself. Part 0 of the master system prompt branches on this
    field."""
    with _patched_env():
        from app import basketball

        with patch.object(basketball, "run_pipeline", return_value={"ok": True}) as rp:
            intake_missing_sport = {
                "intake_id": "xyz",
                "slug": "some-coach",
                "coach_name": "Some Coach",
                "coach_email": "s@x.com",
                # no 'sport' field on purpose
            }
            basketball.run_basketball_pipeline(intake_missing_sport)
            called = rp.call_args[0][0]
            assert called["sport"] == "basketball"
        # The caller's dict must NOT be mutated — basketball.py copies it.
        assert "sport" not in intake_missing_sport


def test_run_basketball_pipeline_propagates_failure_result():
    """When the shared pipeline reports a failure, `run_basketball_pipeline`
    must surface the same result dict (not swallow it)."""
    with _patched_env():
        from app import basketball

        fail = {
            "ok": False,
            "intake_id": "abc",
            "slug": "cj",
            "stage_failed": "claude",
            "error": "Anthropic 500",
        }
        with patch.object(basketball, "run_pipeline", return_value=fail):
            out = basketball.run_basketball_pipeline({
                "intake_id": "abc",
                "slug": "cj",
                "coach_name": "C J",
                "coach_email": "c@j.com",
            })
            assert out["ok"] is False
            assert out["stage_failed"] == "claude"


def test_run_basketball_gameprep_pipeline_falls_back_to_weekly():
    """Basketball game prep is not yet a first-class pipeline. The stub
    must dispatch to `run_basketball_pipeline` so the coach still gets a
    weekly plan with a coaching-notes mismatch flag (Part 0 of the
    master prompt). Confirms the dispatcher contract — when basketball
    gameprep IS built, this test will need to flip to assert the new
    pipeline is called instead."""
    with _patched_env():
        from app import basketball

        with patch.object(
            basketball, "run_basketball_pipeline",
            return_value={"ok": True, "intake_id": "i1"},
        ) as wk:
            intake = {
                "intake_id": "i1",
                "slug": "x",
                "coach_name": "X Y",
                "coach_email": "x@y.com",
                "form_type": "gameprep",
                "opponent": "Some Opp",
            }
            out = basketball.run_basketball_gameprep_pipeline(intake)
            wk.assert_called_once()
            assert out["ok"] is True


# ---------------------------------------------------------------------------
# 4. main.py wiring — route, dispatcher, secret reference
# ---------------------------------------------------------------------------

def test_main_basketball_route_and_dispatcher():
    """Regression guard: `main.py` must import the basketball pipeline,
    register `/webhook/formspree/basketball`, and dispatch on
    `form_type`. The basketball gameprep branch must dispatch to the
    stub (`run_basketball_gameprep_pipeline`) so the wiring stays
    symmetrical with water polo / lacrosse."""
    main_src = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert (
        "from .basketball import run_basketball_pipeline, "
        "run_basketball_gameprep_pipeline"
    ) in main_src, "main.py must import both basketball entry points"
    assert '@app.post("/webhook/formspree/basketball")' in main_src, (
        "main.py must register /webhook/formspree/basketball"
    )
    assert (
        "background.add_task(run_basketball_pipeline, intake)"
    ) in main_src, "main.py must dispatch weekly basketball intakes"
    assert (
        "background.add_task(run_basketball_gameprep_pipeline, intake)"
    ) in main_src, "main.py must dispatch basketball gameprep intakes"
    # Secret name must thread through.
    assert "formspree_secret_basketball" in main_src, (
        "main.py must read the basketball signing secret"
    )
    # Branch on form_type for all three sports now (waterpolo, lacrosse,
    # basketball).
    assert main_src.count('form_type == "gameprep"') >= 3, (
        "main.py must branch on form_type == \"gameprep\" for all three sports"
    )


def test_webhook_basketball_routes_weekly_and_gameprep():
    """End-to-end routing via the FastAPI TestClient: a basketball intake
    with `formType: gameprep` submitted to /webhook/formspree/basketball
    must dispatch to `run_basketball_gameprep_pipeline`; an intake with
    no formType must dispatch to `run_basketball_pipeline`."""
    import hashlib
    import hmac
    import json
    import time

    with _patched_env(FORMSPREE_SECRET_BASKETBALL="test-basketball-secret"):
        from fastapi.testclient import TestClient
        from app import main as main_mod

        client = TestClient(main_mod.app)

        def _sign(raw: bytes) -> str:
            ts = int(time.time())
            digest = hmac.new(
                b"test-basketball-secret",
                str(ts).encode("ascii") + b"." + raw,
                hashlib.sha256,
            ).hexdigest()
            return f"t={ts},v1={digest}"

        # Case A — formType=gameprep → run_basketball_gameprep_pipeline.
        body_gp = json.dumps({
            "sport": "basketball",
            "formType": "gameprep",
            "name": "Casey Jones",
            "email": "casey@example.com",
            "opponent": "Riverside Hoops",
        }).encode("utf-8")
        with patch.object(main_mod, "run_basketball_gameprep_pipeline") as gp, \
             patch.object(main_mod, "run_basketball_pipeline") as wk:
            resp = client.post(
                "/webhook/formspree/basketball",
                data=body_gp,
                headers={
                    "Content-Type": "application/json",
                    "Formspree-Signature": _sign(body_gp),
                },
            )
        assert resp.status_code == 202, resp.text
        assert resp.json()["form_type"] == "gameprep"
        assert resp.json()["sport"] == "basketball"
        gp.assert_called_once()
        wk.assert_not_called()
        scheduled = gp.call_args.args[0]
        assert scheduled["coach_name"] == "Casey Jones"
        assert scheduled["sport"] == "basketball"
        assert scheduled["form_type"] == "gameprep"

        # Case B — no formType → run_basketball_pipeline (weekly).
        body_wk = json.dumps({
            "sport": "basketball",
            "name": "Casey Jones",
            "email": "casey@example.com",
            "level": "Middle School",
        }).encode("utf-8")
        with patch.object(main_mod, "run_basketball_gameprep_pipeline") as gp, \
             patch.object(main_mod, "run_basketball_pipeline") as wk:
            resp = client.post(
                "/webhook/formspree/basketball",
                data=body_wk,
                headers={
                    "Content-Type": "application/json",
                    "Formspree-Signature": _sign(body_wk),
                },
            )
        assert resp.status_code == 202, resp.text
        assert resp.json()["form_type"] == "week"
        assert resp.json()["sport"] == "basketball"
        wk.assert_called_once()
        gp.assert_not_called()


def test_webhook_basketball_rejects_bad_signature():
    """A request to /webhook/formspree/basketball with an invalid
    HMAC signature must 401, not silently dispatch — same shape as the
    waterpolo and lacrosse routes."""
    import json

    with _patched_env(FORMSPREE_SECRET_BASKETBALL="real-basketball-secret"):
        from fastapi.testclient import TestClient
        from app import main as main_mod

        client = TestClient(main_mod.app)
        body = json.dumps({
            "sport": "basketball",
            "name": "X",
            "email": "x@y.com",
        }).encode("utf-8")
        with patch.object(main_mod, "run_basketball_pipeline") as wk:
            resp = client.post(
                "/webhook/formspree/basketball",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Formspree-Signature": "t=1,v1=deadbeef",
                },
            )
        assert resp.status_code == 401
        wk.assert_not_called()


# ---------------------------------------------------------------------------
# 6. Config — basketball signing secret
# ---------------------------------------------------------------------------

def test_config_exposes_basketball_secret_field():
    """`Settings` must expose `formspree_secret_basketball` and read
    `FORMSPREE_SECRET_BASKETBALL` from the environment. Default is the
    empty string when unset (consistent with waterpolo / lacrosse)."""
    with _patched_env(FORMSPREE_SECRET_BASKETBALL="my-bb-secret"):
        from app.config import get_settings

        s = get_settings()
        assert hasattr(s, "formspree_secret_basketball")
        assert s.formspree_secret_basketball == "my-bb-secret"

    with _patched_env():  # secret not set
        from app.config import get_settings

        s = get_settings()
        assert s.formspree_secret_basketball == ""


def test_health_endpoint_reports_basketball_secret_status():
    """The /health endpoint must report whether the basketball secret
    is configured, alongside waterpolo / lacrosse."""
    with _patched_env(FORMSPREE_SECRET_BASKETBALL="present"):
        from fastapi.testclient import TestClient
        from app.main import app

        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["basketball_secret_configured"] is True
        assert "waterpolo_secret_configured" in body
        assert "lacrosse_secret_configured" in body


# ---------------------------------------------------------------------------
# 7. Master system prompt — Section C anchors
# ---------------------------------------------------------------------------

def test_master_prompt_contains_basketball_section():
    """Cheap guardrail against accidental prompt regressions: confirm
    the master system prompt contains the structural anchors SECTION C
    requires. If any of these fail, the basketball pipeline will still
    run but the generated plan will likely drift to water-polo content."""
    prompt_path = ROOT / "firstwhistle_master_system_prompt.md"
    assert prompt_path.exists(), f"missing master prompt at {prompt_path}"
    text = prompt_path.read_text(encoding="utf-8")

    # Routing
    assert "PART 0 — Route by Sport" in text
    # Section header
    assert "SECTION C — BASKETBALL" in text
    # All ten basketball parts
    for marker in (
        "PART B1 — Parse the Intake (Basketball)",
        "PART B2 — USA Basketball Age-Band Structure",
        "PART B3 — Basketball Session Block Structure",
        "PART B4 — Language Calibration",
        "PART B5 — Drill Progressions",
        "PART B6 — Focal Drills Table (Basketball)",
        "PART B7 — Week 1 Focus + Coach Decision Callouts (Basketball)",
        "PART B8 — KPI Grid (Basketball Tracking Priorities)",
        "PART B9 — Coaching Notes (Basketball)",
        "PART B10 — Basketball Terminology — USA Basketball Canonical",
    ):
        assert marker in text, f"master prompt missing marker: {marker!r}"
    # Age bands
    for band in ("Youth (U10)", "Youth (U12)", "Middle School", "JV", "Varsity"):
        assert band in text, f"master prompt missing age band: {band}"
    # USA Basketball terminology the user named in the Session 17 brief
    for term in (
        "triple threat",
        "ball handling",
        "lay-up",
        "boxing out",
        "pick and roll",
        "motion offense",
        "help defense",
        "closeout",
        "transition",
        "outlet pass",
        "press break",
        "half court",
        "zone defense",
        "man defense",
        "elbow",
        "block",
        "charge",
        "box out",
    ):
        assert term in text.lower() or term in text, (
            f"master prompt missing basketball term: {term!r}"
        )
    # Drill references the user named explicitly
    for drill in (
        "Partner Shooting",
        "Chase Down Layups",
        "2-on-2 Box Out",
        "No Hands Defense",
        "Tip Transition",
    ):
        assert drill in text, f"master prompt missing drill: {drill!r}"
    # Sheet name + surface phrases (Part 10.2 already handles these but
    # SECTION C must reinforce them so the model can't drift).
    for phrase in ("Court Sheet", "on the bench", "courtside"):
        assert phrase in text, f"master prompt missing surface phrase: {phrase!r}"
    # Output contract is still universal
    assert "PART 10 — Output Format" in text
    assert "<!-- ===== FULL PLAN START ===== -->" in text
    assert "<!-- ===== DECK SHEET START ===== -->" in text


# ---------------------------------------------------------------------------
# 8. email_send — Court Sheet, on the bench, gameprep button suppression
# ---------------------------------------------------------------------------

def test_email_send_basketball_uses_court_sheet_and_bench_surface():
    """The weekly coach email rendered with sport=basketball must use
    'Court sheet' as the sheet label and 'on the bench' as the surface
    phrase. Confirms the user-listed Session 17 wiring (sheet name +
    surface) is intact in `email_send.py`."""
    from app import email_send

    captured: dict = {}

    class _FakeResend:
        @staticmethod
        def send(params):
            captured.update(params)
            return {"id": "resend-bb"}

    with patch.object(email_send, "resend") as mock_resend:
        mock_resend.Emails = _FakeResend
        email_send.send_coach_email(
            coach_name="Casey Jones",
            coach_email="casey@example.com",
            week_number=1,
            plan_url="https://example.invalid/coaches/cj/week1-plan.html",
            deck_url="https://example.invalid/coaches/cj/week1-deck.html",
            sport="basketball",
        )

    html = captured["html"]
    text = captured["text"]
    assert "Court sheet" in html
    assert "Court sheet" in text
    assert "on the bench" in html
    assert "on the bench" in text
    # No water-polo / lacrosse leakage into a basketball email.
    for banned in ("Deck sheet", "Field sheet", "on deck", "on the field"):
        assert banned not in html, f"HTML leaked banned phrase: {banned!r}"
        assert banned not in text, f"text leaked banned phrase: {banned!r}"


def test_email_send_basketball_links_basketball_gameprep_and_postgame():
    """Basketball weekly email must render the Game prep intake button
    and the Week in Review button, both pointing at the basketball
    URLs (coachprep.co/gameprep/basketball.html and
    coachprep.co/postgame/basketball.html). Confirms the cross-sport
    URL leakage guard from Session 17 still holds — basketball must
    NOT link to a water-polo or lacrosse form."""
    from app import email_send

    captured: dict = {}

    class _FakeResend:
        @staticmethod
        def send(params):
            captured.update(params)
            return {"id": "resend-bb"}

    with patch.object(email_send, "resend") as mock_resend:
        mock_resend.Emails = _FakeResend
        email_send.send_coach_email(
            coach_name="Casey Jones",
            coach_email="casey@example.com",
            week_number=1,
            plan_url="https://example.invalid/coaches/cj/week1-plan.html",
            deck_url="https://example.invalid/coaches/cj/week1-deck.html",
            sport="basketball",
            coach_code="CJ2026",
        )

    html = captured["html"]
    text = captured["text"]
    # Both buttons render now that basketball has dedicated URLs.
    assert "Game prep intake" in html
    assert "Game prep intake" in text
    assert "Week in Review" in html
    assert "Week in Review" in text
    # And they point at the basketball URLs (with the coach code
    # appended for prefill, mirroring water polo / lacrosse behaviour).
    assert "coachprep.co/gameprep/basketball.html?code=CJ2026" in html
    assert "coachprep.co/gameprep/basketball.html?code=CJ2026" in text
    assert "coachprep.co/postgame/basketball.html?code=CJ2026" in html
    assert "coachprep.co/postgame/basketball.html?code=CJ2026" in text
    # No cross-sport URL leakage — basketball must never link to a
    # water-polo or lacrosse form.
    assert "gameprep/lacrosse.html" not in html
    assert "postgame/lacrosse.html" not in html
    assert "postgame/waterpolo.html" not in html
    # The water-polo gameprep URL ends in `/gameprep/` (no html file);
    # make sure that URL doesn't leak into the basketball email either.
    assert "coachiq-source.github.io/CoachIq/gameprep/?code=" not in html


def test_email_send_waterpolo_default_keeps_gameprep_button():
    """Backward compat: water polo (and the no-sport default) must
    still render the game-prep button. The basketball-suppression
    change must not regress water-polo."""
    from app import email_send

    captured: dict = {}

    class _FakeResend:
        @staticmethod
        def send(params):
            captured.update(params)
            return {"id": "resend-wp"}

    with patch.object(email_send, "resend") as mock_resend:
        mock_resend.Emails = _FakeResend
        email_send.send_coach_email(
            coach_name="Anna Safford",
            coach_email="anna@example.com",
            week_number=2,
            plan_url="https://example.invalid/coaches/anna/week2-plan.html",
            deck_url="https://example.invalid/coaches/anna/week2-deck.html",
            sport="waterpolo",
        )

    html = captured["html"]
    text = captured["text"]
    assert "Game prep intake" in html
    assert "Game prep intake" in text
    assert "Deck sheet" in html


def test_email_send_lacrosse_keeps_gameprep_button():
    """Lacrosse must also continue to render the game-prep button —
    the basketball suppression must not catch lacrosse in its blast
    radius."""
    from app import email_send

    captured: dict = {}

    class _FakeResend:
        @staticmethod
        def send(params):
            captured.update(params)
            return {"id": "resend-lx"}

    with patch.object(email_send, "resend") as mock_resend:
        mock_resend.Emails = _FakeResend
        email_send.send_coach_email(
            coach_name="Jamie Rivera",
            coach_email="jamie@example.com",
            week_number=1,
            plan_url="https://example.invalid/coaches/jr/week1-plan.html",
            deck_url="https://example.invalid/coaches/jr/week1-deck.html",
            sport="lacrosse",
        )

    html = captured["html"]
    assert "Game prep intake" in html
    assert "lacrosse.html" in html
    assert "Field sheet" in html


# ---------------------------------------------------------------------------
# 9. End-to-end intake parsing
# ---------------------------------------------------------------------------

def test_basketball_intake_flows_through_intake_parser():
    """A realistic Formspree basketball intake parses cleanly and ends
    up with the basketball-specific fields the master prompt's Part B1
    expects (in `extras` since they aren't canonical aliases)."""
    with _patched_env():
        from app.intake import parse_formspree_payload

        body = {
            "form": "xkoklraj",
            "submission": {
                "_date": "2026-04-27T15:00:00+00:00",
                "sport": "basketball",
                "name": "Casey Jones",
                "email": "casey@example.com",
                "program": "Jefferson Middle School",
                "coachCode": "CJ2026",
                "gender": "girls",
                "level": "Middle School",
                "experience": "1–2 years",
                "played": "Yes — high school",
                "rosterSize": "12",
                "playerExperience": "Mixed — some experienced, some new",
                "devStage": (
                    "Fundamentals solid — starting to install plays "
                    "and concepts"
                ),
                "thinAt": "Guard depth, Post / big depth",
                "pointGuard": (
                    "Developing — still building decision-making "
                    "under pressure"
                ),
                "personnelChallenge": (
                    "two starters out for the first three weeks with "
                    "shoulder rehab"
                ),
                "offense": (
                    "Motion offense — players move, cut, and screen "
                    "to create open looks"
                ),
                "defense": (
                    "Man-to-man — pressure the ball carrier "
                    "everywhere on the court"
                ),
                "rebounding": "2",
                "freeThrows": "Inconsistent — hit or miss depending on the player",
                "pressureHandling": "We struggle — turnovers when pressed",
                "transitionDefense": (
                    "We struggle to get back — opponent scores in transition"
                ),
                "practiceFreq": "3 times a week",
                "practiceLen": "60 minutes",
                "practiceStart": "Nov 10",
                "firstGame": "Dec 1",
                "primaryTarget": (
                    "Player development — every player improves this season"
                ),
                "leastPrepared": "Halftime — I don't know what to say",
                "fixThisWeek": (
                    "we get out-rebounded every game; nobody boxes out"
                ),
                "whatWorking": (
                    "our energy and effort is great. don't want to "
                    "over-coach and kill that"
                ),
            },
        }
        intake = parse_formspree_payload(body)
        assert intake["coach_name"] == "Casey Jones"
        assert intake["coach_email"] == "casey@example.com"
        # `program` is aliased to `team_name`.
        assert intake["team_name"] == "Jefferson Middle School"
        assert intake["coach_code"] == "CJ2026"
        assert intake["slug"] == "casey-jones"
        # `level` is one of the canonical aliases.
        assert intake["level"] == "Middle School"
        # Basketball-specific fields land in `extras` (Part B1 reads them).
        for key in (
            "rosterSize",
            "playerExperience",
            "devStage",
            "thinAt",
            "pointGuard",
            "personnelChallenge",
            "offense",
            "defense",
            "rebounding",
            "freeThrows",
            "pressureHandling",
            "transitionDefense",
            "practiceFreq",
            "practiceLen",
            "practiceStart",
            "firstGame",
            "primaryTarget",
            "leastPrepared",
            "fixThisWeek",
            "whatWorking",
            "experience",
            "played",
            "gender",
        ):
            assert key in intake["extras"], (
                f"basketball field {key!r} missing from extras"
            )
        # And the sport flag itself is present in extras (Part 0 routes
        # on it, falling through to the canonical `sport` field on the
        # intake itself once stamped by the dispatcher).
        assert intake["extras"]["sport"] == "basketball"


# ---------------------------------------------------------------------------
# 10. Coach store accepts basketball as a valid sport
# ---------------------------------------------------------------------------

def test_coach_store_accepts_basketball_sport():
    """Returning-coach store must accept `sport=basketball` so the
    weekly basketball pipeline's pre-flight `upsert_coach_profile`
    call doesn't fail validation."""
    import tempfile

    from app import coach_store

    with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as f:
        db_path = f.name
    try:
        coach_store.reset_coach_store_for_tests(db_path)
        profile = coach_store.upsert_coach_profile(
            code="CJ2026",
            name="Casey Jones",
            email="casey@example.com",
            program="Jefferson Middle School",
            sport="basketball",
        )
        assert profile.sport == "basketball"
    finally:
        coach_store.reset_coach_store_for_tests(None)


if __name__ == "__main__":  # pragma: no cover
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
