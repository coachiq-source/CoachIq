"""Tests for the water-polo game-prep pipeline (Session 7).

Covers:
  1. Intake parser recognizes `form_type` (formType / formtype / form_type).
  2. Intake parser preserves gameprep-specific fields in `extras`.
  3. `parse_gameprep` extracts a single HTML doc from the GAME PREP markers.
  4. `parse_gameprep` falls back to a fenced code block on marker drift.
  5. `parse_gameprep` rejects empty or non-HTML input.
  6. `deploy_gameprep` computes the opponent slug and writes exactly one file
     at `coaches/<slug>/gameprep-<opponent-slug>.html`.
  7. `run_gameprep_pipeline` orchestrates generate → parse → deploy → email
     with the correct arguments and stamps `sport=waterpolo` + `form_type=gameprep`.
  8. `run_gameprep_pipeline` catches stage failures and notifies ops.
  9. `send_gameprep_email` builds the correct subject + body.
 10. `main.py` waterpolo dispatcher branches on `form_type == "gameprep"`.
 11. Master system prompt contains the required Part G / Part 10.3 anchors.
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


# ---------------------------------------------------------------------------
# 1. Intake parsing
# ---------------------------------------------------------------------------

def test_intake_form_type_canonical_alias_camelcase():
    """`formType` camelCase from the gameprep intake form lands on the
    canonical `form_type` key."""
    from app.intake import parse_formspree_payload

    body = {
        "name": "Jamie Rivera",
        "email": "jamie@example.com",
        "formType": "gameprep",
        "sport": "waterpolo",
        "opponent": "St. Mary's Prep",
    }
    intake = parse_formspree_payload(body)
    assert intake["form_type"] == "gameprep"


def test_intake_form_type_snake_case_alias():
    from app.intake import parse_formspree_payload

    body = {
        "name": "Jamie Rivera",
        "email": "jamie@example.com",
        "form_type": "gameprep",
    }
    intake = parse_formspree_payload(body)
    assert intake["form_type"] == "gameprep"


def test_intake_form_type_missing_is_empty_string():
    """Weekly intakes don't set form_type — the field should be present but
    empty so main.py can check it safely."""
    from app.intake import parse_formspree_payload

    body = {"name": "Jamie Rivera", "email": "jamie@example.com"}
    intake = parse_formspree_payload(body)
    assert intake.get("form_type", "") == ""


def test_intake_form_key_is_not_mistaken_for_form_type():
    """Formspree posts `{"form": "myklwjnp", "submission": {...}}`. The bare
    `form` key (a Formspree form id) must NOT be consumed as form_type."""
    from app.intake import parse_formspree_payload

    body = {
        "form": "myklwjnp",
        "submission": {
            "name": "Jamie Rivera",
            "email": "jamie@example.com",
            "formType": "gameprep",
        },
    }
    intake = parse_formspree_payload(body)
    # `form` is the form id — the unwrap logic consumes it as a wrapper OR
    # leaves it alone; either way it must not become form_type.
    assert intake["form_type"] == "gameprep"
    assert intake["coach_name"] == "Jamie Rivera"


def test_intake_preserves_gameprep_fields_in_extras():
    """Gameprep-specific fields aren't canonical aliases — they land in
    extras so the model can read them via the intake JSON."""
    from app.intake import parse_formspree_payload

    body = {
        "name": "Jamie Rivera",
        "email": "jamie@example.com",
        "formType": "gameprep",
        "opponent": "St. Mary's Prep",
        "gameDate": "2026-05-04",
        "homeAway": "away",
        "gameContext": "league",
        "poolDepth": "shallow",
        "poolLength": "25y",
        "theirDefense": "press",
        "theirOffense": "counter",
        "theirPP6Danger": "high",
        "theirGK": "shot-stopper",
        "threat1Name": "#7 Smith",
        "threat1Position": "hole-set",
        "threat1Why": "dominant post-up, uses body well",
        "biggestConcern": "their hole-set",
        "oneAdjustment": "front-front the hole-set",
        "confidenceLevel": "moderate",
        "extraNotes": "we lost to them 8-6 last time",
    }
    intake = parse_formspree_payload(body)
    extras = intake["extras"]
    assert extras["opponent"] == "St. Mary's Prep"
    assert extras["gameDate"] == "2026-05-04"
    assert extras["homeAway"] == "away"
    assert extras["theirPP6Danger"] == "high"
    assert extras["threat1Name"] == "#7 Smith"
    assert extras["biggestConcern"] == "their hole-set"
    assert extras["oneAdjustment"].startswith("front-front")


# ---------------------------------------------------------------------------
# 2. parse_gameprep
# ---------------------------------------------------------------------------

def test_parse_gameprep_with_markers():
    from app.parser import parse_gameprep

    response = (
        "<!-- ===== GAME PREP START ===== -->\n"
        "<!DOCTYPE html><html><head><title>CoachPrep — Game Prep vs St. Mary's</title>"
        "</head><body><h1>Game Prep vs St. Mary's</h1></body></html>\n"
        "<!-- ===== GAME PREP END ===== -->"
    )
    html = parse_gameprep(response)
    assert html.lstrip().lower().startswith("<!doctype html>")
    assert "Game Prep vs St. Mary's" in html


def test_parse_gameprep_marker_survives_preamble():
    """Chat-style drift shouldn't break the extractor."""
    from app.parser import parse_gameprep

    response = (
        "Sure, here's the game prep document.\n\n"
        "<!-- ===== GAME PREP START ===== -->\n"
        "<!DOCTYPE html><html><body><h1>Game Prep</h1></body></html>\n"
        "<!-- ===== GAME PREP END ===== -->\n\n"
        "Let me know if you need adjustments!"
    )
    html = parse_gameprep(response)
    assert "<h1>Game Prep</h1>" in html
    assert "Sure," not in html
    assert "Let me know" not in html


