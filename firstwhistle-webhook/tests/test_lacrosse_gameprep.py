"""Tests for the lacrosse game-prep pipeline (Session 12).

Water-polo game prep (Session 7) and lacrosse game prep share a pipeline
core. Lacrosse game-prep intakes submit to the same Formspree form as
weekly lacrosse intakes; the webhook branches on `formType` server-side
(same shape as the waterpolo route). These tests cover the lacrosse-
specific behavior on top of the water-polo tests in `test_gameprep.py`:

  1. `deploy_gameprep` accepts a `file_prefix` and the lacrosse pipeline
     passes `"lacrosse-gameprep"` so files land at
     `coaches/<slug>/lacrosse-gameprep-<opponent-slug>.html`.
  2. `run_lacrosse_gameprep_pipeline` stamps `sport=lacrosse` + `form_type=
     gameprep`, forwards `sport="lacrosse"` to `send_gameprep_email`, and
     uses the lacrosse file prefix on deploy.
  3. Ops-notify on stage failure uses the `lax-gameprep:<stage>` label so
     Railway log greps can tell water-polo from lacrosse failures.
  4. `generate_gameprep` reads `intake["sport"]` and points the user
     message at Section LAX-G for lacrosse vs Section WP-G for water polo.
  5. `send_gameprep_email` with `sport="lacrosse"` renders lacrosse-specific
     body copy (EMO/EMD, goalie, field) — no water-polo terminology leaks.
  6. `main.py`'s lacrosse dispatcher branches on `form_type == "gameprep"`
     and routes to `run_lacrosse_gameprep_pipeline` (the same pattern the
     waterpolo dispatcher already uses for its game-prep branch).
  7. Master system prompt contains the required SECTION LAX-G anchors.
"""
from __future__ import annotations

import os
import sys
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


from contextlib import contextmanager


@contextmanager
def _patched_env(**overrides):
    """Context manager that applies env overrides AND clears the
    `get_settings()` lru_cache both on entry and on exit.

    The `patch.dict(..., clear=True)` pattern used elsewhere in the suite
    restores env on exit — but the cached `Settings` instance persists and
    would leak empty-secret values into later tests that hit the FastAPI
    webhook routes (e.g. test_postgame's TestClient-based tests). Double
    cache-clear keeps per-test state isolated without touching other test
    files.
    """
    env = _five_var_env(**overrides)
    with patch.dict(os.environ, env, clear=True):
        _clear_config_cache()
        try:
            yield
        finally:
            # Flush the cache before we leave the patched env so the next
            # `get_settings()` (in a later test) reads the real process env,
            # not the cleared-env snapshot we just took.
            _clear_config_cache()
    # env is now restored; cache is empty; next get_settings() re-reads env.


# ---------------------------------------------------------------------------
# 1. deploy_gameprep file_prefix
# ---------------------------------------------------------------------------

def test_deploy_gameprep_uses_lacrosse_file_prefix_when_supplied():
    """`file_prefix="lacrosse-gameprep"` → file lands at
    `coaches/<slug>/lacrosse-gameprep-<opp>.html`."""
    from app import github_deploy
    from app.config import slugify

    expected_opp_slug = slugify("St. Mary's Prep")
    expected_path = (
        f"coaches/jamie-rivera/lacrosse-gameprep-{expected_opp_slug}.html"
    )

    captured = {}

    def fake_put(client, path, content, message, sha):
        captured["path"] = path
        return {"commit": {"sha": "feed1234"}}

    with patch.object(github_deploy, "_get_existing_sha", return_value=None), \
         patch.object(github_deploy, "_put_file", side_effect=fake_put), \
         patch.object(github_deploy, "httpx") as mock_httpx:
        mock_httpx.Client.return_value.__enter__.return_value = MagicMock()
        result = github_deploy.deploy_gameprep(
            slug="jamie-rivera",
            opponent="St. Mary's Prep",
            gameprep_html="<!doctype html><html></html>",
            coach_name="Jamie Rivera",
            intake_id="abc",
            file_prefix="lacrosse-gameprep",
        )

    assert captured["path"] == expected_path
    assert result.path == expected_path
    assert result.url.endswith("/" + expected_path)
    assert result.opponent_slug == expected_opp_slug


