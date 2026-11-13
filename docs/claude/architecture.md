# Architecture

## Big picture

```
Browser (React admin panel)
   |  REST /api/*  (actions)            ^  WebSocket /ws (snapshot <= 4x/s, log + trade records)
   v                                    |
FastAPI app (one process, one worker) --+
   |
   +- Runtime (services/runtime.py)
   |    owns: SimMarket, broker adapters, CopyEngine, DbSink, WsHub
   |
   +- CopyEngine (engine/copy_engine.py)
   |    leader adapter --events--> dedup --> leader state --> one queue per follower
   |                                                           FollowerWorker x N --> follower adapter
   |
   +- SQLite (SQLAlchemy async, WAL) written in batches by a background task
```

Everything runs on one asyncio event loop. The engine keeps its state in
memory, which is why the server must run with a single worker.

## Backend modules

| Module | Responsibility |
|---|---|
| `brokers/base.py` | `BrokerAdapter` ABC and the data models (`Order`, `Position`, `Fill`, `BrokerEvent`, enums, errors, `EventStream`) |
| `brokers/sim/market.py` | `SimMarket`: one price per index family, optional random walk, manual `set_price` / `move` |
| `brokers/sim/broker.py` | `SimulatedBroker`: paper matching, brackets/OCO, P&L, failure injection |
| `brokers/tradovate/adapter.py` | Stub. Every call raises; comments describe the real integration |
| `core/contracts.py` | Contract specs (point value, tick), tick rounding, P&L |
| `core/security.py` | argon2 password hashing, Fernet `CredentialCipher`, secret redaction for logs |
| `engine/sizing.py` | Pure sizing functions (decimal floor, targets, order delta) |
| `engine/state.py` | `PositionBook` (duplicate-safe positions), `LeaderBook` (entry/exit classification), `Deduper` |
| `engine/reconciler.py` | Pure `decide()` for one follower symbol |
| `engine/connection.py` | `ConnectionSupervisor`: reconnect with exponential backoff |
| `engine/follower_worker.py` | Everything for one follower: copying, SL/TP sync, reconciliation, flatten |
| `engine/copy_engine.py` | Leader subscription, dedup, fan-out, pause/resume/kill switch |
| `engine/events.py` | Records emitted by the engine and the `EngineSink` protocol |
| `engine/risk.py` | `RiskGuard` protocol, `AllowAll`, `RiskManager` (rules, lock state, risk events) |
| `core/trading_day.py` | CME trading day (17:00 Chicago roll), trading-hours window in New York time |
| `services/runtime.py` | Builds/rebuilds the engine from the DB, controls, dashboard snapshot, WS broadcast loop |
| `services/sinks.py` | `DbSink` (batched writer) and `HubSink` (WebSocket) |
| `services/risk_store.py` | `DbRiskStore`: persists the daily-loss lock (`risk_state` table) |
| `api/*` | Auth, status/controls, accounts + copy settings + risk rules, logs + CSV, simulator, WebSocket |
| `db/*` | Models, seed data, small repository helpers |
| `cli.py`, `__main__.py` | `python -m app.cli init|hash-password|gen-key`, `python -m app` |

Account ids: the database id is an integer; inside the engine the same account
is addressed by `str(id)` (`Account.engine_id`).

## Broker adapter contract

`connect`, `disconnect`, `get_account`, `get_positions`, `get_orders`,
`place_order`, `modify_order`, `cancel_order`, `flatten`, `subscribe`.

* `place_order` returns the acknowledged order. A rejection is returned as
  `status == REJECTED` with `reject_reason`; it does not raise.
* Calls while disconnected raise `NotConnectedError`.
* Modify/cancel on a finished order raise `OrderNotWorkingError`.
* `subscribe()` returns a stream of `BrokerEvent`. Every event has a unique
  `event_id`. Events that happen while the connection is down may be lost, so
  consumers resync from `get_positions` / `get_orders` after reconnecting.
