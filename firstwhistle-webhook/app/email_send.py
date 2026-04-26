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


# Sport -> human label for the one-page printable sheet.
# The water-polo "deck sheet" naming is intentionally the default so that any
# intake without a sport field (older rows, tests, misconfigured forms)
# renders the same wording it did before this change.
_SHEET_LABEL_BY_SPORT: dict[str, str] = {
    "waterpolo": "Deck sheet",
    "water_polo": "Deck sheet",
    "lacrosse": "Field sheet",
    "basketball": "Court sheet",
}


# Sport -> "where the coach will be when they open this on their phone"
# surface phrase. Used in the weekly coach-email body line "Open these
# {open_surface} from your phone, or print the {sheet_lower} and tape it
# to the wall." Distinct from `_GAMEPREP_SURFACE_BY_SPORT` (which uses
# "to the field" for the "print it and bring it …" gameprep prose) — here
# we want the coach's *current location* rather than where they're carrying
# the printout to. Water polo stays the default so any caller that doesn't
# pass a sport keeps the original "on deck" wording.
_OPEN_SURFACE_BY_SPORT: dict[str, str] = {
    "waterpolo": "on deck",
    "water_polo": "on deck",
    "lacrosse": "on the field",
    "basketball": "on the bench",
}


def _open_surface(sport: Optional[str]) -> str:
    """Surface noun for the "Open these ___ from your phone" body line."""
    key = (sport or "").strip().lower()
    return _OPEN_SURFACE_BY_SPORT.get(key, "on deck")


def _sheet_label(sport: Optional[str]) -> str:
    """Return the sport-specific name for the printable one-pager (no suffix).

    Falls back to "Deck sheet" so unknown / missing sports keep the
    original water-polo wording rather than leaking something like
    "None sheet" into a coach's inbox.
    """
    if not sport:
        return "Deck sheet"
    return _SHEET_LABEL_BY_SPORT.get(sport.strip().lower(), "Deck sheet")


# Sport -> standalone game-prep intake form URL. When a coach_code is
# known for the intake, we append it as a query parameter so the form can
# prefill the returning-coach fields; otherwise the plain URL is used.
# Water polo keeps the original sport-less URL as the default so any
# caller that doesn't pass a sport (or passes one we don't recognise)
# gets the same link it did before this change.
_GAMEPREP_INTAKE_DEFAULT_URL = "https://coachiq-source.github.io/CoachIq/gameprep/"
_GAMEPREP_INTAKE_URLS_BY_SPORT: dict[str, str] = {
    "waterpolo": _GAMEPREP_INTAKE_DEFAULT_URL,
    "water_polo": _GAMEPREP_INTAKE_DEFAULT_URL,
    "lacrosse": "https://coachiq-source.github.io/CoachIq/gameprep/lacrosse.html",
}


def _gameprep_intake_url(
    sport: Optional[str],
    coach_code: Optional[str],
) -> str:
    key = (sport or "").strip().lower()
    base = _GAMEPREP_INTAKE_URLS_BY_SPORT.get(key, _GAMEPREP_INTAKE_DEFAULT_URL)
    code = (coach_code or "").strip()
    if not code:
        return base
    from urllib.parse import quote
    return f"{base}?code={quote(code, safe='')}"


# Sport -> standalone post-game / Week in Review intake form URL.
# The form takes an optional `code` query parameter to prefill the
# returning-coach fields, mirroring the game-prep intake behaviour.
_POSTGAME_INTAKE_URLS_BY_SPORT: dict[str, str] = {
    "waterpolo": "https://coachiq-source.github.io/CoachIq/postgame/waterpolo.html",
    "water_polo": "https://coachiq-source.github.io/CoachIq/postgame/waterpolo.html",
    "lacrosse": "https://coachiq-source.github.io/CoachIq/postgame/lacrosse.html",
}


def _postgame_intake_url(
    sport: Optional[str],
    coach_code: Optional[str],
) -> Optional[str]:
    """Return the Week-in-Review intake URL for the given sport.

    The sport is already passed to ``send_coach_email``; we use it to
    pick between the water-polo and lacrosse post-game forms. If the
    sport is unknown (e.g. basketball or missing), return ``None`` so
    the caller can skip the Week-in-Review link entirely rather than
    linking to the wrong form.
    """
    key = (sport or "").strip().lower()
    base = _POSTGAME_INTAKE_URLS_BY_SPORT.get(key)
    if not base:
        return None
    code = (coach_code or "").strip()
    if not code:
        return base
    from urllib.parse import quote
    return f"{base}?code={quote(code, safe='')}"