def test_deploy_gameprep_default_file_prefix_is_waterpolo_shape():
    """Backward compatibility: default file_prefix is still `gameprep`
    (no sport namespace) so the water-polo Session 7 behavior doesn't
    shift under us."""
    from app import github_deploy

    captured = {}

    def fake_put(client, path, content, message, sha):
        captured["path"] = path
        return {"commit": {"sha": "beef0000"}}

    with patch.object(github_deploy, "_get_existing_sha", return_value=None), \
         patch.object(github_deploy, "_put_file", side_effect=fake_put), \
         patch.object(github_deploy, "httpx") as mock_httpx:
        mock_httpx.Client.return_value.__enter__.return_value = MagicMock()
        github_deploy.deploy_gameprep(
            slug="jamie-rivera",
            opponent="Opp",
            gameprep_html="<!doctype html><html></html>",
            coach_name="Jamie Rivera",
            intake_id="abc",
            # no file_prefix → default
        )

    assert captured["path"] == "coaches/jamie-rivera/gameprep-opp.html"


# ---------------------------------------------------------------------------
# 3. run_lacrosse_gameprep_pipeline
# ---------------------------------------------------------------------------

def test_run_lacrosse_gameprep_pipeline_happy_path():
    """End-to-end (all I/O stubbed): pipeline stamps sport=lacrosse and
    form_type=gameprep, forwards sport to email, and asks
    `deploy_gameprep` for the lacrosse file prefix. The `sport=lacrosse`
    stamp on the intake is what tells `generate_gameprep` which
    master-prompt section to point at (see claude_client)."""
    from app import gameprep
    from app.github_deploy import GamePrepDeployResult
    from app.email_send import EmailResult

    claude_response = (
        "<!-- ===== GAME PREP START ===== -->\n"
        "<!DOCTYPE html><html><body><h1>Game Prep vs Opp</h1></body></html>\n"
        "<!-- ===== GAME PREP END ===== -->"
    )
    fake_deploy = GamePrepDeployResult(
        url="https://example.invalid/coaches/jr/lacrosse-gameprep-opp.html",
        commit_sha="cab1234",
        path="coaches/jr/lacrosse-gameprep-opp.html",
        opponent_slug="opp",
    )

    seen_intake: dict = {}

    def fake_generate(intake):
        seen_intake.update(dict(intake))
        return claude_response

    with patch.object(gameprep, "generate_gameprep", side_effect=fake_generate), \
         patch.object(gameprep, "deploy_gameprep", return_value=fake_deploy) as mock_deploy, \
         patch.object(gameprep, "send_gameprep_email",
                      return_value=EmailResult(message_id="msg-lx", to="jamie@example.com")) as mock_email:
        intake = {
            "intake_id": "i-lax",
            "slug": "jr",
            "coach_name": "Jamie Rivera",
            "coach_email": "jamie@example.com",
            "opponent": "Opp",
        }
        result = gameprep.run_lacrosse_gameprep_pipeline(intake)

    assert result["ok"] is True
    assert result["sport"] == "lacrosse"
    assert result["gameprep_url"].endswith("lacrosse-gameprep-opp.html")
    assert result["opponent_slug"] == "opp"
    assert result["email_message_id"] == "msg-lx"

    # Intake stamped defensively with sport + form_type — this is what
    # `generate_gameprep` reads to pick the right master-prompt section.
    assert seen_intake["sport"] == "lacrosse"
    assert seen_intake["form_type"] == "gameprep"

    # Deploy was asked to use the lacrosse file prefix.
    assert mock_deploy.call_args.kwargs["file_prefix"] == "lacrosse-gameprep"
    assert mock_deploy.call_args.kwargs["opponent"] == "Opp"
    # Email got the lacrosse sport so the body uses lacrosse wording.
    assert mock_email.call_args.kwargs["sport"] == "lacrosse"
    assert mock_email.call_args.kwargs["opponent"] == "Opp"


