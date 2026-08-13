# CLAUDE.md — assistant

Project-specific instructions for this repo. These merge with the global
behavioral guidelines in `~/CLAUDE.md` (Think Before Coding, Simplicity First,
Surgical Changes, Goal-Driven Execution) — follow those too.

## What this is

A personal-assistant Telegram bot. Core entry point is `assistant_step4.py`.
Supporting modules cover habits, study planning, reminders, daily digests,
location/visit detection, weather, and university grade scraping.

## Stack

- Language: Python 3 (venv at `./venv` — `source venv/bin/activate`)
- Bot: `python-telegram-bot` 21.x
- LLM: `anthropic` SDK — default to the latest Claude models
- Backend / DB: Supabase (`supabase` client); schema lives in the `*.sql` files
- Web server: FastAPI + uvicorn (OAuth callback in `oauth_server.py`)
- Google APIs: `google-auth`, `google-auth-oauthlib`, `google-api-python-client`
- Browser automation: Playwright (kept container-safe for Railway)
- Scheduling: `croniter`; date parsing: `dateparser`
- Config: `python-dotenv` (`.env`), `PyYAML`
- Tests: `pytest` (`tests/`)
- Deploy: Docker → Railway

## Conventions

- Services hold business logic and data access (`*_service.py`); repositories
  wrap DB access (`*_repository.py`). Match this split when adding code.
- Use async/await for network/IO calls; don't block the bot event loop.
- No hardcoded secrets — read from `.env` / environment.
- SQL schema changes go in the matching `*.sql` file alongside the code.
- Run tests before declaring a change done: `pytest`.
