"""FastAPI entrypoint for the FirstWhistle webhook server."""
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
from .pipeline import run_pipeline

log = configure_logging()

app = FastAPI(
    title="FirstWhistle Webhook",
    version=__version__,
    docs_url="/docs",
    redoc_url=None,
)


@app.on_event("startup")
def _startup() -> None:
    # Fail fast on startup if env or prompt file is misconfigured.
    settings = get_settings()
    try:
        prompt_len = len(load_system_prompt())
    except Exception as exc:  # pragma: no cover
        log.error("failed to load system prompt on startup: %s", exc)
        raise
    log.info(
        "firstwhistle webhook starting v%s model=%s prompt_chars=%d public_base=%s",
        __version__, settings.claude_model, prompt_len, settings.public_base_url,
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
    }


def _verify_secret(
    header_secret: str | None,
    body_secret: str | None,
) -> None:
    expected = get_settings().webhook_secret
    # Session 1: WEBHOOK_SECRET is optional. If it's not configured, skip auth
    # entirely. Harden this in a later session before going to real production.
    if not expected:
        return
    # Constant-time compare against either source.
    for candidate in (header_secret, body_secret):
        if candidate and hmac.compare_digest(candidate, expected):
            return
    log.warning("webhook auth failed header=%r body=%r", bool(header_secret), bool(body_secret))
    raise HTTPException(status_code=401, detail="invalid or missing webhook secret")


async def _parse_request_body(request: Request) -> Dict[str, Any]:
    """Handle JSON or form-urlencoded or multipart from Formspree."""
    content_type = (request.headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        try:
            data = await request.json()
        except Exception as exc:
            raise HTTPException(400, detail=f"invalid JSON: {exc}")
        if not isinstance(data, dict):
            raise HTTPException(400, detail="JSON body must be an object")
        return data

    if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        form = await request.form()
        # form.multi_items() preserves duplicate keys; we collapse into a dict
        # but keep lists for repeat keys.
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

    # Fallback: try JSON parse of raw body (some forwarders omit content-type).
    raw = (await request.body()).decode("utf-8", errors="replace").strip()
    if not raw:
        raise HTTPException(400, detail="empty request body")
    try:
        data = json.loads(raw)
    except Exception:
        raise HTTPException(400, detail=f"unsupported content-type: {content_type!r}")
    if not isinstance(data, dict):
        raise HTTPException(400, detail="body must be a JSON object")
    return data


@app.post("/webhook/formspree")
async def webhook_formspree(
    request: Request,
    background: BackgroundTasks,
    x_webhook_secret: str | None = Header(default=None, alias="X-Webhook-Secret"),
) -> JSONResponse:
    body = await _parse_request_body(request)
    _verify_secret(x_webhook_secret, str(body.get("webhook_secret", "")) or None)

    try:
        intake = parse_formspree_payload(body)
    except IntakeValidationError as exc:
        log.warning("intake rejected: %s", exc)
        raise HTTPException(422, detail=str(exc))

    log.info(
        "intake accepted id=%s slug=%s coach=%s email=%s",
        intake["intake_id"], intake["slug"],
        intake["coach_name"], intake["coach_email"],
    )

    # Kick off the expensive work in the background so we can ack Formspree
    # within its short timeout. The pipeline swallows its own errors and
    # reports them via Resend.
    background.add_task(run_pipeline, intake)

    return JSONResponse(
        status_code=202,
        content={
            "accepted": True,
            "intake_id": intake["intake_id"],
            "slug": intake["slug"],
        },
    )


@app.get("/")
def root() -> Dict[str, str]:
    return {"service": "firstwhistle-webhook", "version": __version__}