def test_parse_gameprep_fenced_fallback():
    """Model drifts to a fenced code block — extractor should still recover."""
    from app.parser import parse_gameprep

    response = (
        "```html\n"
        "<!DOCTYPE html><html><body><h1>Game Prep</h1></body></html>\n"
        "```"
    )
    html = parse_gameprep(response)
    assert "<h1>Game Prep</h1>" in html


def test_parse_gameprep_empty_raises():
    from app.parser import parse_gameprep, PlanParseError

    try:
        parse_gameprep("")
    except PlanParseError:
        pass
    else:
        raise AssertionError("empty input must raise PlanParseError")


def test_parse_gameprep_non_html_raises():
    from app.parser import parse_gameprep, PlanParseError

    response = (
        "<!-- ===== GAME PREP START ===== -->\n"
        "just some plaintext, no HTML here\n"
        "<!-- ===== GAME PREP END ===== -->"
    )
    try:
        parse_gameprep(response)
    except PlanParseError:
        pass
    else:
        raise AssertionError("non-HTML content must raise PlanParseError")


# ---------------------------------------------------------------------------
# 3. deploy_gameprep
# ---------------------------------------------------------------------------

def test_deploy_gameprep_path_uses_opponent_slug():
    """File path must be coaches/<slug>/gameprep-<opponent-slug>.html, with
    the opponent slug generated by the shared `slugify` helper."""
    from app import github_deploy
    from app.config import slugify

    # Match whatever slugify produces — the exact rule ("." → "-", "'" → "-")
    # is a product of the shared helper; tests shouldn't second-guess it.
    expected_opp_slug = slugify("St. Mary's Prep")
    expected_path = f"coaches/jamie-rivera/gameprep-{expected_opp_slug}.html"

    captured = {}

    def fake_put(client, path, content, message, sha):
        captured["path"] = path
        captured["content"] = content
        captured["message"] = message
        return {"commit": {"sha": "cafe1234"}}

    with patch.object(github_deploy, "_get_existing_sha", return_value=None), \
         patch.object(github_deploy, "_put_file", side_effect=fake_put), \
         patch.object(github_deploy, "httpx") as mock_httpx:
        mock_httpx.Client.return_value.__enter__.return_value = MagicMock()
        result = github_deploy.deploy_gameprep(
            slug="jamie-rivera",
            opponent="St. Mary's Prep",
            gameprep_html="<!doctype html><html></html>",
            coach_name="Jamie Rivera",
            intake_id="abc123",
        )

    assert captured["path"] == expected_path
    assert result.opponent_slug == expected_opp_slug
    assert result.path == expected_path
    assert result.url.endswith("/" + expected_path)
    assert result.commit_sha == "cafe1234"


