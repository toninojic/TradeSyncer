# CLAUDE.md

Entry point for AI assistants working in this repository. Read this first, then
the files in [`docs/claude/`](docs/claude/README.md).

## What this is

A trade copier for US index futures: every trade on one **leader** account is
mirrored to several **follower** accounts with per-follower sizing. It has a
Python backend (FastAPI + asyncio copy engine + SQLite) and a React admin
panel. It is a single-user tool for the owner's own accounts.

**The MVP is simulation only.** There is a fully working `SimulatedBroker` and a
`TradovateAdapter` stub that makes no network calls. Do not add real broker
connections or anything that could send a real order unless the owner
explicitly asks for it.

## Rules that always apply

1. **Language:** talk to the owner in Serbian (Latin script). All code,
   comments, UI text, commit messages and docs in this repo are in English.
2. **Tone:** explain plainly, in normal adult/technical language. No
   "explain like I'm five" voice and no analogies.
3. **Safety first in the engine:** a follower must never end up with a position
   the leader does not have (reversed, doubled, orphaned stop). When in doubt,
   prefer doing nothing and alerting over sending an order.
4. **Tests are part of the change.** Run them before saying something works:
   `cd backend && pytest -q` (186 tests, about 25 s).
5. **The built admin panel is committed.** After changing anything in
   `frontend/src`, run `npm run build` in `frontend/` and commit `frontend/dist`
   too, so the owner can run the app without Node.js.
6. **Never commit secrets or local data:** `.env`, `backend/data/`, `*.db`.
   They are in `.gitignore`; keep it that way.
7. **Work in milestones** and stop after each one so the owner can try it.
   Tell them exactly how to start and what to click.

## Commands

```bash
# backend (Python 3.12+; the owner uses 3.13 with Git Bash on Windows)
cd backend
source .venv/Scripts/activate        # Windows Git Bash; .venv/bin/activate elsewhere
pip install -e ".[dev]"
python -m app.cli init               # first time: creates ../.env
python -m app                        # server + admin panel on http://127.0.0.1:8000
pytest -q                            # all tests
python -m app.demo                   # terminal demo of the copy engine

# frontend (only when changing the panel; Node 18+)
cd frontend
npm install
npm run dev                          # http://localhost:5173, proxies to :8000
npm run build                        # writes frontend/dist (commit it)
```

## Where things are

| Path | Content |
|---|---|
| `backend/app/brokers/` | `BrokerAdapter` interface, `sim/` (market + simulated broker), `tradovate/` (stub) |
| `backend/app/engine/` | copy engine: sizing, per-follower workers, reconciliation, reconnect, risk rules |
| `backend/app/services/` | runtime (engine lifecycle), DB and WebSocket sinks |
| `backend/app/api/` | REST routes and the `/ws` WebSocket |
| `backend/app/db/` | SQLAlchemy models, seed data |
| `backend/tests/` | `unit/`, `integration/` (engine scenarios), `api/` |
| `frontend/src/` | React admin panel (pages, components, lib) |
| `docs/claude/` | project brief, architecture, glossary, status, working agreement, live-broker notes |

## Before you change the copy engine

Read [`docs/claude/architecture.md`](docs/claude/architecture.md), section
"Copy engine invariants". Most bugs found so far came from breaking one of
them, and each has a regression test in `backend/tests/integration/`.

## Keep the context current

At the end of a milestone or a significant change, update
[`docs/claude/status-and-roadmap.md`](docs/claude/status-and-roadmap.md) and,
if a design decision changed, `architecture.md`.
