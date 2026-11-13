# TradeCopier MVP

Mirror every trade from one **leader** futures account to several **follower**
accounts, with per-follower sizing, contract mapping (e.g. NQ → MNQ),
reconciliation, risk rules and a web admin panel.

This MVP runs entirely on a **simulated broker**. It never connects to a real
broker and never sends real orders.

> **Status: all four milestones are done.**
>
> | Milestone | Content | Status |
> |---|---|---|
> | M1 | Broker interface, SimulatedBroker, copy engine, Tradovate stub, tests | ✅ done |
> | M2 | Database, login, REST API, WebSocket, seed data, Docker Compose | ✅ done |
> | M3 | Admin panel (React) | ✅ done |
> | M4 | Risk rules (daily loss limit, max position, trading hours), trading day | ✅ done |

---

## 1. Run it

You need **Python 3.12 or newer** (3.13 works too). Check with `py -0` on Windows.

### Windows with Git Bash (MINGW64)

First time only (the project lives in `E:\14.TradeSyncer` in this example):

```bash
cd /e/14.TradeSyncer/backend
py -3.13 -m venv .venv            # skip if .venv already exists
source .venv/Scripts/activate
pip install -e ".[dev]"           # run again after every update of the project (new packages)
python -m app.cli init            # creates ../.env: asks for an admin username and password
```

Start the server:

```bash
cd /e/14.TradeSyncer/backend
source .venv/Scripts/activate
python -m app
```

Open <http://127.0.0.1:8000> in your browser and log in with the username and
password you chose in `app.cli init`. Stop the server with `Ctrl+C`.

The admin panel is already built (in `frontend/dist`), so **you do not need
Node.js** to run it. Node is only needed if you want to change the panel's code
(see "Developing the admin panel" below).

Tests and the terminal demo:

```bash
pytest -q
python -m app.demo
```

### Windows PowerShell

```powershell
cd E:\14.TradeSyncer\backend
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1      # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -e ".[dev]"
python -m app.cli init
python -m app
```

### macOS / Linux

```bash
cd tradecopier/backend
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python -m app.cli init
python -m app
```

### With Docker (optional)

Requires Docker Desktop. In the project root (the folder with `docker-compose.yml`):

```bash
docker compose run --rm setup     # first time: creates .env (asks for the admin password)
docker compose up -d --build      # start in the background
docker compose logs -f app        # watch the log (Ctrl+C stops watching, not the app)
docker compose down               # stop
```

The database lives in the Docker volume `tradecopier-data`. The port is published
on `127.0.0.1` only. Python and Docker use separate databases.

### Admin panel pages

| Page | What you can do |
|---|---|
| **Dashboard** | Copier status, pause/resume, the red **KILL SWITCH**, leader position and P&L, every follower's position vs. target with sync status, today's P&L, latency, recent alerts. Updates live. |
| **Accounts** | Add, edit, enable/disable and delete accounts, choose leader/follower and broker type, test the connection. Broker credentials are write-only. |
| **Copy settings** | Per follower: multiplier, max contracts, copy SL/TP on/off, auto-correct vs. alert-only, contract mapping (e.g. NQ → MNQ 10:1) and a sizing preview. |
| **Risk** | Per follower: daily loss limit, max position, trading hours (New York time). Shows today's state of every rule (loss used, locked, exits only), lets you re-enable a locked follower, and lists the triggered rules. |
| **Trade log** | Every leader event with the follower orders it caused: status, fill, latency, errors. Filters and CSV export. |
| **System log** | Connections, reconciliation fixes, rejections, kill switch and rule events. Filter by level, category, account, text. |
| **Simulator** | Buy/sell on the leader (market, limit, stop, with SL/TP), close positions, modify/cancel leader orders, move prices, inject failures (disconnect, reject, partial fill, latency, duplicate events), and start a new trading day on demand. |

A quick test: open **Simulator**, tick *Attach stop loss / take profit*, click
**Buy 1 NQ**, then look at the **Dashboard** and the **Trade log**. Then use
*Failure injection* (e.g. reject the next order on F1) and buy again: the trade
log shows the rejection and, about 3 seconds later, the reconciliation fix.

