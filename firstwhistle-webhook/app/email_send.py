"""Send the coach their plan + deck links via Resend."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from html import escape
from typing import Optional

import resend

from .config import FEEDBACK_FORM_URL, get_settings

log = logging.getLogger("firstwhistle.email")


class EmailSendError(RuntimeError):
    pass


@dataclass(frozen=True)
class EmailResult:
    message_id: str
    to: str


def _init() -> None:
    resend.api_key = get_settings().resend_api_key


def _first_name(coach_name: str) -> str:
    return coach_name.split()[0] if coach_name else "Coach"


def _coach_html(
    coach_name: str,
    week_number: int,
    plan_url: str,
    deck_url: str,
) -> str:
    name = escape(_first_name(coach_name))
    return f"""\
<!doctype html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#111;max-width:560px;margin:0 auto;padding:24px;line-height:1.55">
  <h1 style="font-size:22px;margin:0 0 16px 0;">Your Week {week_number} practice plan is ready</h1>
  <p>Hi {name},</p>
  <p>Your Week {week_number} practice plan and one-page deck sheet are live:</p>
  <p style="margin:24px 0;">
    <a href="{escape(plan_url)}" style="display:inline-block;background:#0b3d91;color:#fff;text-decoration:none;padding:12px 18px;border-radius:6px;font-weight:600;margin-right:8px;">View full practice plan</a>
    <a href="{escape(deck_url)}" style="display:inline-block;background:#eaeef8;color:#0b3d91;text-decoration:none;padding:12px 18px;border-radius:6px;font-weight:600;">Deck sheet (printable)</a>
  </p>
  <p style="font-size:14px;color:#444;">Open these on deck from your phone, or print the deck sheet and tape it to the wall. The full plan is the coach-facing version with rationale, progressions, and coaching cues.</p>
  <p style="font-size:14px;color:#444;">When you've run the week, please share a quick note on how it went — it shapes next week's plan:<br>
    <a href="{escape(FEEDBACK_FORM_URL)}">{escape(FEEDBACK_FORM_URL)}</a>
  </p>
  <p style="margin-top:32px;">— FirstWhistle Coaching</p>
  <hr style="border:none;border-top:1px solid #eee;margin:24px 0;">
  <p style="font-size:12px;color:#888;">Plain links, in case the buttons don't work:<br>
    Plan: <a href="{escape(plan_url)}">{escape(plan_url)}</a><br>
    Deck: <a href="{escape(deck_url)}">{escape(deck_url)}</a><br>
    Feedback: <a href="{escape(FEEDBACK_FORM_URL)}">{escape(FEEDBACK_FORM_URL)}</a>
  </p>
</body></html>
"""


def _coach_text(
    coach_name: str,
    week_number: int,
    plan_url: str,
    deck_url: str,
) -> str:
    first = _first_name(coach_name)
    return (
        f"Hi {first},\n\n"
        f"Your Week {week_number} FirstWhistle practice plan is ready.\n\n"
        f"Full practice plan: {plan_url}\n"
        f"Deck sheet (printable): {deck_url}\n\n"
        "Open the full plan on your phone, print the deck sheet for the wall.\n\n"
        "When you've run the week, please share a quick note on how it went — "
        "it shapes next week's plan:\n"
        f"{FEEDBACK_FORM_URL}\n\n"
        "— FirstWhistle Coaching\n"
    )


def send_coach_email(
    coach_name: str,
    coach_email: str,
    week_number: int,
    plan_url: str,
    deck_url: str,
) -> EmailResult:
    _init()
    s = get_settings()
    subject = (
        f"FirstWhistle — Your Week {week_number} Plan is Ready, "
        f"{_first_name(coach_name)}"
    )
    params: dict = {
        "from": s.email_from,
        "to": [coach_email],
        "subject": subject,
        "html": _coach_html(coach_name, week_number, plan_url, deck_url),
        "text": _coach_text(coach_name, week_number, plan_url, deck_url),
    }
    if s.email_reply_to:
        params["reply_to"] = [s.email_reply_to]

    try:
        resp = resend.Emails.send(params)
    except Exception as exc:
        log.exception("resend send failed")
        raise EmailSendError(f"resend send failed: {exc}") from exc

    msg_id = (resp or {}).get("id", "") if isinstance(resp, dict) else getattr(resp, "id", "")
    log.info("coach email sent to=%s week=%d id=%s", coach_email, week_number, msg_id)
    return EmailResult(message_id=msg_id or "", to=coach_email)


def send_ops_failure_email(
    intake_id: str,
    coach_name: str,
    coach_email: str,
    stage: str,
    error: str,
    details: Optional[str] = None,
) -> Optional[EmailResult]:
    """Notify ops when the pipeline fails. No-op if OPS_NOTIFY_EMAIL is unset."""
    _init()
    s = get_settings()
    if not s.ops_notify_email:
        return None

    subject = f"[FirstWhistle] pipeline failed at {stage} — {coach_name}"
    body_text = (
        f"Intake: {intake_id}\n"
        f"Coach:  {coach_name} <{coach_email}>\n"
        f"Stage:  {stage}\n"
        f"Error:  {error}\n"
    )
    if details:
        body_text += f"\nDetails:\n{details[:4000]}\n"

    try:
        resp = resend.Emails.send({
            "from": s.email_from,
            "to": [s.ops_notify_email],
            "subject": subject,
            "text": body_text,
        })
    except Exception as exc:
        log.exception("ops failure email failed: %s", exc)
        return None

    msg_id = (resp or {}).get("id", "") if isinstance(resp, dict) else getattr(resp, "id", "")
    return EmailResult(message_id=msg_id or "", to=s.ops_notify_email)