def test_run_lacrosse_gameprep_pipeline_stamps_sport_defensively():
    """Replay / test harness intakes that don't set sport or form_type
    must still come out with sport=lacrosse + form_type=gameprep before
    Claude sees them."""
    from app import gameprep

    captured: dict = {}

    def fake_generate(intake):
        captured.update(dict(intake))
        return (
            "<!-- ===== GAME PREP START ===== -->\n"
            "<!DOCTYPE html><html></html>\n"
            "<!-- ===== GAME PREP END ===== -->"
        )

    with patch.object(gameprep, "generate_gameprep", side_effect=fake_generate), \
         patch.object(gameprep, "deploy_gameprep") as mock_deploy, \
         patch.object(gameprep, "send_gameprep_email") as mock_email:
        mock_deploy.return_value = MagicMock(
            url="u", commit_sha="s", path="p", opponent_slug="o"
        )
        mock_email.return_value.message_id = "m"

        intake_bare = {
            "intake_id": "i1",
            "slug": "x",
            "coach_name": "X Y",
            "coach_email": "x@y.com",
            "opponent": "Opp",
            # no sport, no form_type
        }
        gameprep.run_lacrosse_gameprep_pipeline(intake_bare)

    assert captured["sport"] == "lacrosse"
    assert captured["form_type"] == "gameprep"
    # Caller's dict must not be mutated.
    assert "sport" not in intake_bare
    assert "form_type" not in intake_bare


def test_run_lacrosse_gameprep_pipeline_reads_opponent_from_extras():
    """Opponent usually lives in extras (not a canonical alias). Pipeline
    should find it there and forward it to deploy + email."""
    from app import gameprep

    with patch.object(gameprep, "generate_gameprep",
                      return_value=(
                          "<!-- ===== GAME PREP START ===== -->\n"
                          "<!DOCTYPE html><html></html>\n"
                          "<!-- ===== GAME PREP END ===== -->"
                      )), \
         patch.object(gameprep, "deploy_gameprep") as mock_deploy, \
         patch.object(gameprep, "send_gameprep_email") as mock_email:
        mock_deploy.return_value = MagicMock(
            url="u", commit_sha="s", path="p", opponent_slug="extras-opp"
        )
        mock_email.return_value.message_id = "m"

        gameprep.run_lacrosse_gameprep_pipeline({
            "intake_id": "i1",
            "slug": "x",
            "coach_name": "X Y",
            "coach_email": "x@y.com",
            "extras": {"opponent": "Extras Opp"},
        })

    assert mock_deploy.call_args.kwargs["opponent"] == "Extras Opp"
    assert mock_email.call_args.kwargs["opponent"] == "Extras Opp"
    assert mock_deploy.call_args.kwargs["file_prefix"] == "lacrosse-gameprep"


def test_run_lacrosse_gameprep_pipeline_failure_notifies_ops():
    """Stage failure → result has ok=False, stage_failed set, ops email
    sent with the `lax-gameprep:` stage prefix so Railway log greps can
    distinguish sport."""
    from app import gameprep
    from app.claude_client import ClaudeGenerationError

    with patch.object(gameprep, "generate_gameprep",
                      side_effect=ClaudeGenerationError("boom-lax")), \
         patch.object(gameprep, "send_ops_failure_email") as mock_ops:
        result = gameprep.run_lacrosse_gameprep_pipeline({
            "intake_id": "i-fail-lax",
            "slug": "x",
            "coach_name": "X",
            "coach_email": "x@y.com",
            "opponent": "Opp",
        })

    assert result["ok"] is False
    assert result["stage_failed"] == "claude"
    assert result["sport"] == "lacrosse"
    assert "boom-lax" in result["error"]
    mock_ops.assert_called_once()
    ops_kwargs = mock_ops.call_args.kwargs
    assert ops_kwargs["stage"] == "lax-gameprep:claude"
    assert ops_kwargs["intake_id"] == "i-fail-lax"


# ---------------------------------------------------------------------------
# 4. Claude user message uses the lacrosse section pointer
# ---------------------------------------------------------------------------

