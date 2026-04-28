"""FastAPI entrypoint for the FirstWhistle webhook server.

Routes:

  POST /webhook/formspree/waterpolo
      Verified via HMAC against FORMSPREE_SECRET_WATERPOLO. Branches on the
      intake's `form_type` field:
        * `form_type == "gameprep"` → the single-document game-prep pipeline
          (Part G of the master prompt, file `coaches/<slug>/gameprep-<
          opponent-slug>.html`, single-link coach email).
        * `form_type == "postgame"` → the week-in-review capture handler.
          Uses a lenient parser (the postgame form does NOT supply
          `coach_name`, so the weekly-plan strict validator would 422 it)
          and for now just logs the submission and stores the raw body on
          the Railway volume for later pipeline work.
        * anything else → the regular weekly two-document pipeline
          (full plan + deck sheet).

  POST /webhook/formspree/lacrosse
      Verified via HMAC against FORMSPREE_SECRET_LACROSSE. Branches on the
      intake's `form_type` field (same shape as the waterpolo route):
        * `form_type == "gameprep"` → the lacrosse single-document game-
          prep pipeline (SECTION LAX-G of the master prompt, file
          `coaches/<slug>/lacrosse-gameprep-<opponent-slug>.html`,
          single-link coach email in lacrosse terminology).
        * anything else → the weekly two-document pipeline (full plan +
          field sheet) flagged with `sport=lacrosse` so the master prompt
          routes to Section B (LADM, USL drill library, 0–3 year coach
          language).
      The legacy holding-email path is preserved in
      `app.lacrosse.run_lacrosse_holding` for emergency rollback.

  POST /webhook/formspree/basketball
      Verified via HMAC against FORMSPREE_SECRET_BASKETBALL. Branches on
      the intake's `form_type` field (same shape as waterpolo / lacrosse):
        * `form_type == "gameprep"` → routed to the basketball gameprep
          stub, which falls back to the weekly basketball pipeline (Part
          0 of the master prompt: basketball gameprep is not yet a
          first-class pipeline; the model adds a coaching-notes mismatch
          flag and returns the weekly two-document output).
        * anything else → the weekly two-document pipeline (full plan +
          court sheet) flagged with `sport=basketball` so the master
          prompt routes to SECTION C (USA Basketball terminology, 0–3
          year coach language, court sheet sport-specific naming).

  POST /webhook/formspree  (deprecated)
      Returns 410 Gone; kept so legacy Formspree integrations fail loudly
      instead of silently disappearing.
"""
from __future__ import annotations

import hmac
import json
import logging
from typing import Any, Dict

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import __version__
from .coach_store import CoachStoreError, get_coach_profile, upsert_coach_profile
from .config import configure_logging, get_settings, load_system_prompt
from .gameprep import run_gameprep_pipeline, run_lacrosse_gameprep_pipeline
from .intake import (
    IntakeValidationError,
    parse_formspree_payload,
    parse_postgame_payload,
    peek_form_type,
)
from .lacrosse import run_lacrosse_pipeline
from .basketball import run_basketball_pipeline, run_basketball_gameprep_pipeline
from .pipeline import run_pipeline
from .postgame import run_postgame_handler
from .security import SignatureError, verify_formspree_signature

log = configure_logging()

app = FastAPI(
    title="FirstWhistle Webhook",
    version=__version__,
    docs_url="/docs",
    redoc_url=None,
)

# CORS: the CoachPrep returning-coach endpoints are called directly from the
# GitHub-Pages-hosted intake forms (same-origin with the pipeline is not an
# option — the forms live at coachiq-source.github.io). We intentionally
# allow any origin for the `/coach*` routes because the payload is low-value
# (a pseudo-public code → four non-secret fields) and the endpoints are
# rate-limited by Railway at the infra layer. Webhook routes stay
# HMAC-verified regardless of CORS — an attacker can't forge Formspree
# signatures from a browser.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    allow_credentials=False,
    max_age=600,
)