def _coach_html(
    coach_name: str,
    week_number: int,
    plan_url: str,
    deck_url: str,
    sport: Optional[str] = None,
    coach_code: Optional[str] = None,
) -> str:
    name = escape(_first_name(coach_name))
    sheet_label = _sheet_label(sport)
    # Lowercase form for prose ("print the deck sheet and tape it…");
    # short form for the plain-links footer ("Deck: <url>").
    sheet_lower = sheet_label.lower()
    sheet_short = sheet_label.split()[0]  # "Deck" / "Field" / "Court"
    open_surface = _open_surface(sport)
    gameprep_url = _gameprep_intake_url(sport, coach_code)
    postgame_url = _postgame_intake_url(sport, coach_code)

    # Optional fourth button + footer line for the Week-in-Review intake.
    # We only render it for sports where we actually have a post-game form
    # (water polo, lacrosse) so we never link a coach to the wrong sport.
    postgame_button_html = ""
    postgame_prose_html = ""
    postgame_footer_html = ""
    if postgame_url:
        postgame_button_html = (
            f'\n    <a href="{escape(postgame_url)}" '
            'style="display:inline-block;background:#eaeef8;color:#0b3d91;'
            'text-decoration:none;padding:12px 18px;border-radius:6px;'
            'font-weight:600;margin-bottom:8px;">Week in Review</a>'
        )
        postgame_prose_html = (
            f'\n  <p style="font-size:14px;color:#444;">Finished the week? '
            f'Fill in the <a href="{escape(postgame_url)}">Week in Review</a> '
            "so next week's plan builds on what just happened.</p>"
        )
        postgame_footer_html = (
            f'\n    Week in Review: <a href="{escape(postgame_url)}">'
            f'{escape(postgame_url)}</a><br>'
        )

    return f"""\
<!doctype html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#111;max-width:560px;margin:0 auto;padding:24px;line-height:1.55">
  <h1 style="font-size:22px;margin:0 0 16px 0;">Your Week {week_number} practice plan is ready</h1>
  <p>Hi {name},</p>
  <p>Your Week {week_number} practice plan and one-page {sheet_lower} are live:</p>
  <p style="margin:24px 0;">
    <a href="{escape(plan_url)}" style="display:inline-block;background:#0b3d91;color:#fff;text-decoration:none;padding:12px 18px;border-radius:6px;font-weight:600;margin-right:8px;margin-bottom:8px;">View full practice plan</a>
    <a href="{escape(deck_url)}" style="display:inline-block;background:#eaeef8;color:#0b3d91;text-decoration:none;padding:12px 18px;border-radius:6px;font-weight:600;margin-right:8px;margin-bottom:8px;">{sheet_label} (printable)</a>
    <a href="{escape(gameprep_url)}" style="display:inline-block;background:#eaeef8;color:#0b3d91;text-decoration:none;padding:12px 18px;border-radius:6px;font-weight:600;margin-right:8px;margin-bottom:8px;">Game prep intake</a>{postgame_button_html}
  </p>
  <p style="font-size:14px;color:#444;">Open these {escape(open_surface)} from your phone, or print the {sheet_lower} and tape it to the wall. The full plan is the coach-facing version with rationale, progressions, and coaching cues.</p>
  <p style="font-size:14px;color:#444;">Got a game coming up? Use the <a href="{escape(gameprep_url)}">game prep intake</a> to request an opponent scout and game-day package.</p>{postgame_prose_html}
  <p style="font-size:13px;color:#777777;margin-top:8px;">Links go live within 5 minutes of receiving this email.</p>
  <p style="font-size:14px;color:#444;">When you've run the week, please share a quick note on how it went — it shapes next week's plan:<br>
    <a href="{escape(FEEDBACK_FORM_URL)}">{escape(FEEDBACK_FORM_URL)}</a>
  </p>
  <p style="margin-top:32px;">— CoachPrep</p>
  <hr style="border:none;border-top:1px solid #eee;margin:24px 0;">
  <p style="font-size:12px;color:#888;">Plain links, in case the buttons don't work:<br>
    Plan: <a href="{escape(plan_url)}">{escape(plan_url)}</a><br>
    {sheet_short}: <a href="{escape(deck_url)}">{escape(deck_url)}</a><br>
    Game prep intake: <a href="{escape(gameprep_url)}">{escape(gameprep_url)}</a><br>{postgame_footer_html}
    Feedback: <a href="{escape(FEEDBACK_FORM_URL)}">{escape(FEEDBACK_FORM_URL)}</a>
  </p>
</body></html>
"""