def test_deploy_gameprep_falls_back_to_opponent_slug_fallback():
    """Empty opponent → slug falls back to 'opponent'."""
    from app import github_deploy

    def fake_put(client, path, content, message, sha):
        return {"commit": {"sha": "beef9999"}}

    with patch.object(github_deploy, "_get_existing_sha", return_value=None), \
         patch.object(github_deploy, "_put_file", side_effect=fake_put), \
         patch.object(github_deploy, "httpx") as mock_httpx:
        mock_httpx.Client.return_value.__enter__.return_value = MagicMock()
        result = github_deploy.deploy_gameprep(
            slug="jamie-rivera",
            opponent="",  # missing
            gameprep_html="<!doctype html><html></html>",
            coach_name="Jamie Rivera",
            intake_id="abc",
        )
    assert result.opponent_slug == "opponent"
    assert result.path == "coaches/jamie-rivera/gameprep-opponent.html"


# ---------------------------------------------------------------------------
# 4. run_gameprep_pipeline orchestration
# ---------------------------------------------------------------------------

def test_run_gameprep_pipeline_happy_path():
    """End-to-end (all I/O stubbed): pipeline must call generate → parse →
    deploy → email and return the deploy URL in the result dict."""
    from app import gameprep
    from app.github_deploy import GamePrepDeployResult
    from app.email_send import EmailResult

    claude_response = (
        "<!-- ===== GAME PREP START ===== -->\n"
        "<!DOCTYPE html><html><body><h1>Game Prep vs Opp</h1></body></html>\n"
        "<!-- ===== GAME PREP END ===== -->"
    )
    fake_deploy = GamePrepDeployResult(
        url="https://example.invalid/coaches/jr/gameprep-opp.html",
        commit_sha="abc1234",
        path="coaches/jr/gameprep-opp.html",
        opponent_slug="opp",
    )

    seen_intake: dict = {}

    def fake_generate(intake):
        seen_intake.update(dict(intake))
        return claude_response

    with patch.object(gameprep, "generate_gameprep", side_effect=fake_generate), \
         patch.object(gameprep, "deploy_gameprep", return_value=fake_deploy) as mock_deploy, \
         patch.object(gameprep, "send_gameprep_email",
                      return_value=EmailResult(message_id="msg-1", to="jamie@example.com")) as mock_email:
        intake = {
            "intake_id": "i-abc",
            "slug": "jr",
            "coach_name": "Jamie Rivera",
            "coach_email": "jamie@example.com",
            "form_type": "gameprep",
            "opponent": "Opp",
        }
        result = gameprep.run_gameprep_pipeline(intake)

    assert result["ok"] is True
    assert result["gameprep_url"].endswith("gameprep-opp.html")
    assert result["opponent"] == "Opp"
    assert result["opponent_slug"] == "opp"
    assert result["email_message_id"] == "msg-1"

    # sport + form_type must be stamped defensively, even if the caller forgot.
    assert seen_intake["sport"] == "waterpolo"
    assert seen_intake["form_type"] == "gameprep"

    # deploy receives the opponent name (not the slug — that's the deploy's job).
    assert mock_deploy.call_args.kwargs["opponent"] == "Opp"
    # email receives the URL from deploy.
    assert mock_email.call_args.kwargs["gameprep_url"] == fake_deploy.url
    assert mock_email.call_args.kwargs["opponent"] == "Opp"


def test_run_gameprep_pipeline_stamps_sport_and_form_type_defensively():
    """If a caller (replay script, test harness) forgets sport/form_type,
    the pipeline must stamp them before passing to Claude."""
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

        intake_no_sport = {
            "intake_id": "i1",
            "slug": "x",
            "coach_name": "X Y",
            "coach_email": "x@y.com",
            "opponent": "Opp",
            # no sport, no form_type
        }
        gameprep.run_gameprep_pipeline(intake_no_sport)

    assert captured["sport"] == "waterpolo"
    assert captured["form_type"] == "gameprep"
    # Caller's dict must not be mutated.
    assert "sport" not in intake_no_sport
    assert "form_type" not in intake_no_sport