def test_generate_gameprep_sport_lacrosse_user_message_points_to_lax_g():
    """When the intake carries `sport="lacrosse"`, the user message must
    mention SECTION LAX-G (Parts LG1–LG10 + LG-Field), not WP-G.
    Regression guard against the model drifting into water-polo
    terminology on a lacrosse intake."""
    with _patched_env():
        from app import claude_client

        captured: dict = {}

        class _FakeMsg:
            stop_reason = "end_turn"
            content = [MagicMock(type="text", text=(
                "<!-- ===== GAME PREP START ===== -->\n"
                "<!doctype html><html></html>\n"
                "<!-- ===== GAME PREP END ===== -->"
            ))]

        class _FakeClient:
            class messages:
                @staticmethod
                def create(**kwargs):
                    captured.update(kwargs)
                    return _FakeMsg()

        with patch.object(claude_client, "_client", return_value=_FakeClient()), \
             patch.object(claude_client, "load_system_prompt", return_value="SYS"):
            claude_client.generate_gameprep({
                "intake_id": "x",
                "slug": "x",
                "sport": "lacrosse",
                "extras": {"opponent": "Opp"},
            })

        msg = captured["messages"][0]["content"]
        assert "SECTION LAX-G" in msg
        assert "LG1" in msg
        assert "LG-Field" in msg
        # And it must NOT route the model at the water-polo section.
        assert "SECTION WP-G" not in msg


def test_generate_gameprep_sport_waterpolo_user_message_points_to_wp_g():
    """Backward compat: intake without a sport field (or sport=waterpolo)
    still points at Section WP-G — Session 7 behavior unchanged."""
    with _patched_env():
        from app import claude_client

        captured: dict = {}

        class _FakeMsg:
            stop_reason = "end_turn"
            content = [MagicMock(type="text", text=(
                "<!-- ===== GAME PREP START ===== -->\n"
                "<!doctype html><html></html>\n"
                "<!-- ===== GAME PREP END ===== -->"
            ))]

        class _FakeClient:
            class messages:
                @staticmethod
                def create(**kwargs):
                    captured.update(kwargs)
                    return _FakeMsg()

        with patch.object(claude_client, "_client", return_value=_FakeClient()), \
             patch.object(claude_client, "load_system_prompt", return_value="SYS"):
            claude_client.generate_gameprep({
                "intake_id": "x",
                "slug": "x",
                "extras": {"opponent": "Opp"},
                # no sport → defaults to waterpolo
            })

        msg = captured["messages"][0]["content"]
        assert "SECTION WP-G" in msg
        assert "SECTION LAX-G" not in msg


# ---------------------------------------------------------------------------
# 5. Email body — sport=lacrosse uses lacrosse wording
# ---------------------------------------------------------------------------

def test_send_gameprep_email_lacrosse_body_uses_field_terminology():
    """sport=lacrosse → body mentions `to the field` + lacrosse scouting
    categories (goalie, EMD, face-off, clearing). No water-polo leakage
    (5x6, GK, pool, deck)."""
    from app import email_send

    captured: dict = {}

    class _FakeResend:
        @staticmethod
        def send(params):
            captured.update(params)
            return {"id": "resend-lx"}

    with patch.object(email_send, "resend") as mock_resend:
        mock_resend.Emails = _FakeResend
        email_send.send_gameprep_email(
            coach_name="Jamie Rivera",
            coach_email="jamie@example.com",
            opponent="St. Mary's Prep",
            gameprep_url="https://example.invalid/coaches/jr/lacrosse-gameprep-smp.html",
            sport="lacrosse",
        )

    # Subject is unchanged across sports.
    assert captured["subject"] == "CoachPrep — Game Prep vs St. Mary's Prep is ready"

    # Lacrosse-specific wording in both HTML and plain text.
    html = captured["html"]
    text = captured["text"]
    assert "to the field" in html
    assert "to the field" in text
    for term in ("goalie", "EMO", "EMD", "face-off", "clearing"):
        assert term in html, f"HTML missing lacrosse term: {term!r}"
        assert term in text, f"text missing lacrosse term: {term!r}"

    # Water-polo terminology MUST NOT leak into a lacrosse email.
    for banned in ("5x6", " on deck", "pool", "GK tendencies"):
        assert banned not in html, f"HTML leaked waterpolo term: {banned!r}"
        assert banned not in text, f"text leaked waterpolo term: {banned!r}"


