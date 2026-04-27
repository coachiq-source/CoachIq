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