def test_run_gameprep_pipeline_reads_opponent_from_extras():
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
            url="u", commit_sha="s", path="p", opponent_slug="opp"
        )
        mock_email.return_value.message_id = "m"

        gameprep.run_gameprep_pipeline({
            "intake_id": "i1",
            "slug": "x",
            "coach_name": "X Y",
            "coach_email": "x@y.com",
            "extras": {"opponent": "Extras Opp"},
        })

    assert mock_deploy.call_args.kwargs["opponent"] == "Extras Opp"
    assert mock_email.call_args.kwargs["opponent"] == "Extras Opp"


def test_run_gameprep_pipeline_failure_notifies_ops():
    """Stage failure → result has ok=False, stage_failed set, ops email sent."""
    from app import gameprep
    from app.claude_client import ClaudeGenerationError

    with patch.object(gameprep, "generate_gameprep",
                      side_effect=ClaudeGenerationError("boom")), \
         patch.object(gameprep, "send_ops_failure_email") as mock_ops:
        result = gameprep.run_gameprep_pipeline({
            "intake_id": "i-fail",
            "slug": "x",
            "coach_name": "X",
            "coach_email": "x@y.com",
            "opponent": "Opp",
        })

    assert result["ok"] is False
    assert result["stage_failed"] == "claude"
    assert "boom" in result["error"]
    mock_ops.assert_called_once()
    ops_kwargs = mock_ops.call_args.kwargs
    assert ops_kwargs["stage"] == "gameprep:claude"
    assert ops_kwargs["intake_id"] == "i-fail"


# ---------------------------------------------------------------------------
# 5. Email subject + body
# ---------------------------------------------------------------------------

def test_send_gameprep_email_subject_includes_opponent():
    """Subject must match: CoachPrep — Game Prep vs <opponent> is ready."""
    from app import email_send

    captured: dict = {}

    class _FakeResend:
        @staticmethod
        def send(params):
            captured.update(params)
            return {"id": "resend-abc"}

    with patch.object(email_send, "resend") as mock_resend:
        mock_resend.Emails = _FakeResend
        email_send.send_gameprep_email(
            coach_name="Jamie Rivera",
            coach_email="jamie@example.com",
            opponent="St. Mary's Prep",
            gameprep_url="https://example.invalid/coaches/jr/gameprep-smp.html",
        )

    assert captured["subject"] == "CoachPrep — Game Prep vs St. Mary's Prep is ready"
    assert "jamie@example.com" in captured["to"]
    # Body (HTML) contains the single "View game prep package" link and the URL.
    assert "View game prep package" in captured["html"]
    assert "https://example.invalid/coaches/jr/gameprep-smp.html" in captured["html"]
    # Sign-off.
    assert "— CoachPrep" in captured["html"]
    assert "— CoachPrep" in captured["text"]


def test_send_gameprep_email_handles_blank_opponent():
    """Blank opponent shouldn't crash; subject falls back to 'Opponent'."""
    from app import email_send

    captured: dict = {}

    class _FakeResend:
        @staticmethod
        def send(params):
            captured.update(params)
            return {"id": "r-blank"}

    with patch.object(email_send, "resend") as mock_resend:
        mock_resend.Emails = _FakeResend
        email_send.send_gameprep_email(
            coach_name="Jamie Rivera",
            coach_email="jamie@example.com",
            opponent="   ",
            gameprep_url="https://example.invalid/x.html",
        )

    assert captured["subject"] == "CoachPrep — Game Prep vs Opponent is ready"


# ---------------------------------------------------------------------------
# 6. main.py routing
# ---------------------------------------------------------------------------

def test_main_imports_gameprep_pipeline():
    """Regression guard: main.py must import run_gameprep_pipeline AND branch
    the waterpolo dispatcher on form_type == 'gameprep'."""
    main_src = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "from .gameprep import run_gameprep_pipeline" in main_src, (
        "main.py must import run_gameprep_pipeline"
    )
    assert "background.add_task(run_gameprep_pipeline, intake)" in main_src, (
        "main.py must dispatch gameprep intakes to run_gameprep_pipeline"
    )
    # The branch check — form_type lookup must live next to the waterpolo dispatch.
    assert 'form_type == "gameprep"' in main_src, (
        "main.py must branch on form_type == 'gameprep'"
    )


