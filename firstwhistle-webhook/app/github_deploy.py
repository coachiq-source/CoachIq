"""Commit the two generated HTML files to GitHub via the Contents API.

Layout written on every deploy:

    coaches/<slug>/week<n>-plan.html
    coaches/<slug>/week<n>-deck.html

The week number is discovered at deploy time by listing the coach's directory
and finding the highest existing `week<n>-plan.html` (fallback: `week<n>-deck.html`).
The new deploy is that max + 1. If the directory doesn't exist yet, we start
at week 1.
"""
from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass
from typing import Optional

import httpx

from .config import get_settings

log = logging.getLogger("firstwhistle.github")

GITHUB_API = "https://api.github.com"

# Matches e.g. "week3-plan.html" or "week12-deck.html"
_WEEK_FILE_RE = re.compile(
    r"^week(?P<n>\d+)-(?P<kind>plan|deck)\.html$",
    re.IGNORECASE,
)


class GitHubDeployError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeployResult:
    plan_url: str
    deck_url: str
    commit_sha: str
    plan_path: str
    deck_path: str
    week_number: int


def _headers() -> dict[str, str]:
    s = get_settings()
    return {
        "Authorization": f"Bearer {s.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "firstwhistle-webhook/0.1",
    }


def _contents_url(path: str) -> str:
    s = get_settings()
    return f"{GITHUB_API}/repos/{s.github_owner}/{s.github_repo}/contents/{path}"


def _get_existing_sha(client: httpx.Client, path: str) -> Optional[str]:
    s = get_settings()
    r = client.get(_contents_url(path), params={"ref": s.github_branch}, headers=_headers())
    if r.status_code == 200:
        data = r.json()
        if isinstance(data, dict) and "sha" in data:
            return data["sha"]
    if r.status_code == 404:
        return None
    raise GitHubDeployError(
        f"GET {_contents_url(path)} failed: {r.status_code} {r.text[:300]}"
    )


def _list_coach_dir(client: httpx.Client, slug: str) -> list[dict]:
    """Return the Contents API listing for coaches/<slug>, or [] if missing."""
    s = get_settings()
    r = client.get(
        _contents_url(f"coaches/{slug}"),
        params={"ref": s.github_branch},
        headers=_headers(),
    )
    if r.status_code == 404:
        return []
    if r.status_code != 200:
        raise GitHubDeployError(
            f"GET coaches/{slug} failed: {r.status_code} {r.text[:300]}"
        )
    data = r.json()
    if not isinstance(data, list):
        # GitHub returns a dict if the path is a file, not a directory.
        return []
    return data


def _next_week_number(client: httpx.Client, slug: str) -> int:
    """Find the highest existing week number in coaches/<slug>/ and add 1.

    Default to 1 if the directory doesn't exist yet or contains no week<n> files.
    """
    entries = _list_coach_dir(client, slug)
    highest = 0
    for entry in entries:
        name = entry.get("name", "")
        m = _WEEK_FILE_RE.match(name)
        if not m:
            continue
        try:
            n = int(m.group("n"))
        except ValueError:
            continue
        if n > highest:
            highest = n
    return highest + 1 if highest >= 1 else 1


def _put_file(
    client: httpx.Client,
    path: str,
    content: str,
    message: str,
    sha: Optional[str],
) -> dict:
    s = get_settings()
    body = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": s.github_branch,
    }
    if sha:
        body["sha"] = sha
    r = client.put(_contents_url(path), json=body, headers=_headers())
    if r.status_code not in (200, 201):
        raise GitHubDeployError(
            f"PUT {_contents_url(path)} failed: {r.status_code} {r.text[:400]}"
        )
    return r.json()


def deploy_plans(
    slug: str,
    full_plan_html: str,
    deck_sheet_html: str,
    coach_name: str,
    intake_id: str,
) -> DeployResult:
    """Commit both HTML files under coaches/<slug>/ and return their public URLs.

    Discovers the next week number for this coach by listing the existing
    directory, then writes:

        coaches/<slug>/week<n>-plan.html
        coaches/<slug>/week<n>-deck.html

    The two writes happen as two commits because the Contents API is one file
    per request; that's fine and keeps repo history readable.
    """
    s = get_settings()

    with httpx.Client(timeout=30.0) as client:
        week_number = _next_week_number(client, slug)
        plan_path = f"coaches/{slug}/week{week_number}-plan.html"
        deck_path = f"coaches/{slug}/week{week_number}-deck.html"

        log.info(
            "deploying slug=%s week=%d plan_path=%s deck_path=%s",
            slug, week_number, plan_path, deck_path,
        )

        commit_msg_base = (
            f"coach:{slug} week:{week_number} intake:{intake_id} ({coach_name})"
        )

        # The new paths shouldn't exist yet (we just picked week N+1), but
        # fetch existing SHAs anyway — cheap, and defensive if a prior deploy
        # half-landed.
        plan_sha = _get_existing_sha(client, plan_path)
        deck_sha = _get_existing_sha(client, deck_path)

        plan_resp = _put_file(
            client,
            plan_path,
            full_plan_html,
            f"{commit_msg_base} — full plan",
            plan_sha,
        )
        deck_resp = _put_file(
            client,
            deck_path,
            deck_sheet_html,
            f"{commit_msg_base} — deck sheet",
            deck_sha,
        )

    commit_sha = (deck_resp.get("commit") or {}).get("sha") or (
        plan_resp.get("commit") or {}
    ).get("sha", "")

    plan_url = f"{s.public_base_url}/coaches/{slug}/week{week_number}-plan.html"
    deck_url = f"{s.public_base_url}/coaches/{slug}/week{week_number}-deck.html"

    log.info(
        "deploy ok slug=%s week=%d commit=%s plan=%s deck=%s",
        slug, week_number,
        commit_sha[:7] if commit_sha else "?",
        plan_url, deck_url,
    )

    return DeployResult(
        plan_url=plan_url,
        deck_url=deck_url,
        commit_sha=commit_sha,
        plan_path=plan_path,
        deck_path=deck_path,
        week_number=week_number,
    )
