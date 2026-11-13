# Glossary

## Accounts and roles

| Term | Meaning |
|---|---|
| Leader | The account the owner trades on. Everything on it is copied. |
| Follower | An account that copies the leader. |
| F1, F2, F3 | Names of the three seeded demo followers, nothing more: F1 = 1x NQ, F2 = 0.5x NQ (alert-only), F3 = NQ -> MNQ at 10:1. |
| Sim | Simulated broker account (no real money, no real broker). |

## Contracts

| Symbol | Index | USD per point | Tick | USD per tick |
|---|---|---|---|---|
| NQ | Nasdaq-100, E-mini | 20 | 0.25 | 5.00 |
| MNQ | Nasdaq-100, Micro | 2 | 0.25 | 0.50 |
| ES | S&P 500, E-mini | 50 | 0.25 | 12.50 |
| MES | S&P 500, Micro | 5 | 0.25 | 1.25 |
| YM | Dow 30, E-mini | 5 | 1 | 5.00 |
| MYM | Dow 30, Micro | 0.50 | 1 | 0.50 |
| RTY | Russell 2000, E-mini | 50 | 0.10 | 5.00 |
| M2K | Russell 2000, Micro | 5 | 0.10 | 0.50 |

* A micro is one tenth of the E-mini on the same index, so NQ -> MNQ at 10:1 is
  the same exposure.
* The code uses root symbols (`NQ`). A real broker trades a specific expiry
  such as `NQZ6` (Z = December, 6 = 2026; quarterly months are H, M, U, Z).
  Translating and rolling is the adapter's job.
* *Underlying* / *index family*: contracts on the same index. They share one
  price in the simulator and a mapping is only allowed inside one family.

## Positions and orders

| Term | Meaning |
|---|---|
| Position +2 / -2 / flat | Long 2 contracts / short 2 contracts / no position |
| Market order | Executes immediately at the current price |
| Limit order | Buy at this price or lower, sell at this price or higher |
| Stop order | Becomes a market order when the price reaches the stop |
| SL (stop loss) | Stop order that closes a position at a loss |
| TP (take profit) | Limit order that closes a position at a profit |
| Bracket | Entry with SL and TP attached |
| OCO | One-cancels-other: when the SL fills the TP is cancelled, and vice versa |
| Fill / partial fill | Execution of an order / of only part of it |
| Ack | The broker confirms it received the order |
| Reject | The broker refuses the order (margin, limits ...) |
| Working | Accepted by the broker, not filled yet |
| Entry / exit order | Opens or adds to a position / reduces or closes it |

## Money

| Term | Meaning |
|---|---|
| P&L | Profit and loss |
| Realized P&L | From closed positions |
| Unrealized P&L | From open positions at the current price |
| Day P&L | Realized + unrealized for the current trading day |
| Balance | Starting balance + realized P&L |
| Trading day | CME session; it rolls at 17:00 America/Chicago |

## Copying

| Term | Meaning |
|---|---|
| Multiplier | Size factor per follower (0.5 = half) |
| Ratio | Contracts of the follower symbol per leader contract in a mapping |
| Contract mapping | Rule to copy one symbol as another (NQ -> MNQ) |
| Max contracts | Cap on the follower's size per symbol |
| Target | Position the follower should hold, derived from the leader's position |
| Residual | `target - actual - pending`; what still has to be ordered |
| Linked order | A follower order that mirrors a specific leader resting order |
| Copy fill | Market order sent because the leader filled |
| Mirror entry / mirror exit | Mirrored limit/stop entry / mirrored SL or TP |
| Reconciliation | Periodic check of actual vs target position |
| Auto-correct / alert-only | Fix a mismatch with an order / only report it |
| In sync / out of sync | Actual equals target / it does not |
| Waiting for leader flat | Follower will not join an open trade; copies from the next one |
| Latency | Time from receiving the leader's fill to the follower's order ack |
| Flatten | Cancel all working orders and close all positions on an account |
| Kill switch | Flatten every follower and stop copying; the leader is untouched |
| Daily loss limit | Max loss (realized + unrealized) per trading day; reaching it flattens and locks the follower |
| Risk lock / "Loss limit hit" | Follower is not copying because of the daily loss limit, until the next trading day or a manual re-enable |
| Loss used | How much of the daily allowance is consumed; measured from the baseline |
| Baseline | Day P&L at the last manual re-enable; the loss is measured from there |
| Max position | Hard cap on the follower's contracts per symbol (risk rule) |
| Room | Contracts that can still be added under the cap, counting resting entry orders |
| Trading hours | Window in New York time in which new entries are copied |
| Reduce-only / "Exits only" | Outside trading hours: the follower may only shrink its position |
| Day roll / reset | 17:00 Chicago: daily P&L, loss limits and locks start over |
| Event | Broker message (placed, modified, cancelled, filled ...) with a unique id |
| Dedup | Ignoring an event whose id was already processed |
| Backoff | Growing pauses between reconnect attempts (0.5 s, 1 s, 2 s ... max 30 s) |

## Record statuses in the trade log

| Status | Meaning |
|---|---|
| sent | Request is on its way |
| working | Accepted, waiting to fill |
| partially_filled | Part filled, rest working |
| filled | Fully executed |
| ok | Modify, cancel or flatten succeeded |
| cancelled | Order was cancelled |
| rejected | Broker refused it |
| failed | Request did not get through (disconnected, timeout) |
| blocked | Stopped by a risk rule or because copying is halted |
| skipped | Nothing was sent on purpose (size rounds to 0, copy SL/TP off ...) |
