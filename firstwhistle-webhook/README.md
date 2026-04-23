# FirstWhistle Webhook Pipeline

Receives coach intake submissions from Formspree, generates a weekly practice
plan + one-page deck sheet with the Claude API, commits both files to GitHub,
and emails the coach their links via Resend.

## Architecture

```
Formspree form submit
        │ POST /webhook/formspree
        ▼
FastAPI endpoint  ─── verifies shared secret, parses & validates intake
        │ returns 202 immediately
        ▼
BackgroundTask (same process)
        │
        ├── app/claude_client.py   → Messages API call (system prompt + intake JSON)
        ├── app/parser.py          → extracts 2 HTML docs from response
        ├── app/github_deploy.py   → commits coaches/<slug>/plan.html + deck.html
        └── app/email_send.py      → emails coach via Resend
```

## Project layout

```
firstwhistle-webhook/
├── app/
│   ├── main.py            FastAPI app + /webhook/formspree + /health
│   ├── config.py          env loading, system prompt loader, slugify
│   ├── intake.py          Formspree payload normalization + validation
│   ├── claude_client.py   Anthropic SDK wrapper
│   ├── parser.py          extract 2 HTML docs from Claude response
│   ├── github_deploy.py   Contents API commits
│   ├── email_send.py      Resend coach + ops emails
│   └── pipeline.py        end-to-end orchestrator
├── scripts/
│   ├── sample_intake.json sample Formspree-style payload
│   └── test_webhook.py    curl-like tester (JSON or form-encoded)
├── tests/
│   └── test_intake.py     unit tests for intake + parser
├── firstwhistle_master_system_prompt.md   ← replace with real prompt
├── requirements.txt
├── Procfile               (Railway/Render)
├── railway.toml
├── .env.example
└── README.md
```

## Setup

```bash
cd firstwhistle-webhook
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Fill in all required vars — the server fails fast on startup if any are missing.

# Drop the real master system prompt into place:
#   firstwhistle_master_system_prompt.md
```

## Run locally

```bash
uvicorn app.main:app --reload --port 8000
```

Health check: <http://127.0.0.1:8000/health>
API docs:     <http://127.0.0.1:8000/docs>

## Test with a sample payload

```bash
# JSON (recommended)
python scripts/test_webhook.py

# Form-urlencoded (matches Formspree's default)
python scripts/test_webhook.py --form

# Override URL
python scripts/test_webhook.py --url https://fw-webhook.up.railway.app/webhook/formspree
```

## Environment variables

The 5 required vars match the Railway Shared Variables exactly. Everything
else is optional with sensible defaults. See `.env.example`.

**Required**

| Var | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Claude auth |
| `GITHUB_TOKEN` | PAT with `Contents: read/write` on the target repo |
| `GITHUB_REPO` | Combined `owner/repo` (e.g. `coachiq-source/CoachIq`) — split at startup |
| `RESEND_API_KEY` | Resend auth |
| `COACH_EMAIL_FROM` | Sender address (must be verified in Resend for prod) |

**Optional**

| Var | Default | Purpose |
|---|---|---|
| `CLAUDE_MODEL` | `claude-sonnet-4-20250514` | Model id |
| `CLAUDE_MAX_TOKENS` | `16000` | Response cap |
| `GITHUB_BRANCH` | `main` | Target branch for commits |
| `PUBLIC_BASE_URL` | `https://<owner>.github.io/<repo>` | Public base URL for coach pages |
| `WEBHOOK_SECRET` | *(unset → auth disabled)* | Shared secret — accepted via `X-Webhook-Secret` header or `webhook_secret` form field |
| `EMAIL_REPLY_TO` | — | Reply-to header for coach emails |
| `OPS_NOTIFY_EMAIL` | `johnmaxwell.kelly@gmail.com` | Pipeline-failure alerts land here |
| `HOST` / `PORT` / `LOG_LEVEL` / `SYSTEM_PROMPT_PATH` | | Server basics |

## Wiring Formspree

Formspree doesn't sign webhooks. Two safe options:

1. **Hidden field** — add a hidden input named `webhook_secret` to the form,
   value = your `WEBHOOK_SECRET`. Formspree will forward it in the payload.
2. **Webhook forwarder** — Formspree → forwarder (e.g. Zapier/n8n) that
   injects an `X-Webhook-Secret` header before POSTing here.

Either works. The server accepts both, whichever is present.

## GitHub repo expectations

- `GITHUB_REPO` must exist and have a default branch (`GITHUB_BRANCH`).
- The server writes to:

      coaches/<slug>/week<n>-plan.html
      coaches/<slug>/week<n>-deck.html

  The slug is derived from the coach's name alone (e.g. "Magnus Sims" →
  `magnus-sims`). The week number is discovered at deploy time by listing
  the existing `coaches/<slug>/` directory and incrementing the highest
  `week<n>` prefix; the first deploy for a new coach starts at week 1.
- If you're serving via GitHub Pages from `main`, `PUBLIC_BASE_URL` is
  derived automatically as `https://<owner>.github.io/<repo>`. Override
  `PUBLIC_BASE_URL` if you're using a custom domain or a `/docs`/`gh-pages`
  Pages source.

## Deploying to Railway

1. `railway init` → new service from this directory.
2. Set every var from `.env.example` in the Railway dashboard.
3. Push — `railway.toml` tells Railway how to start and healthcheck.
4. Grab the public URL and plug it into Formspree.

## Running tests

```bash
python tests/test_intake.py
# or:
pytest tests/
```

The unit tests cover intake normalization + HTML extraction and do not
require any API keys.

## Future sessions

- Session 2: wire the real master system prompt + integration test against Claude.
- Session 3: end-to-end smoke test with real GitHub + Resend.
- Session 4: retries, idempotency by `intake_id`, and admin dashboard.