def test_send_gameprep_email_waterpolo_default_still_uses_deck_terminology():
    """Backward compat: sport=None (or waterpolo) → "on deck" + 5x6 wording
    exactly as Session 7 set it up."""
    from app import email_send

    captured: dict = {}

    class _FakeResend:
        @staticmethod
        def send(params):
            captured.update(params)
            return {"id": "resend-wp"}

    with patch.object(email_send, "resend") as mock_resend:
        mock_resend.Emails = _FakeResend
        email_send.send_gameprep_email(
            coach_name="Jamie Rivera",
            coach_email="jamie@example.com",
            opponent="St. Mary's Prep",
            gameprep_url="https://example.invalid/coaches/jr/gameprep-smp.html",
            # no sport → defaults to waterpolo wording
        )

    html = captured["html"]
    text = captured["text"]
    # Water-polo surface and vocabulary land in the body.
    assert "on deck" in html
    assert "on deck" in text
    for term in ("GK tendencies", "5x6 shape"):
        assert term in html, f"HTML missing waterpolo term: {term!r}"


# ---------------------------------------------------------------------------
# 6. main.py routing
# ---------------------------------------------------------------------------

def test_main_lacrosse_dispatcher_branches_on_form_type():
    """Regression guard: main.py must import `run_lacrosse_gameprep_
    pipeline` and dispatch lacrosse intakes with `form_type == "gameprep"`
    to it (same pattern the waterpolo dispatcher already uses). Lacrosse
    weekly intakes must still route to `run_lacrosse_pipeline`. There
    must be NO dedicated `/webhook/formspree/lacrosse-gameprep` route and
    NO reference to a lacrosse-gameprep signing secret — lacrosse uses
    one Formspree form for both weekly and game prep, keyed by the
    single `FORMSPREE_SECRET_LACROSSE` secret."""
    main_src = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "from .gameprep import run_gameprep_pipeline, run_lacrosse_gameprep_pipeline" in main_src, (
        "main.py must import run_lacrosse_gameprep_pipeline"
    )
    # The lacrosse dispatch path dispatches gameprep intakes to the
    # lacrosse gameprep pipeline — this is the substance of the change.
    assert "background.add_task(run_lacrosse_gameprep_pipeline, intake)" in main_src, (
        "main.py must dispatch lacrosse gameprep intakes to "
        "run_lacrosse_gameprep_pipeline"
    )
    # Weekly lacrosse dispatch must still be present.
    assert "background.add_task(run_lacrosse_pipeline, intake)" in main_src, (
        "main.py must still dispatch lacrosse weekly intakes to "
        "run_lacrosse_pipeline"
    )
    # The dedicated sub-route / sub-secret must be GONE — lacrosse uses
    # the same dispatcher-on-form_type shape as water polo.
    assert "/webhook/formspree/lacrosse-gameprep" not in main_src, (
        "main.py must NOT register a dedicated /webhook/formspree/"
        "lacrosse-gameprep route — the /webhook/formspree/lacrosse route "
        "branches on form_type instead"
    )
    assert "formspree_secret_lacrosse_gameprep" not in main_src, (
        "main.py must NOT reference a dedicated lacrosse gameprep "
        "signing secret — lacrosse uses FORMSPREE_SECRET_LACROSSE for "
        "every form type (weekly + gameprep)"
    )
    # Verify both waterpolo and lacrosse branch on form_type == "gameprep".
    assert main_src.count('form_type == "gameprep"') >= 2, (
        "main.py must branch on form_type == \"gameprep\" in both the "
        "waterpolo and lacrosse dispatch paths"
    )