def _coach_text(
    coach_name: str,
    week_number: int,
    plan_url: str,
    deck_url: str,
    sport: Optional[str] = None,
    coach_code: Optional[str] = None,
) -> str:
    first = _first_name(coach_name)
    sheet_label = _sheet_label(sport)
    sheet_lower = sheet_label.lower()
    gameprep_url = _gameprep_intake_url(sport, coach_code)
    postgame_url = _postgame_intake_url(sport, coach_code)

    # Week-in-Review line only renders for sports with a matching form.
    postgame_line = f"Week in Review: {postgame_url}\n" if postgame_url else ""
    postgame_prose = (
        "Finished the week? Fill in the Week in Review link above so next "
        "week's plan builds on what just happened.\n"
        if postgame_url
        else ""
    )

    return (
        f"Hi {first},\n\n"
        f"Your Week {week_number} CoachPrep practice plan is ready.\n\n"
        f"Full practice plan: {plan_url}\n"
        f"{sheet_label} (printable): {deck_url}\n"
        f"Game prep intake: {gameprep_url}\n"
        f"{postgame_line}"
        "\n"
        f"Open the full plan on your phone, print the {sheet_lower} for the wall.\n"
        "Got a game coming up? Use the game prep intake link above to request "
        "an opponent scout and game-day package.\n"
        f"{postgame_prose}"
        "Links go live within 5 minutes of receiving this email.\n\n"
        "When you've run the week, please share a quick note on how it went — "
        "it shapes next week's plan:\n"
        f"{FEEDBACK_FORM_URL}\n\n"
        "— CoachPrep\n"
    )


