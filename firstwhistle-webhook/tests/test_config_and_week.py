"""Smoke tests for the Railway-aligned config shape + week-number discovery.

Runs without hitting real APIs — we patch env vars and the GitHub HTTP client.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _clear_config_cache():
    """Drop the lru_cache on get_settings so each test sees fresh env."""
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


def test_config_parses_combined_github_repo():
    with patch.dict(os.environ, _five_var_env(), clear=True):
        _clear_config_cache()
        from app.config import get_settings

        s = get_settings()
        assert s.github_owner == "coachiq-source"
        assert s.github_repo == "CoachIq"
        assert s.github_branch == "main"
        assert s.public_base_url == "https://coachiq-source.github.io/CoachIq"
        assert s.email_from == "johnmaxwell.kelly@gmail.com"
        assert s.claude_model == "claude-sonnet-4-20250514"
        assert s.webhook_secret == ""  # optional, unset
        assert s.ops_notify_email == "johnmaxwell.kelly@gmail.com"


def test_config_rejects_malformed_github_repo():
    with patch.dict(os.environ, _five_var_env(GITHUB_REPO="no-slash-here"), clear=True):
        _clear_config_cache()
        from app.config import get_settings

        try:
            get_settings()
        except RuntimeError as exc:
            assert "owner/repo" in str(exc)
        else:
            raise AssertionError("should have raised on malformed GITHUB_REPO")


def test_config_respects_public_base_url_override():
    with patch.dict(
        os.environ,
        _five_var_env(PUBLIC_BASE_URL="https://plans.firstwhistle.coach/"),
        clear=True,
    ):
        _clear_config_cache()
        from app.config import get_settings

        s = get_settings()
        # trailing slash stripped
        assert s.public_base_url == "https://plans.firstwhistle.coach"


def test_config_missing_required_var_fails_fast():
    env = _five_var_env()
    del env["ANTHROPIC_API_KEY"]
    with patch.dict(os.environ, env, clear=True):
        _clear_config_cache()
        from app.config import get_settings

        try:
            get_settings()
        except RuntimeError as exc:
            assert "ANTHROPIC_API_KEY" in str(exc)
        else:
            raise AssertionError("should have raised on missing required var")


def _fake_client_with_entries(entries: list[dict] | int):
    """Build a MagicMock httpx client that returns `entries` from GET.

    If `entries` is an int, treat it as an HTTP status code (e.g. 404).
    """
    client = MagicMock()

    def fake_get(url, params=None, headers=None):
        resp = MagicMock()
        if isinstance(entries, int):
            resp.status_code = entries
            resp.json.return_value = {}
            resp.text = ""
        else:
            resp.status_code = 200
            resp.json.return_value = entries
            resp.text = ""
        return resp

    client.get.side_effect = fake_get
    return client


def test_next_week_number_empty_directory():
    with patch.dict(os.environ, _five_var_env(), clear=True):
        _clear_config_cache()
        from app import github_deploy

        client = _fake_client_with_entries(404)  # dir doesn't exist yet
        n = github_deploy._next_week_number(client, "magnus-sims")
        assert n == 1, f"expected week 1 for new coach, got {n}"


def test_next_week_number_increments_past_highest():
    entries = [
        {"name": "week1-plan.html"},
        {"name": "week1-deck.html"},
        {"name": "week2-plan.html"},
        {"name": "week2-deck.html"},
        {"name": "week3-plan.html"},
        {"name": "week3-deck.html"},
        {"name": "readme.md"},           # ignored
        {"name": "week99-stuff.html"},   # ignored (not plan/deck)
    ]
    with patch.dict(os.environ, _five_var_env(), clear=True):
        _clear_config_cache()
        from app import github_deploy

        client = _fake_client_with_entries(entries)
        n = github_deploy._next_week_number(client, "magnus-sims")
        assert n == 4, f"expected week 4, got {n}"


def test_next_week_number_only_deck_exists():
    # Edge case: plan write failed but deck landed — we still advance past it.
    entries = [
        {"name": "week1-deck.html"},
    ]
    with patch.dict(os.environ, _five_var_env(), clear=True):
        _clear_config_cache()
        from app import github_deploy

        client = _fake_client_with_entries(entries)
        n = github_deploy._next_week_number(client, "magnus-sims")
        assert n == 2, f"expected week 2, got {n}"


def test_slug_is_coach_name_only():
    """Spec: 'Magnus Sims' -> 'magnus-sims' (team name MUST NOT be in the slug)."""
    with patch.dict(os.environ, _five_var_env(), clear=True):
        _clear_config_cache()
        from app.intake import parse_formspree_payload

        intake = parse_formspree_payload({
            "name": "Magnus Sims",
            "email": "magnus@example.com",
            "team": "Riverside 14U Girls",  # MUST NOT appear in slug
        })
        assert intake["slug"] == "magnus-sims", f"got slug={intake['slug']!r}"


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
