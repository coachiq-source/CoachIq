#!/usr/bin/env python3
"""Fire a sample Formspree-style payload at a local webhook server.

Usage:
    python scripts/test_webhook.py                         # JSON body
    python scripts/test_webhook.py --form                  # form-urlencoded
    python scripts/test_webhook.py --url http://host:port  # override target
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "scripts" / "sample_intake.json"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default=os.environ.get("WEBHOOK_URL", "http://127.0.0.1:8000/webhook/formspree"))
    p.add_argument("--form", action="store_true", help="Send as form-urlencoded instead of JSON")
    p.add_argument("--secret", default=os.environ.get("WEBHOOK_SECRET"), help="Override webhook secret (otherwise uses the one in sample_intake.json)")
    p.add_argument("--payload", type=Path, default=SAMPLE)
    args = p.parse_args()

    if not args.payload.exists():
        print(f"Missing payload file: {args.payload}", file=sys.stderr)
        return 2

    data = json.loads(args.payload.read_text(encoding="utf-8"))

    headers = {}
    if args.secret:
        headers["X-Webhook-Secret"] = args.secret
        data.pop("webhook_secret", None)  # prefer header if both supplied

    if args.form:
        r = httpx.post(args.url, data=data, headers=headers, timeout=30.0)
    else:
        r = httpx.post(args.url, json=data, headers=headers, timeout=30.0)

    print(f"POST {args.url} -> {r.status_code}")
    print(r.text)
    return 0 if 200 <= r.status_code < 300 else 1


if __name__ == "__main__":
    sys.exit(main())
