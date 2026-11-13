# Connecting real brokers (notes, nothing implemented)

The MVP never connects to a real broker. This file records what a real
integration needs so the discussion does not start from zero.

## What changes and what does not

Only a new `BrokerAdapter` implementation is needed (plus a `broker_type`
option in `Runtime._create_adapter`, the API schema and the Accounts form).
The copy engine, database, API and admin panel stay as they are.

TradingView is only the screen the owner trades on. Orders placed there go to
the brokerage account. The copier watches the brokerage account through the
broker's API, so it sees those orders without talking to TradingView.

## Adapter checklist

1. **Auth and session:** log in, renew tokens before they expire, heartbeat.
2. **Symbols:** translate root symbols (`NQ`) to the active contract (`NQZ6`)
   and roll before expiry. Map the broker's contract ids back to root symbols
   in events and positions.
3. **Events:** translate the broker's order / fill / position messages into
   `BrokerEvent`s. `event_id` must be stable and unique (for example broker
   entity id + version) so duplicates are dropped by the engine.
4. **Orders:** place (market, limit, stop), modify, cancel, flatten. Pass
   `client_order_id` through if the API has a client id, so retries are
   idempotent. Return rejections as `status == REJECTED` with the reason.
5. **Snapshots:** `get_positions`, `get_orders(active_only=False)`,
   `get_account` for reconciliation and resync after reconnect.
6. **Connection status:** emit `CONNECTION_STATUS` on disconnect so the
   `ConnectionSupervisor` reconnects with backoff.
7. **Limits:** respect API rate limits; back off on throttling responses.
8. **Secrets:** credentials arrive decrypted in memory only; never log them.
9. **Tests:** run the integration scenarios against the broker's demo
   environment before any live account.

Things that differ from the simulator and need checking per broker: whether
bracket exits are position-linked, how OCO partial fills behave, order
modification semantics (replace vs amend), partial-fill reporting, and what
happens to working orders at the session break.

## Per broker

The requirements below come from forums and third-party sites and were **not
confirmed with the brokers**. Confirm them before relying on them.

### Tradovate

* REST + WebSocket API, separate demo and live environments.
* Login uses username/password plus an API application's client id (`cid`) and
  secret (`sec`). The stub in `brokers/tradovate/adapter.py` lists the
  endpoints to look at (`auth/accesstokenrequest`, `order/placeorder`,
  `order/placeoso`, `order/modifyorder`, `order/cancelorder`,
  `order/liquidateposition`, `position/list`, `order/list`, WebSocket
  `user/syncrequest`); verify the names against the official docs.
* Reported requirements for live API access: a live account with at least
  USD 1,000 and a paid API-access subscription (about USD 25/month).
* One vendor source says prop-firm / evaluation accounts on Tradovate are
  excluded from direct API access. Important if the owner's accounts are with
  a prop firm.

### Rithmic

* R | Protocol API: WebSocket + Protocol Buffers. (R | API+ is C++/.NET.)
* There is a test gateway; production access for a custom application requires
  passing Rithmic's conformance process.
* Python library: `async-rithmic` (its docs show the test system name, app
  name/version and gateway URL parameters).

### MetaTrader 5

* The usual Python route is the `MetaTrader5` package talking to a locally
  installed MT5 terminal on Windows. On Linux/Docker it needs a workaround
  (Wine-based bridges such as `mt5linux`).
* MT5 is mostly used by forex/CFD brokers; check that the owner's futures
  accounts are really on MT5.

## Operational differences in live trading

* Latency is network-bound (tens of milliseconds), not 1-2 ms.
* CME has a daily break (16:00-17:00 Chicago time); brokers have maintenance
  windows.
* The copier only works while the machine is on and connected. For live use an
  always-on machine or a VPS near the broker is safer than a desktop PC.
* Prop firms have their own rules about copy trading; check them.

## Suggested path

1. Find out which broker/platform the accounts are on and whether API access
   is available for that account type.
2. Build the adapter against the demo environment.
3. Run leader-demo + follower-demo for several days; watch sync status,
   latency and the logs.
4. Go live with minimum size first.

## Sources (third party, checked 2026-11-13)

* https://community.tradovate.com/t/api-access-requirements/7769
* https://danetrades.freshdesk.com/support/solutions/articles/202000104289-tradovate-api-requirements-and-subscription
* https://blog.pickmytrade.trade/tradovate-api-access-without-1000-minimum-2026-options/
* https://pickmytrade.io/help/rithmic/rithmic-api-overview/
* https://async-rithmic.readthedocs.io/en/stable/connection.html
* https://pypi.org/project/mt5linux/0.1.8