# ---------------------------------------------------------------------------
# 7. Master system prompt anchors
# ---------------------------------------------------------------------------

def test_master_prompt_contains_gameprep_section():
    prompt_path = ROOT / "firstwhistle_master_system_prompt.md"
    assert prompt_path.exists(), f"missing master prompt at {prompt_path}"
    text = prompt_path.read_text(encoding="utf-8")

    # Routing
    assert "form_type" in text
    assert '`form_type == "gameprep"`' in text or "form_type == \"gameprep\"" in text
    # Section header
    assert "SECTION WP-G — WATER POLO GAME PREP" in text
    # All ten mandatory sections' Part headings
    for marker in (
        "PART G1 — Parse the Game-Prep Intake",
        "PART G2 — Section 1: Game Header",
        "PART G3 — Section 2: Their System",
        "PART G4 — Section 3: GK Tendencies",
        "PART G5 — Section 4: Top Threats",
        "PART G6 — Section 5: Your Defensive Assignment",
        "PART G7 — Section 6: Your Offensive Answer",
        "PART G8 — Section 7: 5x6 Game Plan",
        "PART G9 — Section 8: Timeout Scripts",
        "PART G10 — Section 9: Halftime Adjustment Triggers",
        "PART G-Pool — Section 10: Pool Notes",
    ):
        assert marker in text, f"master prompt missing marker: {marker!r}"
    # Output contract
    assert "Part 10.3 — Game-Prep Output Format Override" in text
    assert "<!-- ===== GAME PREP START ===== -->" in text
    assert "<!-- ===== GAME PREP END ===== -->" in text


# ---------------------------------------------------------------------------
# 8. End-to-end intake → dispatch wiring (no network)
# ---------------------------------------------------------------------------

def test_gameprep_intake_flows_through_intake_parser():
    """A realistic Formspree game-prep intake parses cleanly and produces the
    fields the pipeline needs: form_type='gameprep' canonical, opponent in
    extras, coach_code preserved if supplied."""
    from app.intake import parse_formspree_payload

    body = {
        "form": "myklwjnp",
        "submission": {
            "_date": "2026-04-24T15:00:00+00:00",
            "sport": "waterpolo",
            "formType": "gameprep",
            "name": "Jamie Rivera",
            "email": "jamie@example.com",
            "program": "Riverside Aquatics",
            "coachCode": "JR2026",
            "opponent": "St. Mary's Prep",
            "gameDate": "2026-05-04",
            "homeAway": "away",
            "gameContext": "league",
            "rematch": "true",
            "poolDepth": "shallow",
            "poolLength": "25y",
            "poolNotes": "sun at 4pm, east-facing",
            "theirDefense": "press",
            "theirOffense": "counter",
            "theirPP6Danger": "high",
            "theirGK": "shot-stopper",
            "threat1Name": "#7 Smith",
            "threat1Position": "hole-set",
            "threat1Why": "dominant post-up",
            "biggestConcern": "their hole-set",
            "oneAdjustment": "front-front the hole-set",
            "confidenceLevel": "moderate",
            "extraNotes": "lost 8-6 last time",
        },
    }
    intake = parse_formspree_payload(body)
    assert intake["coach_name"] == "Jamie Rivera"
    assert intake["coach_email"] == "jamie@example.com"
    assert intake["team_name"] == "Riverside Aquatics"  # `program` alias
    assert intake["coach_code"] == "JR2026"
    assert intake["form_type"] == "gameprep"
    assert intake["slug"] == "jamie-rivera"
    # Gameprep-specific fields live in extras for the model to read.
    assert intake["extras"]["opponent"] == "St. Mary's Prep"
    assert intake["extras"]["gameDate"] == "2026-05-04"
    assert intake["extras"]["theirPP6Danger"] == "high"
    assert intake["extras"]["sport"] == "waterpolo"


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
