"""Environment configuration + shared helpers for the FirstWhistle webhook.

Railway-aligned env var layout (5 required + a few optional):

    ANTHROPIC_API_KEY     Claude auth
    GITHUB_TOKEN          PAT w/ Contents: RW on target repo
    GITHUB_REPO           Combined "owner/repo" (e.g. "coachiq-source/CoachIq")
    RESEND_API_KEY        Resend auth
    COACH_EMAIL_FROM      Sender address

Optional overrides:

    CLAUDE_MODEL                    (default "claude-sonnet-4-20250514")
    CLAUDE_MAX_TOKENS               (default 16000)
    GITHUB_BRANCH                   (default "main")
    PUBLIC_BASE_URL                 (default https://<owner>.github.io/<repo>)
    FORMSPREE_SECRET_WATERPOLO          HMAC signing secret for the water polo form
    FORMSPREE_SECRET_LACROSSE           HMAC signing secret for the lacrosse form
    FORMSPREE_SECRET_LACROSSE_GAMEPREP  HMAC signing secret for the lacrosse
                                        game-prep form (separate Formspree form
                                        from the weekly lacrosse intake)
    WEBHOOK_SECRET                      (legacy, deprecated endpoint only)
    EMAIL_REPLY_TO                  (optional reply-to header)
    OPS_NOTIFY_EMAIL                (default "johnmaxwell.kelly@gmail.com")
    LACROSSE_HOLDING_HOURS          (default 48)
    HOST / PORT / LOG_LEVEL / SYSTEM_PROMPT_PATH
"""
from __future__ import annotations

import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ---- Ops defaults ---------------------------------------------------------

DEFAULT_OPS_NOTIFY_EMAIL = "johnmaxwell.kelly@gmail.com"
FEEDBACK_FORM_URL = (
    "https://docs.google.com/forms/d/e/"
    "1FAIpQLSe21HIuajLgy1pNStNaY7828mkNjprCqy59tWgVchx11jqnwQ/viewform"
)


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val


def _optional(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _split_owner_repo(combined: str) -> tuple[str, str]:
    """Split a GITHUB_REPO value of the form 'owner/repo' into two pieces."""
    cleaned = combined.strip().strip("/")
    if "/" not in cleaned:
        raise RuntimeError(
            f"GITHUB_REPO must be in the form 'owner/repo', got: {combined!r}"
        )
    owner, _, repo = cleaned.partition("/")
    owner = owner.strip()
    repo = repo.strip()
    if not owner or not repo:
        raise RuntimeError(
            f"GITHUB_REPO could not be parsed into owner+repo: {combined!r}"
        )
    if "/" in repo:
        raise RuntimeError(
            f"GITHUB_REPO must be exactly 'owner/repo' — extra slashes in {combined!r}"
        )
    return owner, repo


@dataclass(frozen=True)
class Settings:
    # Claude
    anthropic_api_key: str
    claude_model: str
    claude_max_tokens: int

    # Legacy single-secret auth (used by the deprecated /webhook/formspree
    # endpoint). Kept so the old endpoint still refuses to silently accept
    # traffic; new sport-specific endpoints use HMAC instead.
    webhook_secret: str

    # Per-sport Formspree HMAC signing secrets (one per Formspree form).
    # Game-prep forms run under separate Formspree ids from the weekly intake
    # forms, so they need their own secrets. Water polo's game-prep secret
    # has been historically reused from `formspree_secret_waterpolo` (both
    # live on the same Formspree form id `myklwjnp`); lacrosse game prep
    # lives on its own Formspree form and therefore gets its own secret.
    formspree_secret_waterpolo: str
    formspree_secret_lacrosse: str
    formspree_secret_lacrosse_gameprep: str

    # Lacrosse holding-email wording knob (SLA we promise the coach).
    lacrosse_holding_hours: int

    # GitHub — owner/repo split from a single GITHUB_REPO env var.
    github_token: str
    github_owner: str
    github_repo: str
    github_branch: str
    public_base_url: str

    # Resend
    resend_api_key: str
    email_from: str
    email_reply_to: str
    ops_notify_email: str

    # Server
    host: str
    port: int
    log_level: str
    system_prompt_path: Path


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load settings once per process. Fails fast if required vars missing."""
    github_repo_combined = _require("GITHUB_REPO")
    gh_owner, gh_repo = _split_owner_repo(github_repo_combined)
    gh_branch = _optional("GITHUB_BRANCH", "main")

    # Derive a GitHub-Pages URL by default; allow an explicit override.
    default_public_base = f"https://{gh_owner}.github.io/{gh_repo}"
    public_base = _optional("PUBLIC_BASE_URL", default_public_base).rstrip("/")

    return Settings(
        anthropic_api_key=_require("ANTHROPIC_API_KEY"),
        claude_model=_optional("CLAUDE_MODEL", "claude-sonnet-4-20250514"),
        claude_max_tokens=int(_optional("CLAUDE_MAX_TOKENS", "16000")),
        webhook_secret=_optional("WEBHOOK_SECRET", ""),
        formspree_secret_waterpolo=_optional("FORMSPREE_SECRET_WATERPOLO", ""),
        formspree_secret_lacrosse=_optional("FORMSPREE_SECRET_LACROSSE", ""),
        formspree_secret_lacrosse_gameprep=_optional(
            "FORMSPREE_SECRET_LACROSSE_GAMEPREP", ""
        ),
        lacrosse_holding_hours=int(_optional("LACROSSE_HOLDING_HOURS", "48")),
        github_token=_require("GITHUB_TOKEN"),
        github_owner=gh_owner,
        github_repo=gh_repo,
        github_branch=gh_branch,
        public_base_url=public_base,
        resend_api_key=_require("RESEND_API_KEY"),
        email_from=_require("COACH_EMAIL_FROM"),
        email_reply_to=_optional("EMAIL_REPLY_TO", ""),
        ops_notify_email=_optional("OPS_NOTIFY_EMAIL", DEFAULT_OPS_NOTIFY_EMAIL),
        host=_optional("HOST", "0.0.0.0"),
        port=int(_optional("PORT", "8000")),
        log_level=_optional("LOG_LEVEL", "INFO").upper(),
        system_prompt_path=Path(
            _optional("SYSTEM_PROMPT_PATH", "firstwhistle_master_system_prompt.md")
        ),
    )


@lru_cache(maxsize=1)
def load_system_prompt() -> str:
    """Load the master system prompt from disk, cached for the process lifetime."""
    settings = get_settings()
    path = settings.system_prompt_path
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.exists():
        raise RuntimeError(
            f"System prompt file not found at {path}. "
            "Set SYSTEM_PROMPT_PATH in the environment or drop the file in place."
        )
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise RuntimeError(f"System prompt file {path} is empty.")
    return text


def configure_logging() -> logging.Logger:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logger = logging.getLogger("firstwhistle")
    logger.setLevel(settings.log_level)
    return logger


_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(value: str, fallback: str = "coach") -> str:
    """URL-safe slug: lowercase, ascii-only, hyphen-separated."""
    if not value:
        return fallback
    # Decompose accents, keep ascii
    normalized = unicodedata.normalize("NFKD", value)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = _SLUG_STRIP.sub("-", ascii_only).strip("-")
    return slug or fallback