def test_webhook_lacrosse_routes_gameprep_to_lacrosse_gameprep_pipeline():
    """End-to-end routing via the FastAPI TestClient: a lacrosse intake
    with `formType: gameprep` submitted to /webhook/formspree/lacrosse
    must dispatch to `run_lacrosse_gameprep_pipeline` (and a weekly
    lacrosse intake with no formType must still dispatch to
    `run_lacrosse_pipeline`)."""
    import hashlib
    import hmac
    import json
    import time

    # Use a separate env so the HMAC secret is deterministic for this test,
    # and keep the cache isolated.
    with _patched_env(FORMSPREE_SECRET_LACROSSE="test-lacrosse-secret"):
        from fastapi.testclient import TestClient
        from app import main as main_mod

        client = TestClient(main_mod.app)

        def _sign(raw: bytes) -> str:
            ts = int(time.time())
            digest = hmac.new(
                b"test-lacrosse-secret",
                str(ts).encode("ascii") + b"." + raw,
                hashlib.sha256,
            ).hexdigest()
            return f"t={ts},v1={digest}"

        # Case A — formType=gameprep → run_lacrosse_gameprep_pipeline.
        body_gp = json.dumps({
            "sport": "lacrosse",
            "formType": "gameprep",
            "name": "Jamie Rivera",
            "email": "jamie@example.com",
            "opponent": "St. Mary's Prep",
        }).encode("utf-8")
        with patch.object(main_mod, "run_lacrosse_gameprep_pipeline") as gp, \
             patch.object(main_mod, "run_lacrosse_pipeline") as wk:
            resp = client.post(
                "/webhook/formspree/lacrosse",
                data=body_gp,
                headers={
                    "Content-Type": "application/json",
                    "Formspree-Signature": _sign(body_gp),
                },
            )
        assert resp.status_code == 202, resp.text
        assert resp.json()["form_type"] == "gameprep"
        gp.assert_called_once()
        wk.assert_not_called()
        scheduled = gp.call_args.args[0]
        assert scheduled["coach_name"] == "Jamie Rivera"
        assert scheduled["form_type"] == "gameprep"
        assert scheduled["sport"] == "lacrosse"

        # Case B — no formType → run_lacrosse_pipeline (weekly).
        body_wk = json.dumps({
            "sport": "lacrosse",
            "name": "Jamie Rivera",
            "email": "jamie@example.com",
            "level": "U14",
        }).encode("utf-8")
        with patch.object(main_mod, "run_lacrosse_gameprep_pipeline") as gp, \
             patch.object(main_mod, "run_lacrosse_pipeline") as wk:
            resp = client.post(
                "/webhook/formspree/lacrosse",
                data=body_wk,
                headers={
                    "Content-Type": "application/json",
                    "Formspree-Signature": _sign(body_wk),
                },
            )
        assert resp.status_code == 202, resp.text
        assert resp.json()["form_type"] == "week"
        wk.assert_called_once()
        gp.assert_not_called()


def test_dedicated_lacrosse_gameprep_route_is_removed():
    """The /webhook/formspree/lacrosse-gameprep route must 404 — if it
    ever comes back, Formspree traffic would split between two URLs and
    we'd have two signing secrets to juggle."""
    with _patched_env():
        from fastapi.testclient import TestClient
        from app.main import app

        client = TestClient(app)
        resp = client.post(
            "/webhook/formspree/lacrosse-gameprep",
            json={"name": "X"},
        )
        # FastAPI returns 404 for unregistered POST paths.
        assert resp.status_code == 404


def test_config_has_no_lacrosse_gameprep_secret_field():
    """Settings must NOT expose formspree_secret_lacrosse_gameprep —
    lacrosse uses one secret per sport now, not one per pipeline."""
    with _patched_env():
        from app.config import get_settings

        s = get_settings()
        assert not hasattr(s, "formspree_secret_lacrosse_gameprep")


# ---------------------------------------------------------------------------
# 7. Master system prompt anchors
# ---------------------------------------------------------------------------