* `OrderRequest.client_order_id` is an idempotency key: placing the same key
  twice returns the first order.

## Copy engine

### Flow of one leader event

1. Leader adapter emits an event. `CopyEngine` drops it if the `event_id` was
   seen before.
2. The leader's `LeaderBook` is updated (orders, positions, and for a new order
   whether it is an *entry*, *exit* or *market* order).
3. A `LeaderEventRecord` goes to the sink (trade log).
4. If the copier is running, the event is queued for every `FollowerWorker`.
5. Each worker processes its queue strictly one action at a time. Leader
   events, the follower's own broker events, reconciliation ticks and flatten
   requests all go through this queue, so there are no races inside a follower
   and a slow follower never blocks another.

### What a worker does per event type

| Leader event | Follower action |
|---|---|
| Market order placed | Nothing. It is copied when it fills. |
| Limit/stop **entry** placed | Mirror the same resting order ("linked" order), sized by the target rule |
| SL/TP (**exit**) placed | Mirror it through `_sync_exits`, never larger than the follower's position |
| Order modified | Modify the linked order (price, size) |
| Order cancelled/rejected | Cancel the linked order |
| Fill | Drive the follower to its target with a market order for the *residual* |

### Sizing

```
target(symbol) = sign(leader position) * min(floor(|leader position| * multiplier * ratio), max_contracts)
residual       = target - actual - pending
```

`pending` counts working market orders plus linked orders that are expected to
fill because their leader counterpart already did. The floor is computed with
`Decimal` so that `0.29 * 100` is 29.

### Copy engine invariants

Each of these exists because its absence caused a real bug. Each has a test in
`backend/tests/integration/`.

1. **Positions come from cumulative fills per order.** An update changes the
   position by `filled_qty - already_applied`. The same fill can arrive in the
   `place_order` response, in the event stream and in a snapshot without being
   counted twice (`PositionBook.apply_order`).
2. **A follower exit order never exceeds the follower's position**, and is
   cancelled when the follower has no position in that direction
   (`_sync_exits`). Otherwise a stop could reverse the follower.
3. **A linked order that lags behind the leader is cancelled before the
   follower is adjusted** (`_cancel_lagging`). This is the "leader closes while
   a follower order is pending" case.
4. **Stale fills are skipped.** If a newer leader fill is already known, an
   older queued fill is not acted on (`leader_fill_seq`), because positions are
   always evaluated at their latest state. Acting on it trades an intermediate
   state that no longer exists.
5. **A follower that is ahead of the leader on a still-working linked order
   does not sell back** (`_link_adjustment`). Example: leader limit filled 2 of
   4, follower's mirrored limit filled 4.
6. **No joining mid-trade.** After start, pause/resume, kill switch or a manual
   flatten a follower is in `WAIT_FOR_FLAT` until the leader is flat, unless
   its position already equals the target.
7. **Alert-only followers are never corrected toward the target.** They copy
   each fill's own size change; reconciliation only reports.
8. **Auto-correct gives up after `max_correction_attempts` (3)** and alerts
   once, until *retry sync* or a config change resets it. This avoids an
   endless reject loop.
9. **A flatten that fails while a follower is offline is retried on
   reconnect** (`flatten_pending`).
10. **A broker position that differs from the local book is adopted only when
    seen on two consecutive checks** (`verify_positions`), so an in-flight fill
    does not cause a false correction.
11. **The sink can never break trading.** Sink errors are caught and logged.
12. **A risk-locked follower does not copy at all** (`_copying_allowed`), and
    the lock is set *before* the flatten is sent, so nothing can slip in
    between.
13. **Orders that only reduce exposure always pass the risk guard**
    (`sizing.reduces_exposure`; a reversal through zero does not count as a
    reduction).
14. **The risk cap is part of the target, not a block.** `FollowerConfig.
    position_cap` is the tighter of max contracts and the risk cap, so a capped
    follower is in sync at the cap. Position + pending + resting same-side
    entries never exceed the cap: new mirrored entries are sized to the
    remaining room (`_entry_room`) and a market order first cancels resting
    entries if it needs their room (`_make_room`).