### API docs

All actions are also available as a REST API: <http://127.0.0.1:8000/docs>
(log in with `POST /api/auth/login` first).

### What is in `.env`

`python -m app.cli init` writes it for you; `.env.example` explains every line.
It holds the admin username, a **hash** of the admin password (never the
password itself), the session secret and the key that encrypts broker
credentials. Keep `.env` private and do not lose `ENCRYPTION_KEY`.

### Developing the admin panel (optional, needs Node.js 18+)

```bash
cd /e/14.TradeSyncer/frontend
npm install
npm run dev        # http://localhost:5173, talks to the Python server on port 8000
npm run build      # writes frontend/dist, which the Python server serves
```

---

## 2. How the copier works

### Seed accounts used by tests and the demo

| Account | Settings |
|---|---|
| leader | trades NQ |
| f1 | 1× NQ |
| f2 | 0.5× NQ |
| f3 | NQ → MNQ at 10:1 |

### Sizing: the follower tracks a *target position*

```
target = sign(leader position) × min( floor(|leader position| × multiplier × ratio), max contracts )
```

Orders are the difference between the target before and after a leader trade.
With 0.5× and a leader that buys 1, then 1 more: the first buy gives 0
(skipped), the second gives 1, because 0.5 × 2 = 1. Rounding is exact
(decimal arithmetic), so 0.29 × 100 is 29, never 28.

### What gets copied

| Leader does | Follower gets |
|---|---|
| Market order fills | Market order for the change in target (copied on the **fill**) |
| Places a limit/stop entry | The same resting order, sized by the target rule |
| Modifies / cancels it | Same modify / cancel |
| SL/TP (bracket or standalone) | Same SL/TP, **never larger than the follower's own position** |
| Partial close | Follower reduces; its SL/TP shrink to the new size |

### Risk rules (per follower, all optional)

| Rule | What happens |
|---|---|
| **Daily loss limit** | Counts realized + unrealized P&L of the trading day and is checked on every price change. When it is reached the follower is **flattened** and **stops copying** until the next trading day. You can re-enable it by hand earlier (Dashboard or Risk page); it then gets a fresh allowance of the same size, measured from its current P&L. The lock survives a restart. |
| **Max position** | Hard cap per symbol. The follower's target is capped, so it simply holds less than the leader (it is not blocked and not "out of sync"). Resting entry orders plus the position never add up to more than the cap. |
| **Trading hours** | Entered in New York time, with weekdays. Outside the window **new entries are not copied; exits still are** (closing, SL/TP changes), so an open position stays managed. When the window opens while the leader is already in a trade, the follower waits for the next trade. |

The **trading day** follows the CME session: it rolls at **17:00 Chicago time**
(midnight in Belgrade for most of the year). At the roll, daily P&L starts at
zero, risk counters are cleared and locked followers copy again. Positions held
through the roll are counted from the price at the roll. The Simulator page has
a button to trigger the roll by hand for testing.

A quick test of the daily loss limit: on **Risk** set a limit of 300 for F1,
on **Simulator** buy 2 NQ and press the `− 10` price button (F1 is then at
−$400). The Dashboard shows F1 flat with "Loss limit hit" and a **Re-enable**
button; the other followers keep their positions.

### Safety mechanisms

* **Idempotency** – every broker event has a unique id; duplicates are ignored.
  Positions are computed from cumulative fills per order, so the same fill
  arriving twice (event + snapshot) is never counted twice.
* **Per-follower queues** – one slow or offline follower never delays the others.
* **Reconciliation** (default every 3 s) – compares each follower's actual
  position with its target using a fresh broker snapshot.
  * `auto_correct`: sends a market order for the difference (max 3 attempts,
    then it only alerts until you press *retry sync*).
  * `alert_only`: only writes an alert to the system log.
* **Rejected / failed orders** are logged with the reason and fixed by
  reconciliation (auto-correct followers).