def test_master_prompt_contains_lacrosse_gameprep_section():
    prompt_path = ROOT / "firstwhistle_master_system_prompt.md"
    assert prompt_path.exists(), f"missing master prompt at {prompt_path}"
    text = prompt_path.read_text(encoding="utf-8")

    # Routing
    assert "form_type" in text
    assert '`form_type == "gameprep"` AND the intake is lacrosse' in text
    # Section header
    assert "SECTION LAX-G — LACROSSE GAME PREP" in text
    # All ten mandatory sections' Part headings
    for marker in (
        "PART LG1 — Parse the Game-Prep Intake (Lacrosse)",
        "PART LG2 — Section 1: Game Header",
        "PART LG3 — Section 2: Their System",
        "PART LG4 — Section 3: Goalie Tendencies",
        "PART LG5 — Section 4: Top Threats",
        "PART LG6 — Section 5: Your Defensive Assignment",
        "PART LG7 — Section 6: Your Offensive Answer",
        "PART LG8 — Section 7: EMD Game Plan",
        "PART LG9 — Section 8: Timeout Scripts",
        "PART LG10 — Section 9: Halftime Adjustment Triggers",
        "PART LG-Field — Section 10: Field Notes",
    ):
        assert marker in text, f"master prompt missing marker: {marker!r}"
    # Lacrosse-specific terminology tokens that the section must use
    for term in ("EMO", "EMD", "goalie", "ground ball", "clearing", "face-off"):
        assert term in text, f"master prompt missing lacrosse term: {term!r}"
    # The naming-conventions section must describe the lacrosse file path.
    assert "lacrosse-gameprep-<opponent-slug>.html" in text
    # Part 10.3 output contract still intact (shared with waterpolo)
    assert "<!-- ===== GAME PREP START ===== -->" in text
    assert "<!-- ===== GAME PREP END ===== -->" in text


# ---------------------------------------------------------------------------
# 8. End-to-end intake → dispatch wiring (no network)
# ---------------------------------------------------------------------------

def test_lacrosse_gameprep_intake_flows_through_intake_parser():
    """A realistic Formspree lacrosse game-prep intake parses cleanly."""
    from app.intake import parse_formspree_payload

    body = {
        "form": "abc123",
        "submission": {
            "_date": "2026-05-04T15:00:00+00:00",
            "sport": "lacrosse",
            "formType": "gameprep",
            "name": "Jamie Rivera",
            "email": "jamie@example.com",
            "program": "Conestoga Youth Lacrosse",
            "coachCode": "JR2026",
            "gender": "boys",
            "opponent": "St. Mary's Prep",
            "gameDate": "2026-05-04",
            "homeAway": "away",
            "gameContext": "league",
            "rematch": "true",
            "fieldSurface": "turf",
            "fieldSize": "full",
            "fieldNotes": "east-facing; 4pm start, sun in goalie's eyes",
            "theirDefense": "slide-heavy",
            "theirOffense": "invert",
            "theirEMODanger": "high",
            "theirGoalie": "shot-stopper",
            "theirFaceoff": "dominant",
            "threat1Name": "#7 Smith",
            "threat1Position": "attack",
            "threat1Why": "crease feeder, dangerous inside",
            "biggestConcern": "their face-off dominance",
            "oneAdjustment": "send wings into denial mode",
            "confidenceLevel": "moderate",
            "extraNotes": "lost 10-6 last meeting",
        },
    }
    intake = parse_formspree_payload(body)
    assert intake["coach_name"] == "Jamie Rivera"
    assert intake["coach_email"] == "jamie@example.com"
    assert intake["team_name"] == "Conestoga Youth Lacrosse"  # `program` alias
    assert intake["coach_code"] == "JR2026"
    assert intake["form_type"] == "gameprep"
    assert intake["slug"] == "jamie-rivera"
    # Lacrosse gameprep fields live in extras.
    assert intake["extras"]["opponent"] == "St. Mary's Prep"
    assert intake["extras"]["gameDate"] == "2026-05-04"
    assert intake["extras"]["fieldSurface"] == "turf"
    assert intake["extras"]["theirEMODanger"] == "high"
    assert intake["extras"]["theirFaceoff"] == "dominant"
    assert intake["extras"]["sport"] == "lacrosse"


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
