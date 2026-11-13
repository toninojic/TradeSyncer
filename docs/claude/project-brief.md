# Project brief

## Goal

One **leader** trading account; every trade on it is mirrored automatically to
several **follower** accounts, each with its own size settings. A web admin
panel shows the state live and lets the owner control everything.

The owner trades US index futures (NQ, ES, YM, RTY and the micros MNQ, MES,
MYM, M2K), usually by hand through TradingView. The tool is for the owner's own
accounts only: no multi-user support, no billing.

## Hard constraints

* **Broker-agnostic.** The owner does not know yet whether the accounts are on
  Tradovate, Rithmic or MT5. Everything goes through the `BrokerAdapter`
  interface; the engine never knows which broker it talks to.
* **Simulation only in the MVP.** A complete `SimulatedBroker` runs everything.
  `TradovateAdapter` is a stub with TODOs and no network calls. No real orders.
* **Security.** Single admin login (username and password hash in `.env`,
  server-side sessions). Broker credentials encrypted at rest (Fernet key in
  `.env`), never returned by the API, masked in logs. Server binds to
  `127.0.0.1`.

## Tech stack (agreed with the owner)

* Backend: Python 3.12+, FastAPI, asyncio, SQLAlchemy 2 (async) + SQLite, pytest
* Frontend: React 18, Vite 5, TypeScript, Tailwind 3 (small in-repo UI kit)
* Live updates: WebSocket
* One process: the copy engine runs inside the FastAPI process (one worker)
* Docker Compose file exists; the Python way of running is the primary one

## Required features

| Area | Requirement |
|---|---|
| Sizing | follower target = `floor(leader position x multiplier x ratio)`, capped at max contracts; nothing sent when it rounds to 0 |
| Mapping | per follower, e.g. NQ -> MNQ at 10:1 (same index family only) |
| Copying | entries, exits, partial closes, limit/stop orders and bracket SL/TP incl. modify and cancel |
| Idempotency | every leader event has a unique id; duplicates are ignored |
| Reconciliation | every 3 s compare expected vs actual follower position; per follower either auto-correct or alert only |
| Failure handling | rejected orders, partial fills, reconnect with backoff, leader closing while a follower order is pending |
| Latency | measured from receiving the leader fill to the follower order ack |
| Risk | per follower: daily loss limit (flatten + lock until the next trading day), max position (hard cap), allowed trading hours (exits only outside) |
| Controls | pause/resume, global kill switch (flatten all followers, stop copying), per-account flatten |
| Admin panel | Dashboard, Accounts, Copy settings, Risk, Trade log (filters, CSV), System log, Simulator |
| Seed data | 1 leader + 3 simulated followers: 1x NQ, 0.5x NQ, NQ -> MNQ 10:1 |

## Decisions the owner made

| Question | Decision |
|---|---|
| Sizing when scaling in/out | **Position target.** Follower size follows the leader's total position, not each order separately (0.5x of 1 + 1 contracts is 1, not 0 + 0). |
| What counts toward the daily loss limit | **Realized + unrealized** P&L, checked on every price change. |
| Trading day and time zone | **CME session.** The day resets at 17:00 America/Chicago. Trading hours are entered in New York time (ET). |
| After a daily-loss breach | Follower is flattened and copying is disabled; it **re-enables automatically at the next trading day**, and can be re-enabled by hand earlier. |

## Out of scope for the MVP

Multiple users, payments, real broker connections, mobile app, trade
journaling, Pine Script / webhooks.

## Design decisions made during the build (owner informed)

* Market orders are copied when the leader **fills**; resting limit/stop orders
  and SL/TP are mirrored as real resting orders on the follower.
* A follower's SL/TP is never larger than the follower's own position.
* After pause/resume, kill switch or a manual flatten, a follower does **not**
  jump into a trade that is already open; it waits until the leader is flat.
* The kill switch never touches the leader account.
* **Max position caps, it does not block.** A follower whose leader exceeds the
  cap holds exactly the cap and counts as in sync.
* **Outside trading hours a follower is reduce-only**: exits are copied, entries
  are not, and it does not join an open trade when the window opens.
* **Manual re-enable after a daily-loss lock gives a fresh allowance** of the
  same size, measured from the follower's P&L at that moment.
* The simulator carries positions across the daily reset at the reset price
  (like a daily settlement), so yesterday's open loss does not count today.
* The built admin panel (`frontend/dist`) is committed so the app runs without
  Node.js.