* **Partial fills** – the working remainder counts as "pending", so nothing is
  ordered twice; a remainder stuck for more than 5 s is cancelled and replaced.
* **Reconnect with backoff** (0.5 s, 1 s, 2 s … max 30 s). After reconnecting,
  the copier resyncs from the broker and reconciles.
* **Leader closes while a follower order is pending** – a mirrored order that
  did not fill with the leader is cancelled before the follower is adjusted, so
  the follower never ends up reversed.
* **Joining** – after pause/resume, kill switch or a manual flatten, a follower
  does not jump into a trade that is already open. It waits until the leader
  is flat and copies from the next trade.
* **Kill switch** – stops copying and flattens all followers. A follower that is
  offline at that moment is flattened as soon as it reconnects.
* **Latency** is measured from receiving the leader's fill to the follower's
  order acknowledgement.

---

## 3. Server, API and security (M2)

* **One process**: FastAPI and the copy engine run in the same Python process
  (one uvicorn worker), so copying is not slowed down by inter-process traffic.
* **SQLite database** (`backend/data/tradecopier.db`): accounts, copy settings,
  contract mappings, risk rules, trade log (leader events + follower orders),
  system log, copier state. The engine never waits for the disk: records are
  written in batches by a background task.
* **Seed data** on first start: `Leader (sim)`, `F1 - 1x NQ`,
  `F2 - 0.5x NQ` (alert-only mode), `F3 - NQ to MNQ 10:1`.
* **Changes apply live**: copy settings update a running follower immediately;
  adding/removing accounts or changing the leader rebuilds the copier (simulated
  positions are kept).
* **Copier state survives restarts**: if you paused it or hit the kill switch,
  it starts paused.
* **Login**: one admin, username + argon2 password hash from `.env`. Sessions
  are server-side (logout really ends them), cookie is `HttpOnly` and
  `SameSite=Strict`. 5 wrong passwords lock the login for 60 s.
* **Broker credentials** are encrypted with Fernet (key from `.env`) before they
  are stored, are never returned by the API and are masked in every log line.
* **Localhost only**: the server binds to `127.0.0.1`; Docker publishes the port
  on `127.0.0.1` only. The WebSocket also checks the browser origin.

| Endpoint group | Paths |
|---|---|
| Auth | `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/me` |
| Dashboard | `GET /api/status`, `GET /api/meta` |
| Copier | `POST /api/copier/pause`, `/resume`, `/kill`, `/reconcile` |
| Accounts | `GET/POST /api/accounts`, `GET/PUT/DELETE /api/accounts/{id}`, `POST .../test-connection`, `.../flatten`, `.../retry-sync`, `GET .../orders` |
| Copy settings | `GET/PUT /api/accounts/{id}/copy-settings` |
| Risk rules | `GET/PUT /api/accounts/{id}/risk-rules`, `POST /api/accounts/{id}/risk/unlock`, `GET /api/risk/events` (`?day=today`) |
| Trade log | `GET /api/trades` (filters: since, until, follower_id, status, action, symbol), `GET /api/trades/export.csv` |
| System log | `GET /api/logs` (filters: level, category, account_id, search) |
| Simulator | `GET /api/sim/state`, `POST /api/sim/order`, `/close`, `/price`, `/walk`, `/new-day`, `/orders/{id}/modify`, `/orders/{id}/cancel`, `/accounts/{id}/failures` |
| Live updates | WebSocket `/ws` (snapshot, leader_event, follower_order, log) |

---

## 4. Simulated broker

* One price per index: NQ/MNQ, ES/MES, YM/MYM and RTY/M2K share a price, so a
  leader NQ order and a follower MNQ order trigger together.
* Random walk (optional) plus manual price setting.
* Real contract values: NQ $20/pt, MNQ $2/pt, ES $50/pt, MES $5/pt, YM $5/pt,
  MYM $0.50/pt, RTY $50/pt, M2K $5/pt.
* Market, limit, stop, bracket (SL/TP as an OCO pair that follows the position
  size), modify, cancel, flatten.
