# Status and roadmap

Last updated: 2026-11-13 (after milestone 3).

## Done

| Milestone | Content | Verified by |
|---|---|---|
| M1 | `BrokerAdapter` interface, `SimulatedBroker` with failure injection, copy engine (sizing, linked orders, SL/TP sync, reconciliation, reconnect, kill switch, per-account flatten), Tradovate stub, terminal demo | unit + integration tests |
| M2 | SQLite persistence, admin login, credential encryption, log redaction, REST API, WebSocket, seed data, `python -m app`, `python -m app.cli init`, Dockerfile + compose | API tests, manual run with curl |
| M3 | React admin panel: Login, Dashboard, Accounts, Copy settings, Risk (form + triggered list), Trade log (filters, live, CSV), System log, Simulator | Playwright click-through of every page in Chromium, desktop and phone width |

Test count: 132 (`cd backend && pytest -q`).

Measured copy latency in the full server on the simulator: about 1-2 ms
typical, 10-16 ms for the first trades after a cold start.

## Next: M4 (not started)

Risk rules are stored and editable (`risk_rules` table, Risk page) but **not
enforced yet**. The engine already has the hook: `RiskGuard.check_order()` in
`engine/risk.py`, called from `FollowerWorker._send()`; a returned reason marks
the order `blocked`.

Planned work:

1. `RiskManager` implementing `RiskGuard`, loaded from the DB and updated live
   when rules change.
2. **Daily loss limit** on realized + unrealized P&L, checked on price changes
   and fills. On breach: flatten the follower, disable its copying, write a
   `risk_events` row and a system log entry. Re-enable automatically at the
   next trading day; manual re-enable allowed earlier.
3. **Max position** per symbol: block (or reduce) orders that would exceed it,
   including reconciliation corrections. Orders that reduce exposure are always
   allowed.
4. **Trading hours** in New York time with weekday selection: block new
   exposure outside the window; exits always pass.
5. **Trading day** helper: CME session rolling at 17:00 America/Chicago; reset
   the simulator's daily P&L at the boundary (`SimulatedBroker.reset_day`).
6. Risk page: show today's triggered rules and the follower's current state
   (limit used, disabled until ...).
7. Tests: unit tests for each rule and the day boundary; integration test for a
   daily-loss breach; API tests.
8. Polish: README final pass, any UI rough edges found by the owner.

## Known gaps and caveats

* **Docker image build is unverified.** The compose file is syntactically
  valid and the Dockerfile steps were replayed by hand, but the image itself
  was never built because the build sandbox could not reach Docker Hub.
* **No database migrations.** Tables are created with `create_all`. A schema
  change in M4 needs a small migration step or a note to delete
  `backend/data/tradecopier.db` (dev data only).
* **Simulator state is in memory.** Positions and orders reset on restart;
  settings and logs persist.
* **Risk rules are not enforced** until M4 (see above).
* **Tradovate adapter is a stub.** "Test connection" on such an account
  reports that it is a stub.
* **Leader events during a pause are not replayed.** After resume, followers
  wait for the leader to be flat (by design).
* **Follower limit entries that could not be placed while the follower was
  offline are not re-placed**; if the leader's order later fills, the follower
  is brought to target with a market order.
* The latency panel on the Dashboard is per browser session (it is built from
  live WebSocket records, not from the database).

## After the MVP (ideas, not agreed)

* A real broker adapter once the owner knows where the accounts are (see
  [`live-brokers.md`](live-brokers.md)).
* Persisting simulator state or adding a replay mode.
* Notifications (e-mail/Telegram) for errors and risk breaches.
* Alembic migrations.