def send_coach_email(
    coach_name: str,
    coach_email: str,
    week_number: int,
    plan_url: str,
    deck_url: str,
    sport: Optional[str] = None,
    coach_code: Optional[str] = None,
) -> EmailResult:
    _init()
    s = get_settings()
    subject = (
        f"CoachPrep — Your Week {week_number} Plan is Ready, "
        f"{_first_name(coach_name)}"
    )
    params: dict = {
        "from": s.email_from,
        "to": [coach_email],
        "subject": subject,
        "html": _coach_html(
            coach_name, week_number, plan_url, deck_url, sport, coach_code,
        ),
        "text": _coach_text(
            coach_name, week_number, plan_url, deck_url, sport, coach_code,
        ),
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


def _lacrosse_coach_html(coach_name: str, holding_hours: int) -> str:
    name = escape(_first_name(coach_name))
    hrs = int(holding_hours)
    return f"""\
<!doctype html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#111;max-width:560px;margin:0 auto;padding:24px;line-height:1.55">
  <h1 style="font-size:22px;margin:0 0 16px 0;">We've got your intake — plan on the way</h1>
  <p>Hi {name},</p>
  <p>Thanks for sending through your team info. Your personalized lacrosse
  practice plan is being prepared and you'll receive it within the next
  {hrs} hours.</p>
  <p>If anything changes on your end before then (injuries, schedule shifts,
  new priorities), just reply to this email and I'll fold it in.</p>
  <p style="margin-top:32px;">— CoachPrep</p>
</body></html>
"""


def _lacrosse_coach_text(coach_name: str, holding_hours: int) -> str:
    first = _first_name(coach_name)
    return (
        f"Hi {first},\n\n"
        "Thanks for sending through your team info. Your personalized "
        f"lacrosse practice plan is being prepared and you'll receive it "
        f"within the next {int(holding_hours)} hours.\n\n"
        "If anything changes on your end before then — injuries, schedule "
        "shifts, new priorities — just reply to this email and I'll fold "
        "it in.\n\n"
        "— CoachPrep\n"
    )


def send_lacrosse_holding_email(
    coach_name: str,
    coach_email: str,
    holding_hours: int,
) -> EmailResult:
    """Coach-facing holding email for the lacrosse intake path.

    Lacrosse auto-generation isn't live yet; this email reassures the
    coach that we received their submission and will deliver manually.
    """
    _init()
    s = get_settings()
    subject = (
        f"CoachPrep — We've got your intake, {_first_name(coach_name)}"
    )
    params: dict = {
        "from": s.email_from,
        "to": [coach_email],
        "subject": subject,
        "html": _lacrosse_coach_html(coach_name, holding_hours),
        "text": _lacrosse_coach_text(coach_name, holding_hours),
    }
    if s.email_reply_to:
        params["reply_to"] = [s.email_reply_to]

    try:
        resp = resend.Emails.send(params)
    except Exception as exc:
        log.exception("lacrosse holding email failed")
        raise EmailSendError(f"resend send failed: {exc}") from exc

    msg_id = (resp or {}).get("id", "") if isinstance(resp, dict) else getattr(resp, "id", "")
    log.info(
        "lacrosse holding email sent to=%s id=%s hours=%d",
        coach_email, msg_id, int(holding_hours),
    )
    return EmailResult(message_id=msg_id or "", to=coach_email)


# Per-sport surface noun used in the game-prep email copy ("bring it on
# deck" vs "bring it to the field"). Water polo is the default so any
# caller that doesn't pass a sport keeps the original wording.
_GAMEPREP_SURFACE_BY_SPORT: dict[str, str] = {
    "waterpolo": "on deck",
    "water_polo": "on deck",
    "lacrosse": "to the field",
    "basketball": "to the bench",
}

# Per-sport "what the package covers" summary line. Mirrors the ten
# mandatory sections in the master prompt's game-prep part, phrased in
# the terminology of that sport.
_GAMEPREP_COVERS_BY_SPORT: dict[str, str] = {
    "waterpolo": (
        "their system, GK tendencies, top threats, your defensive "
        "assignments, your offensive answer, 5x6 shape, timeout scripts, "
        "and halftime triggers"
    ),
    "water_polo": (
        "their system, GK tendencies, top threats, your defensive "
        "assignments, your offensive answer, 5x6 shape, timeout scripts, "
        "and halftime triggers"
    ),
    "lacrosse": (
        "their system, goalie tendencies, top threats, your defensive "
        "matchups, your offensive answer, EMO shape, EMD plan, face-off "
        "and clearing cues, timeout scripts, and halftime triggers"
    ),
}


def _gameprep_surface(sport: Optional[str]) -> str:
    key = (sport or "").strip().lower()
    return _GAMEPREP_SURFACE_BY_SPORT.get(key, "on deck")


def _gameprep_covers(sport: Optional[str]) -> str:
    key = (sport or "").strip().lower()
    return _GAMEPREP_COVERS_BY_SPORT.get(
        key, _GAMEPREP_COVERS_BY_SPORT["waterpolo"]
    )


def _gameprep_coach_html(
    coach_name: str,
    opponent: str,
    gameprep_url: str,
    sport: Optional[str] = None,
) -> str:
    name = escape(_first_name(coach_name))
    opp = escape(opponent)
    surface = _gameprep_surface(sport)
    covers = _gameprep_covers(sport)
    return f"""\
<!doctype html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#111;max-width:560px;margin:0 auto;padding:24px;line-height:1.55">
  <h1 style="font-size:22px;margin:0 0 16px 0;">Your game prep vs {opp} is ready</h1>
  <p>Hi {name},</p>
  <p>The game prep package for your match against <strong>{opp}</strong> is live.
  Open it on your phone on the way to the game, or print it and bring it {escape(surface)}.</p>
  <p style="margin:24px 0;">
    <a href="{escape(gameprep_url)}" style="display:inline-block;background:#0b3d91;color:#fff;text-decoration:none;padding:12px 18px;border-radius:6px;font-weight:600;">View game prep package</a>
  </p>
  <p style="font-size:14px;color:#444;">It covers {escape(covers)}.</p>
  <p style="font-size:13px;color:#777777;margin-top:8px;">The link goes live within 5 minutes of receiving this email.</p>
  <p style="margin-top:32px;">— CoachPrep</p>
  <hr style="border:none;border-top:1px solid #eee;margin:24px 0;">
  <p style="font-size:12px;color:#888;">Plain link, in case the button doesn't work:<br>
    {escape(gameprep_url)}
  </p>
</body></html>
"""


def _gameprep_coach_text(
    coach_name: str,
    opponent: str,
    gameprep_url: str,
    sport: Optional[str] = None,
) -> str:
    first = _first_name(coach_name)
    surface = _gameprep_surface(sport)
    covers = _gameprep_covers(sport)
    return (
        f"Hi {first},\n\n"
        f"Your game prep package for the match against {opponent} is ready.\n\n"
        f"View game prep package: {gameprep_url}\n\n"
        "Open it on your phone on the way to the game, or print it and bring "
        f"it {surface}. It covers {covers}.\n\n"
        "The link goes live within 5 minutes of receiving this email.\n\n"
        "— CoachPrep\n"
    )


def send_gameprep_email(
    coach_name: str,
    coach_email: str,
    opponent: str,
    gameprep_url: str,
    sport: Optional[str] = None,
) -> EmailResult:
    """Send the game-prep coach email.

    Subject: ``CoachPrep — Game Prep vs <opponent> is ready`` (same for every
    sport). Body: a single "View game prep package" link plus a one-line
    summary of what the package covers, phrased in the terminology of the
    sport. Sign-off: "— CoachPrep".

    The ``sport`` argument drives two pieces of copy: the surface noun
    ("bring it on deck" for water polo, "bring it to the field" for
    lacrosse) and the "what it covers" one-liner (5x6 vs EMD, GK vs goalie,
    etc.). Defaults to water polo wording if unspecified so existing
    callers keep the original behavior.
    """
    _init()
    s = get_settings()
    opp_display = (opponent or "Opponent").strip() or "Opponent"
    subject = f"CoachPrep — Game Prep vs {opp_display} is ready"
    params: dict = {
        "from": s.email_from,
        "to": [coach_email],
        "subject": subject,
        "html": _gameprep_coach_html(coach_name, opp_display, gameprep_url, sport),
        "text": _gameprep_coach_text(coach_name, opp_display, gameprep_url, sport),
    }
    if s.email_reply_to:
        params["reply_to"] = [s.email_reply_to]

    try:
        resp = resend.Emails.send(params)
    except Exception as exc:
        log.exception("resend gameprep send failed")
        raise EmailSendError(f"resend send failed: {exc}") from exc

    msg_id = (resp or {}).get("id", "") if isinstance(resp, dict) else getattr(resp, "id", "")
    log.info(
        "gameprep email sent to=%s opponent=%s sport=%s id=%s",
        coach_email, opp_display, (sport or "waterpolo"), msg_id,
    )
    return EmailResult(message_id=msg_id or "", to=coach_email)


def send_ops_lacrosse_manual_email(
    intake_id: str,
    coach_name: str,
    coach_email: str,
    intake_summary: str,
) -> Optional[EmailResult]:
    """Tell ops to manually fulfill a lacrosse intake."""
    _init()
    s = get_settings()
    if not s.ops_notify_email:
        return None

    subject = f"[CoachPrep] lacrosse intake — manual plan needed ({coach_name})"
    body_text = (
        f"A lacrosse intake was just received. Auto-generation is not live "
        f"for lacrosse yet; please prepare and send the plan manually.\n\n"
        f"Intake: {intake_id}\n"
        f"Coach:  {coach_name} <{coach_email}>\n\n"
        f"Intake summary:\n{intake_summary[:4000]}\n"
    )
    try:
        resp = resend.Emails.send({
            "from": s.email_from,
            "to": [s.ops_notify_email],
            "subject": subject,
            "text": body_text,
        })
    except Exception as exc:
        log.exception("ops lacrosse manual email failed: %s", exc)
        return None

    msg_id = (resp or {}).get("id", "") if isinstance(resp, dict) else getattr(resp, "id", "")
    return EmailResult(message_id=msg_id or "", to=s.ops_notify_email)


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

    subject = f"[CoachPrep] pipeline failed at {stage} — {coach_name}"
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
