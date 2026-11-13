# Status and roadmap

Last updated: 2026-11-13 (after milestone 4). All four planned milestones are
done; the MVP as specified is complete.

## Done

| Milestone | Content | Verified by |
|---|---|---|
| M1 | `BrokerAdapter` interface, `SimulatedBroker` with failure injection, copy engine (sizing, linked orders, SL/TP sync, reconciliation, reconnect, kill switch, per-account flatten), Tradovate stub, terminal demo | unit + integration tests |
| M2 | SQLite persistence, admin login, credential encryption, log redaction, REST API, WebSocket, seed data, `python -m app`, `python -m app.cli init`, Dockerfile + compose | API tests, manual run with curl |
| M3 | React admin panel: Login, Dashboard, Accounts, Copy settings, Risk, Trade log (filters, live, CSV), System log, Simulator | Playwright click-through of every page in Chromium, desktop and phone width |
| M4 | Risk rules enforced: daily loss limit (flatten + lock, manual re-enable, auto re-enable at the day roll, lock persisted), max position (cap + entry room), trading hours (reduce-only), CME trading day, simulator "new trading day", Risk page live state | unit + integration + API tests, Playwright run of the risk flows |

Test count: 186 (`cd backend && pytest -q`, about 25 s).

Measured copy latency in the full server on the simulator: about 1-2 ms
typical, 10-16 ms for the first trades after a cold start.

## Known gaps and caveats

* **Docker image build is unverified.** The compose file is syntactically
  valid and the Dockerfile steps were replayed by hand, but the image itself
  was never built because the build sandbox could not reach Docker Hub.
* **No database migrations.** Tables are created with `create_all`, which adds
  new tables (M4's `risk_state` appears automatically) but never alters an
  existing one. Adding a column to an existing table needs a migration step or
  deleting `backend/data/tradecopier.db` (dev data only).
* **Simulator state is in memory.** Positions, orders and day P&L reset on
  restart; settings, logs and the risk lock persist.
* **Tradovate adapter is a stub.** "Test connection" on such an account
  reports that it is a stub.
* **Leader events during a pause are not replayed.** After resume, followers
  wait for the leader to be flat (by design).
* **Follower limit entries that could not be placed while the follower was
  offline are not re-placed**; if the leader's order later fills, the follower
  is brought to target with a market order.
* **Risk events are deduplicated per follower, rule and trading day.** The
  database holds the first occurrence; the count of later hits lives in memory
  (`triggered_today`) and is lost on restart.
* **A follower in `WAIT_FOR_FLAT` that still holds a position does not follow
  exits** until the leader is flat (then reconciliation closes it). The
  trading-hours rule avoids this state by using reduce-only mode instead.
* **Daily P&L for real brokers** would come from the adapter's `get_account()`
  (polled once per second by the runtime); only the simulator is implemented.
* The latency panel on the Dashboard is per browser session (it is built from
  live WebSocket records, not from the database).
* After "Start new trading day now" in the simulator the trading-day label does
  not change, so events from before the simulated roll still show under
  "Today only".

## After the MVP (ideas, not agreed with the owner)

* A real broker adapter once the owner knows where the accounts are (see
  [`live-brokers.md`](live-brokers.md)). Do this against a demo environment
  first.
* Notifications (e-mail/Telegram) for errors and risk breaches.
* Alembic migrations.
* Persisting simulator state or adding a replay mode.
* Trailing drawdown rule (prop-firm style) next to the daily loss limit.