15. **Outside trading hours a follower is reduce-only and sticky.** Its target
    is clamped with `reduce_only_target`, entries are skipped, exits pass. When
    the window opens, reduce-only is only released once the leader is flat or
    the follower is already in line (`_update_restrictions`), so it never joins
    mid-trade.

### Reconciliation tick (default every 3 s)

For each follower: fetch a broker snapshot, merge it, cancel lagging linked
orders and stale market remainders, then for every managed symbol call
`reconciler.decide(target, actual, pending, mode, attempts)`. Result is one of
`NONE`, `WAIT`, `CORRECT` (send market order) or `ALERT`. Then `_sync_exits`.

### Risk rules (`engine/risk.py`)

`RiskManager` is owned by the runtime, survives engine rebuilds and is attached
to each new engine. It implements `RiskGuard` (`check_order`, `is_locked`,
`outside_hours`, `note`) and runs its own loop: woken on every simulated price
change and at least once per second.

| Rule | Mechanism |
|---|---|
| Daily loss limit | `evaluate()` compares `baseline - day_pnl` with the limit. On breach: lock (persisted via `RiskStore`), risk event + error log, then `engine.flatten_follower()`. |
| Manual re-enable | `unlock()`: clears the lock and sets `baseline` to the current day P&L, so the follower gets a fresh allowance. The worker gets a `JoinAction` (waits for leader flat if needed). |
| New trading day | `trading_day(now)` changed: locks and baselines cleared, counters reset, `on_new_day` callback (runtime resets simulated day P&L), released workers rejoin. `start_new_day()` does the same on demand for the simulator. |
| Max position | In the target via `FollowerConfig.risk_max_position`; guard blocks any single non-reducing order that would exceed it. |
| Trading hours | `outside_hours()` from `within_trading_hours` (New York time, weekdays, windows across midnight supported). The worker turns it into reduce-only mode. |

Day P&L comes from a provider function (`Runtime._day_pnl`): the simulated
account's snapshot, or the polled `get_account()` cache for other adapters.
`RiskManager.note()` writes one risk event per follower/rule/trading day and
counts later hits in memory. The clock is injectable (`now_fn`), which is how
the tests move through trading hours and the 17:00 roll.

In the simulator, `reset_day()` restarts the day P&L and carries open positions
at the current price (`_PositionState.day_price`), like a daily settlement; the
entry price and the balance are unchanged.

### Engine settings (`engine/config.py`)

| Setting | Default | Meaning |
|---|---|---|
| `reconcile_interval_s` | 3.0 | Reconciliation period |
| `linked_fill_grace_s` | 2.0 | How long a linked order may lag the leader's fill |
| `order_timeout_s` | 10.0 | Broker call timeout |
| `stale_market_order_s` | 5.0 | A working market remainder older than this is replaced |
| `max_correction_attempts` | 3 | Auto-correct attempts before alert-only |

## Simulated broker

* Market orders fill at the current price; resting limits fill at their limit;
  stops trigger at the stop and fill at market.
* NQ/MNQ, ES/MES, YM/MYM, RTY/M2K share one price each.
* A bracket creates an OCO pair of exits when the entry fills. Bracket exits
  are position-linked: they shrink with the position and are cancelled when it
  is flat. Standalone stop/limit orders are not touched.
* Failure injection: `inject_disconnect`, `restore_connection`,
  `reject_next_orders`, `partial_fill_next_orders`, `partial_fill_order`,
  `set_latency`, `duplicate_next_events`, `release_partial_fills`,
  `clear_failures`.
* State is in memory and resets on restart.

## Runtime and persistence

* `Runtime.rebuild()` creates the engine from the accounts in the DB. It needs
  exactly one enabled leader. Adapters are reused across rebuilds, so simulated
  positions survive.
* Rebuild triggers: account created/deleted, role, enabled, broker type or
  credentials changed. Copy settings are applied live with
  `engine.update_follower()` (no rebuild).