@app.on_event("startup")
def _startup() -> None:
    settings = get_settings()
    try:
        prompt_len = len(load_system_prompt())
    except Exception as exc:  # pragma: no cover
        log.error("failed to load system prompt on startup: %s", exc)
        raise
    log.info(
        "firstwhistle webhook starting v%s model=%s prompt_chars=%d public_base=%s "
        "waterpolo_secret=%s lacrosse_secret=%s basketball_secret=%s",
        __version__, settings.claude_model, prompt_len, settings.public_base_url,
        "SET" if settings.formspree_secret_waterpolo else "MISSING",
        "SET" if settings.formspree_secret_lacrosse else "MISSING",
        "SET" if settings.formspree_secret_basketball else "MISSING",
    )


@app.get("/health")
def health() -> Dict[str, Any]:
    settings = get_settings()
    try:
        prompt_ok = bool(load_system_prompt())
    except Exception as exc:
        return {"ok": False, "error": f"system_prompt: {exc}"}
    return {
        "ok": True,
        "version": __version__,
        "model": settings.claude_model,
        "prompt_loaded": prompt_ok,
        "waterpolo_secret_configured": bool(settings.formspree_secret_waterpolo),
        "lacrosse_secret_configured": bool(settings.formspree_secret_lacrosse),
        "basketball_secret_configured": bool(settings.formspree_secret_basketball),
    }