* Failure injection (used by tests, and by the simulator page in M3):

  | Method | Effect |
  |---|---|
  | `inject_disconnect(duration_s)` | drop the connection; events during the outage are lost |
  | `reject_next_orders(n, reason)` | reject the next n orders |
  | `partial_fill_next_orders(n, ratio, remainder_delay)` | fill only part of the next n orders |
  | `partial_fill_order(order_id, ...)` | make a resting order fill partly |
  | `set_latency(ms)` | delay every request |
  | `duplicate_next_events(n)` | deliver the next n events twice |

Simulator state lives in memory and resets when the app restarts.

---

## 5. Project structure

```
TradeSyncer/
├─ README.md · .env.example · Dockerfile · docker-compose.yml
├─ CLAUDE.md                   entry point for AI assistants
├─ docs/claude/                project brief, architecture, glossary, status, working agreement
├─ frontend/                   (see below)
└─ backend/
   ├─ pyproject.toml
   ├─ app/
   │  ├─ __main__.py           python -m app  (starts the server)
   │  ├─ main.py               FastAPI app, lifespan, sessions
   │  ├─ cli.py                python -m app.cli init / hash-password / gen-key
   │  ├─ config.py             settings from .env
   │  ├─ demo.py               terminal demo (M1)
   │  ├─ api/                  REST routes + WebSocket
   │  ├─ services/             runtime (engine lifecycle), DB/WebSocket sinks, WS hub
   │  ├─ db/                   SQLAlchemy models, seed data, helpers
   │  ├─ core/                 contracts, trading day (CME session), security, logging
   │  ├─ brokers/
   │  │  ├─ base.py            BrokerAdapter interface + data models
   │  │  ├─ sim/               SimMarket (prices) and SimulatedBroker
   │  │  └─ tradovate/         TradovateAdapter stub (no network calls)
   │  └─ engine/
   │     ├─ copy_engine.py     leader subscription, dedup, controls (pause, kill switch)
   │     ├─ follower_worker.py per-follower copying, SL/TP sync, reconciliation
   │     ├─ sizing.py          target-position sizing (pure functions)
   │     ├─ reconciler.py      reconciliation decision (pure function)
   │     ├─ state.py           duplicate-safe position bookkeeping
   │     ├─ connection.py      reconnect with exponential backoff
   │     ├─ risk.py            risk rules: daily loss lock, max position, trading hours
   │     ├─ config.py          follower and engine settings
   │     └─ events.py          trade-log / system-log records
   └─ tests/
      ├─ unit/                 sizing, rounding, mapping, reconciliation, simulator
      ├─ integration/          end-to-end copy scenarios with the SimulatedBroker
      └─ api/                  login, security, accounts, trade flow, WebSocket

frontend/                      admin panel: React + Vite + TypeScript + Tailwind
├─ dist/                       built panel served by the Python server (shipped)
└─ src/
   ├─ main.tsx                 routes, providers
   ├─ lib/                     API client, auth, live WebSocket, formatting, types
   ├─ components/              layout, UI kit, status badges
   └─ pages/                   Dashboard, Accounts, CopySettings, Risk, TradeLog, SystemLog, Simulator
```

## 6. Adding a real broker later

Implement `app.brokers.base.BrokerAdapter` (connect, positions, orders, place /
modify / cancel, flatten, event stream). `app/brokers/tradovate/adapter.py`
shows where each Tradovate call would go. The copy engine needs no changes.

## 7. Git

The project lives at <https://github.com/toninojic/TradeSyncer> (branch `main`).

```bash
git status            # what changed locally
git log --oneline     # history
git pull              # get the latest version from GitHub
```

Not in git on purpose: `.env` (your password hash and keys), `backend/data/`
(the local database), `.venv/` and `node_modules/`. After cloning on another
computer, run the setup from section 1 again (`pip install`, `python -m app.cli init`).

## 8. For AI assistants

Start with [`CLAUDE.md`](CLAUDE.md), then [`docs/claude/`](docs/claude/README.md):
project brief, architecture and engine invariants, glossary, status and roadmap,
working agreement, and notes on connecting real brokers.