* `copier_running` is stored in `app_settings`; pause and kill switch survive
  a restart.
* Tables: `accounts`, `copy_settings`, `contract_mappings`, `risk_rules`,
  `risk_state`, `risk_events`, `leader_events`, `follower_orders`,
  `system_log`, `app_settings`. Times are UNIX epoch seconds (UTC).
* Risk rule changes are applied live (`Runtime.apply_follower_settings`):
  the rules go to the `RiskManager`, the cap goes into the follower's config,
  and a reconciliation tick is queued.
* Tables are created with `create_all` on startup. There are no migrations yet;
  a schema change needs either a migration tool or deleting the dev database.

## API surface

| Group | Paths |
|---|---|
| Auth | `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/me` |
| Dashboard | `GET /api/status`, `GET /api/meta` |
| Copier | `POST /api/copier/pause`, `/resume`, `/kill`, `/reconcile` |
| Accounts | `GET/POST /api/accounts`, `GET/PUT/DELETE /api/accounts/{id}`, `POST .../test-connection`, `.../flatten`, `.../retry-sync`, `GET .../orders` |
| Copy settings | `GET/PUT /api/accounts/{id}/copy-settings` |
| Risk | `GET/PUT /api/accounts/{id}/risk-rules`, `POST /api/accounts/{id}/risk/unlock`, `GET /api/risk/events` (`?day=today`) |
| Logs | `GET /api/trades`, `GET /api/trades/export.csv`, `GET /api/logs` |
| Simulator | `GET /api/sim/state`, `POST /api/sim/order`, `/close`, `/price`, `/walk`, `/new-day`, `/orders/{id}/modify`, `/orders/{id}/cancel`, `/accounts/{id}/failures` |
| Live | WebSocket `/ws`: `snapshot`, `leader_event`, `follower_order`, `log`, `risk_event` |

The snapshot (`GET /api/status` and the `snapshot` WebSocket message) carries
`trading_day`, `next_reset` and, per follower, `risk` (locked, loss used, limit,
cap, trading hours, counts) and `copy.reduce_only` / `copy.risk_locked`.

Security details: sessions are server-side (the cookie only holds a session id
and is `HttpOnly`, `SameSite=Strict`); five failed logins lock the client for
60 s; the WebSocket checks the session and the browser origin.

## Frontend

| Path | Content |
|---|---|
| `src/main.tsx` | Providers and routes |
| `src/lib/api.ts` | Fetch wrapper, error messages, 401 handling |
| `src/lib/auth.tsx` | Login state |
| `src/lib/live.tsx` | WebSocket with reconnect; `useLive()` (snapshot, alerts, latencies), `useLiveEvents()` |
| `src/lib/types.ts` | Types matching the API payloads |
| `src/components/ui.tsx` | Button, Card, Badge, Input, Select, Toggle, Field, Modal, Table, toasts |
| `src/components/RiskControls.tsx` | Loss meter and the re-enable button with its confirmation |
| `src/pages/*` | Dashboard, Accounts, CopySettings, Risk, TradeLog, SystemLog, Simulator, Login |

FastAPI serves `frontend/dist` and falls back to `index.html` for browser
routes; unknown `/api/*` paths stay 404.

## Tests

* `tests/unit`: sizing and rounding, reconciliation decision, state books,
  simulated broker, Tradovate stub, trading day and trading hours, risk guard.
* `tests/integration`: engine scenarios with the simulated broker (entry/exit,
  scale in/out, SL/TP modify, partial fills, rejection, disconnects, duplicate
  events, leader closes while follower pending, pause/kill/flatten, daily loss
  breach / re-enable / new day, max position, trading hours).
* `tests/api`: login and security, account CRUD, trade flow through the API,
  CSV, WebSocket, SPA serving, risk rules live (breach, unlock, restart, new
  day).

Integration tests use `rig.settle()` (waits until every queue is empty) instead
of sleeping, so they are deterministic.
