"""FastAPI entrypoint for the FirstWhistle webhook server.

Routes:

  POST /webhook/formspree/waterpolo
      Verified via HMAC against FORMSPREE_SECRET_WATERPOLO. Triggers the
      full Claude + GitHub + coach-email pipeline.

  POST /webhook/formspree/lacrosse
      Verified via HMAC against FORMSPREE_SECRET_LACROSSE. Triggers the
      full Claude + GitHub + coach-email pipeline (same as water polo).
      The intake is flagged with `sport=lacrosse` so the master system
      prompt routes to Section B (LADM, USL drill library, 0–3 year
      coach language). The legacy holding-email path is preserved in
      `app.lacrosse.run_lacrosse_holding` for emergency rollback.

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
from fastapi.responses import JSONResponse

from . import __version__
from .config import configure_logging, get_settings, load_system_prompt
from .intake import IntakeValidationError, parse_formspree_payload
from .lacrosse import run_lacrosse_pipeline
from .pipeline import run_pipeline
from .security import SignatureError, verify_formspree_signature

log = configure_logging()

app = FastAPI(
    title="FirstWhistle Webhook",
    version=__version__,
    docs_url="/docs",
    redoc_url=None,
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
        "waterpolo_secret=%s lacrosse_secret=%s",
        __version__, settings.claude_model, prompt_len, settings.public_base_url,
        "SET" if settings.formspree_secret_waterpolo else "MISSING",
        "SET" if settings.formspree_secret_lacrosse else "MISSING",
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

    # 3. Parse.
    body = await _parse_body_flex(request, raw_body)
    try:
        intake = parse_formspree_payload(body)
    except IntakeValidationError as exc:
        log.warning("%s intake rejected: %s", sport, exc)
        raise HTTPException(422, detail=str(exc))

    # 4. Dispatch to sport-specific background work.
    intake["sport"] = sport
    log.info(
        "%s intake accepted id=%s slug=%s coach=%s email=%s",
        sport, intake["intake_id"], intake["slug"],
        intake["coach_name"], intake["coach_email"],
    )
    if sport == "waterpolo":
        background.add_task(run_pipeline, intake)
    elif sport == "lacrosse":
        background.add_task(run_lacrosse_pipeline, intake)
    else:  # pragma: no cover — defensive
        raise HTTPException(500, detail=f"unknown sport: {sport}")

    return JSONResponse(
        status_code=202,
        content={
            "accepted": True,
            "sport": sport,
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
                "Use /webhook/formspree/waterpolo or /webhook/formspree/lacrosse. "
                "Each form has its own signing secret."
            ),
        },
    )


@app.get("/")
def root() -> Dict[str, str]:
    return {"service": "firstwhistle-webhook", "version": __version__}