async def _parse_body_flex(
    request: Request, raw_body: bytes
) -> Dict[str, Any]:
    """Parse a Formspree request body that was already read as bytes.

    We need the raw bytes for HMAC verification, so we can't let Starlette
    consume the body a second time via `request.json()` / `request.form()`
    without feeding it the cached bytes first. Luckily Starlette caches
    the first `.body()` call, so subsequent calls read from cache.
    """
    content_type = (request.headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        try:
            data = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except Exception as exc:
            raise HTTPException(400, detail=f"invalid JSON: {exc}")
        if not isinstance(data, dict):
            raise HTTPException(400, detail="JSON body must be an object")
        return data

    if (
        "application/x-www-form-urlencoded" in content_type
        or "multipart/form-data" in content_type
    ):
        # Starlette's `request.form()` will re-read the cached body for us.
        form = await request.form()
        out: Dict[str, Any] = {}
        for k, v in form.multi_items():
            if k in out:
                existing = out[k]
                if isinstance(existing, list):
                    existing.append(v)
                else:
                    out[k] = [existing, v]
            else:
                out[k] = v
        return out

    # Fallback: JSON-ish raw body with no content-type set.
    raw = raw_body.decode("utf-8", errors="replace").strip()
    if not raw:
        raise HTTPException(400, detail="empty request body")
    try:
        data = json.loads(raw)
    except Exception:
        raise HTTPException(400, detail=f"unsupported content-type: {content_type!r}")
    if not isinstance(data, dict):
        raise HTTPException(400, detail="body must be a JSON object")
    return data


def _verify_hmac_or_401(
    sport: str,
    raw_body: bytes,
    signature_header: str | None,
    signing_secret: str,
) -> None:
    if not signing_secret:
        log.error("webhook %s called but no signing secret configured", sport)
        raise HTTPException(503, detail=f"{sport} signing secret not configured")
    try:
        verify_formspree_signature(
            raw_body=raw_body,
            header_value=signature_header or "",
            signing_secret=signing_secret,
        )
    except SignatureError as exc:
        log.warning("webhook %s signature rejected: %s", sport, exc)
        raise HTTPException(401, detail=f"invalid signature: {exc}")


async def _handle_sport_webhook(
    sport: str,
    request: Request,
    background: BackgroundTasks,
    signature_header: str | None,
    signing_secret: str,
) -> JSONResponse:
    # 1. Read raw body for HMAC verification.
    raw_body = await request.body()

    # 2. Verify Formspree HMAC signature BEFORE parsing, spending Claude
    #    credits, or touching the pipeline.
    _verify_hmac_or_401(sport, raw_body, signature_header, signing_secret)

    # 3. Flatten the body; DO NOT validate yet. Different form_types use
    #    different required-field sets — in particular the post-game form
    #    doesn't send `coach_name`, so running the weekly-plan strict
    #    validator first would 422 a perfectly legitimate submission.
    body = await _parse_body_flex(request, raw_body)
    form_type = peek_form_type(body)

    # 4. Route on form_type BEFORE strict validation.
    if sport == "waterpolo" and form_type == "postgame":
        # Post-game / Week-in-Review capture path. Lenient parser (never
        # raises on missing fields); handler just logs + stores the payload
        # to a JSONL archive for the future post-game pipeline.
        intake = parse_postgame_payload(body)
        intake["sport"] = sport
        coach_name_disp = intake.get("coach_name") or "(unknown)"
        log.info(
            "postgame intake accepted coach=%s sport=%s",
            coach_name_disp, sport,
        )
        background.add_task(run_postgame_handler, intake)
        return JSONResponse(
            status_code=202,
            content={
                "accepted": True,
                "sport": sport,
                "form_type": "postgame",
                "intake_id": intake["intake_id"],
                "slug": intake["slug"],
            },
        )

    # 5. Non-postgame paths use the strict parser (gameprep + weekly-plan
    #    both require the full coach_name/coach_email contract).
    try:
        intake = parse_formspree_payload(body)
    except IntakeValidationError as exc:
        log.warning("%s intake rejected: %s", sport, exc)
        raise HTTPException(422, detail=str(exc))

    intake["sport"] = sport
    # Re-read form_type from the parsed intake (peek is best-effort; the
    # strict parser is authoritative once it succeeds).
    form_type = str(intake.get("form_type") or form_type or "").strip().lower()
    log.info(
        "%s intake accepted id=%s slug=%s coach=%s email=%s form_type=%s",
        sport, intake["intake_id"], intake["slug"],
        intake["coach_name"], intake["coach_email"],
        form_type or "(none)",
    )
    if sport == "waterpolo":
        if form_type == "gameprep":
            background.add_task(run_gameprep_pipeline, intake)
        else:
            # Default / "week" / anything else → regular weekly pipeline.
            background.add_task(run_pipeline, intake)
    elif sport == "lacrosse":
        if form_type == "gameprep":
            # Lacrosse game prep — single-document pipeline keyed on
            # SECTION LAX-G of the master prompt, deployed to
            # coaches/<slug>/lacrosse-gameprep-<opp-slug>.html.
            background.add_task(run_lacrosse_gameprep_pipeline, intake)
        else:
            # Default / "week" / anything else → regular weekly lacrosse
            # pipeline (full plan + field sheet).
            background.add_task(run_lacrosse_pipeline, intake)
    elif sport == "basketball":
        if form_type == "gameprep":
            # Basketball game prep is not yet a first-class pipeline.
            # Part 0 of the master prompt routes such submissions back
            # into the weekly flow with a coaching-notes mismatch flag;
            # the stub does the same dispatch so log greps still see
            # the form_type=gameprep lineage.
            background.add_task(run_basketball_gameprep_pipeline, intake)
        else:
            # Default / "week" / anything else → regular weekly
            # basketball pipeline (full plan + court sheet).
            background.add_task(run_basketball_pipeline, intake)
    else:  # pragma: no cover — defensive
        raise HTTPException(500, detail=f"unknown sport: {sport}")

    return JSONResponse(
        status_code=202,
        content={
            "accepted": True,
            "sport": sport,
            "form_type": form_type or "week",
            "intake_id": intake["intake_id"],
            "slug": intake["slug"],
        },
    )


@app.post("/webhook/formspree/waterpolo")
async def webhook_waterpolo(
    request: Request,
    background: BackgroundTasks,
    formspree_signature: str | None = Header(default=None, alias="Formspree-Signature"),
) -> JSONResponse:
    secret = get_settings().formspree_secret_waterpolo
    return await _handle_sport_webhook(
        sport="waterpolo",
        request=request,
        background=background,
        signature_header=formspree_signature,
        signing_secret=secret,
    )


@app.post("/webhook/formspree/lacrosse")
async def webhook_lacrosse(
    request: Request,
    background: BackgroundTasks,
    formspree_signature: str | None = Header(default=None, alias="Formspree-Signature"),
) -> JSONResponse:
    secret = get_settings().formspree_secret_lacrosse
    return await _handle_sport_webhook(
        sport="lacrosse",
        request=request,
        background=background,
        signature_header=formspree_signature,
        signing_secret=secret,
    )


@app.post("/webhook/formspree/basketball")
async def webhook_basketball(
    request: Request,
    background: BackgroundTasks,
    formspree_signature: str | None = Header(default=None, alias="Formspree-Signature"),
) -> JSONResponse:
    secret = get_settings().formspree_secret_basketball
    return await _handle_sport_webhook(
        sport="basketball",
        request=request,
        background=background,
        signature_header=formspree_signature,
        signing_secret=secret,
    )


@app.post("/webhook/formspree")
async def webhook_formspree_deprecated(request: Request) -> JSONResponse:
    """Deprecated endpoint. Returns 410 Gone with a pointer to the new URLs.

    Kept so any still-connected Formspree integration fails loudly during the
    migration window. Once both forms are pointed at the new per-sport URLs
    this endpoint can be removed entirely.
    """
    log.warning(
        "deprecated /webhook/formspree called from %s — migrate Formspree to /webhook/formspree/{sport}",
        request.client.host if request.client else "unknown",
    )
    return JSONResponse(
        status_code=410,
        content={
            "error": "deprecated",
            "message": (
                "Use /webhook/formspree/waterpolo, /webhook/formspree/lacrosse, "
                "or /webhook/formspree/basketball. Each sport has its own "
                "signing secret; the webhook branches on `formType` server-"
                "side to dispatch weekly / gameprep / postgame."
            ),
        },
    )


# ---------------------------------------------------------------------------
# CoachPrep returning-coach profile store
# ---------------------------------------------------------------------------

class CoachProfileIn(BaseModel):
    """Body accepted by POST /coach."""
    code: str = Field(..., description="4–12 chars, letters/digits/._-, no spaces.")
    name: str = Field(..., min_length=1)
    email: str = Field(..., min_length=3)
    program: str = Field("", description="School / Club / Program name.")
    sport: str = Field("waterpolo", description="waterpolo | lacrosse | basketball")


class CoachProfileOut(BaseModel):
    code: str
    name: str
    email: str
    program: str
    sport: str
    created_at: str
    updated_at: str


@app.post("/coach", response_model=CoachProfileOut)
def post_coach(body: CoachProfileIn) -> CoachProfileOut:
    """Upsert a coach profile indexed by the coach-chosen code.

    Called on every intake submission when the coach provided a code — the
    pipeline itself also writes to this store from the background task, but
    we expose the endpoint directly so frontends can "pre-register" a code
    without a full intake if we ever need that.
    """
    try:
        profile = upsert_coach_profile(
            code=body.code,
            name=body.name,
            email=body.email,
            program=body.program,
            sport=body.sport,
        )
    except CoachStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    log.info(
        "coach upsert via POST /coach code=%s sport=%s",
        profile.code, profile.sport,
    )
    return CoachProfileOut(**profile.__dict__)


@app.get("/coach/{code}", response_model=CoachProfileOut)
def get_coach(code: str) -> CoachProfileOut:
    """Look up a coach profile by code. 404 if the code isn't registered.

    Used by the intake forms to auto-fill name / email / program / sport
    when a returning coach types their code and hits "Apply."
    """
    try:
        profile = get_coach_profile(code)
    except CoachStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if profile is None:
        raise HTTPException(
            status_code=404,
            detail=f"No CoachPrep profile found for code {code!r}.",
        )
    return CoachProfileOut(**profile.__dict__)


@app.get("/")
def root() -> Dict[str, str]:
    return {"service": "firstwhistle-webhook", "version": __version__}
