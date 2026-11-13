import { useQuery } from "@tanstack/react-query";
import { Minus, Plus, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { ConnectionBadge, RoleBadge } from "@/components/status";
import { Badge, Button, Card, Empty, Field, Input, PageHeader, Select, Table, Td, Th, Toggle, useToast } from "@/components/ui";
import { api } from "@/lib/api";
import { UNDERLYING_LABEL, price, signedQty, SYMBOLS } from "@/lib/format";
import { useLive } from "@/lib/live";
import type { BrokerOrder, FailureState, SimState } from "@/lib/types";

export function SimulatorPage() {
  const { snapshot } = useLive();
  const sim = useQuery({ queryKey: ["sim-state"], queryFn: () => api.get<SimState>("/api/sim/state"), refetchInterval: 1000 });
  const leader = snapshot?.accounts.find((a) => a.role === "leader" && a.enabled);

  if (!snapshot || sim.isLoading) return <Empty>Loading…</Empty>;
  if (!leader) return <Empty>The simulator needs an enabled leader account.</Empty>;
  if (leader.broker_type !== "sim") return <Empty>The simulator only works when the leader is a simulated account.</Empty>;

  return (
    <div className="space-y-5">
      <PageHeader
        title="Simulator"
        description="Trade on the simulated leader, move prices and inject failures to test how the copier reacts. Nothing here touches a real broker."
      />
      <div className="grid gap-5 xl:grid-cols-2">
        <MarketCard prices={snapshot.prices} randomWalk={snapshot.random_walk} />
        <OrderTicket prices={snapshot.prices} />
      </div>
      <div className="grid gap-5 xl:grid-cols-2">
        <LeaderOrders orders={sim.data?.leader_orders ?? []} />
        <PositionsCard />
      </div>
      <FailureCard state={sim.data} />
    </div>
  );
}

// ------------------------------------------------------------------ market

function MarketCard({ prices, randomWalk }: { prices: Record<string, number>; randomWalk: boolean }) {
  const toast = useToast();
  const [inputs, setInputs] = useState<Record<string, string>>({});

  async function move(underlying: string, body: { price?: number; move?: number }) {
    try {
      await api.post("/api/sim/price", { symbol: underlying, ...body });
    } catch (err) {
      toast("error", (err as Error).message);
    }
  }

  return (
    <Card
      title="Market prices"
      description="Contracts of the same index share one price (NQ and MNQ move together)."
      actions={
        <Toggle
          checked={randomWalk}
          onChange={(v) => void api.post("/api/sim/walk", { running: v })}
          label="Random walk"
        />
      }
    >
      <div className="space-y-3">
        {Object.entries(prices).map(([underlying, value]) => (
          <div key={underlying} className="flex flex-wrap items-center gap-2 border-b border-slate-800/70 pb-3 last:border-0">
            <div className="w-28">
              <div className="text-xs text-slate-400">{UNDERLYING_LABEL[underlying]}</div>
              <div className="num text-lg font-semibold text-white">{price(value)}</div>
            </div>
            <div className="flex gap-1">
              {[-10, -1].map((p) => (
                <Button key={p} size="sm" onClick={() => void move(underlying, { move: p })} aria-label={`Down ${Math.abs(p)}`}>
                  <Minus size={12} />
                  {Math.abs(p)}
                </Button>
              ))}
              {[1, 10].map((p) => (
                <Button key={p} size="sm" onClick={() => void move(underlying, { move: p })} aria-label={`Up ${p}`}>
                  <Plus size={12} />
                  {p}
                </Button>
              ))}
            </div>
            <div className="ml-auto flex gap-1">
              <Input
                className="h-8 w-28"
                type="number"
                step="0.25"
                placeholder="Set price"
                aria-label={`Set ${UNDERLYING_LABEL[underlying]} price`}
                value={inputs[underlying] ?? ""}
                onChange={(e) => setInputs((s) => ({ ...s, [underlying]: e.target.value }))}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && Number(inputs[underlying]) > 0) void move(underlying, { price: Number(inputs[underlying]) });
                }}
              />
              <Button
                size="sm"
                disabled={!(Number(inputs[underlying]) > 0)}
                onClick={() => void move(underlying, { price: Number(inputs[underlying]) })}
              >
                Set
              </Button>
            </div>
          </div>
        ))}
        <p className="text-xs text-slate-500">Buttons move the price by index points. Resting orders trigger as soon as the price reaches them.</p>
      </div>
    </Card>
  );
}

// ------------------------------------------------------------------ order ticket

const UNDERLYING_OF: Record<string, string> = {
  NQ: "NASDAQ100", MNQ: "NASDAQ100", ES: "SP500", MES: "SP500", YM: "DOW30", MYM: "DOW30", RTY: "RUSSELL2000", M2K: "RUSSELL2000",
};

function OrderTicket({ prices }: { prices: Record<string, number> }) {
  const toast = useToast();
  const [symbol, setSymbol] = useState("NQ");
  const [qty, setQty] = useState("1");
  const [type, setType] = useState<"market" | "limit" | "stop">("market");
  const [orderPrice, setOrderPrice] = useState("");
  const [withBracket, setWithBracket] = useState(false);
  const [sl, setSl] = useState("");
  const [tp, setTp] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const current = prices[UNDERLYING_OF[symbol]];

  useEffect(() => {
    // prefill prices around the current market when the symbol or type changes
    if (current) {
      setOrderPrice(String(current));
      setSl(String(current - 20));
      setTp(String(current + 40));
    }
  }, [symbol]);

  async function send(side: "buy" | "sell") {
    const body: Record<string, unknown> = { symbol, side, qty: Number(qty), type };
    if (type === "limit") body.limit_price = Number(orderPrice);
    if (type === "stop") body.stop_price = Number(orderPrice);
    if (withBracket) {
      // the SL/TP fields are written for a long position; flip them for a short
      const slv = Number(sl);
      const tpv = Number(tp);
      const ref = type === "market" ? current : Number(orderPrice);
      body.stop_loss = side === "buy" ? slv : ref + (ref - slv);
      body.take_profit = side === "buy" ? tpv : ref - (tpv - ref);
    }
    setBusy(side);
    try {
      const order = await api.post<BrokerOrder>("/api/sim/order", body);
      toast(order.status === "rejected" ? "error" : "success", `Leader ${side} ${qty} ${symbol}: ${order.status}`);
    } catch (err) {
      toast("error", (err as Error).message);
    } finally {
      setBusy(null);
    }
  }

  async function close(sym: string | null) {
    setBusy("close");
    try {
      await api.post("/api/sim/close", { symbol: sym });
      toast("success", sym ? `Leader ${sym} closed` : "Leader flattened");
    } catch (err) {
      toast("error", (err as Error).message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <Card title="Leader order" description="Orders go to the leader account; followers copy them.">
      <div className="grid gap-3 sm:grid-cols-3">
        <Field label="Symbol">
          <Select value={symbol} onChange={(e) => setSymbol(e.target.value)}>
            {SYMBOLS.map((s) => <option key={s}>{s}</option>)}
          </Select>
        </Field>
        <Field label="Quantity">
          <Input type="number" min="1" step="1" value={qty} onChange={(e) => setQty(e.target.value)} />
        </Field>
        <Field label="Type">
          <Select value={type} onChange={(e) => setType(e.target.value as typeof type)}>
            <option value="market">Market</option>
            <option value="limit">Limit</option>
            <option value="stop">Stop</option>
          </Select>
        </Field>
        {type !== "market" && (
          <Field label={type === "limit" ? "Limit price" : "Stop price"} hint={`Market: ${price(current)}`}>
            <Input type="number" step="0.25" value={orderPrice} onChange={(e) => setOrderPrice(e.target.value)} />
          </Field>
        )}
      </div>
      <div className="mt-4 space-y-3 rounded-lg border border-slate-800 bg-slate-950/40 p-3">
        <Toggle checked={withBracket} onChange={setWithBracket} label="Attach stop loss / take profit (bracket)" />
        {withBracket && (
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Stop loss" hint="Written for a buy; mirrored automatically for a sell.">
              <Input type="number" step="0.25" value={sl} onChange={(e) => setSl(e.target.value)} />
            </Field>
            <Field label="Take profit">
              <Input type="number" step="0.25" value={tp} onChange={(e) => setTp(e.target.value)} />
            </Field>
          </div>
        )}
      </div>
      <div className="mt-4 flex flex-wrap gap-2">
        <Button variant="success" size="lg" className="flex-1" onClick={() => void send("buy")} loading={busy === "buy"} disabled={!(Number(qty) >= 1)}>
          Buy {qty} {symbol}
        </Button>
        <Button variant="danger" size="lg" className="flex-1" onClick={() => void send("sell")} loading={busy === "sell"} disabled={!(Number(qty) >= 1)}>
          Sell {qty} {symbol}
        </Button>
      </div>
      <div className="mt-2 flex gap-2">
        <Button variant="ghost" size="sm" onClick={() => void close(symbol)} loading={busy === "close"}>
          Close {symbol} position
        </Button>
        <Button variant="ghost" size="sm" onClick={() => void close(null)}>
          Close everything on the leader
        </Button>
      </div>
    </Card>
  );
}

// ------------------------------------------------------------------ leader working orders

function LeaderOrders({ orders }: { orders: BrokerOrder[] }) {
  const toast = useToast();
  const [edits, setEdits] = useState<Record<string, string>>({});
  const role = (o: BrokerOrder) => (o.parent_id ? (o.type === "stop" ? "SL" : "TP") : o.type);

  async function modify(o: BrokerOrder) {
    const value = Number(edits[o.id]);
    if (!(value > 0)) return;
    try {
      await api.post(`/api/sim/orders/${o.id}/modify`, o.type === "stop" ? { stop_price: value } : { limit_price: value });
      setEdits((e) => ({ ...e, [o.id]: "" }));
      toast("success", "Order modified");
    } catch (err) {
      toast("error", (err as Error).message);
    }
  }

  async function cancel(o: BrokerOrder) {
    try {
      await api.post(`/api/sim/orders/${o.id}/cancel`);
      toast("success", "Order cancelled");
    } catch (err) {
      toast("error", (err as Error).message);
    }
  }

  return (
    <Card title="Leader working orders" description="Change the price or cancel; followers mirror the change.">
      {orders.length === 0 ? (
        <Empty>No working orders on the leader.</Empty>
      ) : (
        <Table>
          <thead>
            <tr>
              <Th>Order</Th>
              <Th>Price</Th>
              <Th>New price</Th>
              <Th />
            </tr>
          </thead>
          <tbody>
            {orders.map((o) => (
              <tr key={o.id}>
                <Td>
                  <span className="flex items-center gap-2">
                    <Badge tone={o.side === "buy" ? "green" : "red"}>{o.side}</Badge>
                    <span className="text-slate-100">
                      {o.qty - o.filled_qty} {o.symbol}
                    </span>
                    <Badge tone="slate">{role(o)}</Badge>
                  </span>
                </Td>
                <Td className="num">{price(o.limit_price ?? o.stop_price)}</Td>
                <Td>
                  {o.type !== "market" && (
                    <div className="flex gap-1">
                      <Input
                        className="h-8 w-28"
                        type="number"
                        step="0.25"
                        value={edits[o.id] ?? ""}
                        placeholder={String(o.limit_price ?? o.stop_price ?? "")}
                        onChange={(e) => setEdits((s) => ({ ...s, [o.id]: e.target.value }))}
                        onKeyDown={(e) => e.key === "Enter" && void modify(o)}
                      />
                      <Button size="sm" onClick={() => void modify(o)} disabled={!(Number(edits[o.id]) > 0)}>
                        Update
                      </Button>
                    </div>
                  )}
                </Td>
                <Td className="text-right">
                  <Button size="sm" variant="ghost" onClick={() => void cancel(o)} aria-label="Cancel order">
                    <X size={14} /> Cancel
                  </Button>
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
    </Card>
  );
}

// ------------------------------------------------------------------ positions overview

function PositionsCard() {
  const { snapshot } = useLive();
  const accounts = (snapshot?.accounts ?? []).filter((a) => a.enabled);
  return (
    <Card title="Positions" description="Live, for every enabled account.">
      <Table>
        <thead>
          <tr>
            <Th>Account</Th>
            <Th>Position</Th>
            <Th className="text-right">Working orders</Th>
          </tr>
        </thead>
        <tbody>
          {accounts.map((a) => (
            <tr key={a.id}>
              <Td>
                <span className="flex items-center gap-2">
                  <RoleBadge role={a.role} />
                  {a.name}
                </span>
              </Td>
              <Td className="num">
                {a.positions.length === 0 ? (
                  <span className="text-slate-500">Flat</span>
                ) : (
                  a.positions.map((p) => (
                    <span key={p.symbol} className={`mr-3 ${p.qty > 0 ? "text-emerald-400" : "text-rose-400"}`}>
                      {p.symbol} {signedQty(p.qty)}
                    </span>
                  ))
                )}
              </Td>
              <Td className="num text-right">{a.working_orders}</Td>
            </tr>
          ))}
        </tbody>
      </Table>
    </Card>
  );
}

// ------------------------------------------------------------------ failure injection

type FailureAction = "disconnect" | "restore" | "reject_next" | "partial_next" | "latency" | "duplicate_next" | "clear";

const FAILURES: { value: FailureAction; label: string; help: string }[] = [
  { value: "disconnect", label: "Disconnect", help: "Drops the connection. Events during the outage are lost; the copier reconnects with backoff and resyncs." },
  { value: "restore", label: "Restore connection", help: "Allows reconnecting again (for a disconnect without duration)." },
  { value: "reject_next", label: "Reject next orders", help: "The next N orders on this account are rejected by the broker." },
  { value: "partial_next", label: "Partial fill next orders", help: "The next N orders fill only partly; the rest fills after a delay (or never)." },
  { value: "latency", label: "Add latency", help: "Every request to this account waits this long before the broker answers." },
  { value: "duplicate_next", label: "Duplicate next events", help: "The next N events from this account are delivered twice." },
  { value: "clear", label: "Clear all failures", help: "Removes every injected failure from this account." },
];

function FailureCard({ state }: { state: SimState | undefined }) {
  const toast = useToast();
  const accounts = state?.accounts ?? [];
  const [accountId, setAccountId] = useState<string>("");
  const [action, setAction] = useState<FailureAction>("disconnect");
  const [count, setCount] = useState("1");
  const [duration, setDuration] = useState("10");
  const [reason, setReason] = useState("Insufficient margin (simulated)");
  const [ratio, setRatio] = useState("0.5");
  const [remainder, setRemainder] = useState("2");
  const [latency, setLatency] = useState("500");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!accountId && accounts.length) setAccountId(String(accounts.find((a) => a.role === "follower")?.id ?? accounts[0].id));
  }, [accounts, accountId]);

  const help = useMemo(() => FAILURES.find((f) => f.value === action)?.help, [action]);

  async function inject(id: string, act: FailureAction) {
    const body: Record<string, unknown> = { action: act, count: Number(count) || 1 };
    if (act === "disconnect" && duration.trim()) body.duration_s = Number(duration);
    if (act === "reject_next") body.reason = reason;
    if (act === "partial_next") {
      body.ratio = Number(ratio);
      body.remainder_delay_s = remainder.trim() === "" ? null : Number(remainder);
    }
    if (act === "latency") body.latency_ms = Number(latency);
    setBusy(true);
    try {
      await api.post(`/api/sim/accounts/${id}/failures`, body);
      toast("success", `Injected: ${FAILURES.find((f) => f.value === act)?.label}`);
    } catch (err) {
      toast("error", (err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title="Failure injection" description="Make the simulated broker misbehave and watch the dashboard, trade log and system log.">
      <div className="grid gap-5 lg:grid-cols-[minmax(0,420px)_1fr]">
        <div className="space-y-3">
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Account">
              <Select value={accountId} onChange={(e) => setAccountId(e.target.value)}>
                {accounts.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
              </Select>
            </Field>
            <Field label="Failure">
              <Select value={action} onChange={(e) => setAction(e.target.value as FailureAction)}>
                {FAILURES.map((f) => <option key={f.value} value={f.value}>{f.label}</option>)}
              </Select>
            </Field>
            {(action === "reject_next" || action === "partial_next" || action === "duplicate_next") && (
              <Field label="How many">
                <Input type="number" min="1" value={count} onChange={(e) => setCount(e.target.value)} />
              </Field>
            )}
            {action === "disconnect" && (
              <Field label="Duration (seconds)" hint="Empty = until you restore it.">
                <Input type="number" min="1" value={duration} onChange={(e) => setDuration(e.target.value)} />
              </Field>
            )}
            {action === "reject_next" && (
              <Field label="Reject reason" className="sm:col-span-2">
                <Input value={reason} onChange={(e) => setReason(e.target.value)} />
              </Field>
            )}
            {action === "partial_next" && (
              <>
                <Field label="Fill ratio" hint="0.5 = half fills first">
                  <Input type="number" step="0.1" min="0.1" max="0.9" value={ratio} onChange={(e) => setRatio(e.target.value)} />
                </Field>
                <Field label="Rest fills after (s)" hint="Empty = never by itself">
                  <Input type="number" min="0" value={remainder} onChange={(e) => setRemainder(e.target.value)} />
                </Field>
              </>
            )}
            {action === "latency" && (
              <Field label="Latency (ms)" hint="0 removes it">
                <Input type="number" min="0" step="100" value={latency} onChange={(e) => setLatency(e.target.value)} />
              </Field>
            )}
          </div>
          {help && <p className="text-xs text-slate-400">{help}</p>}
          <Button variant="primary" onClick={() => void inject(accountId, action)} loading={busy} disabled={!accountId}>
            Inject
          </Button>
        </div>

        <Table>
          <thead>
            <tr>
              <Th>Account</Th>
              <Th>Connection</Th>
              <Th>Active failures</Th>
              <Th />
            </tr>
          </thead>
          <tbody>
            {accounts.map((a) => (
              <tr key={a.id}>
                <Td>
                  <span className="flex items-center gap-2">
                    <RoleBadge role={a.role} /> {a.name}
                  </span>
                </Td>
                <Td>
                  <ConnectionBadge connection={a.connection} />
                </Td>
                <Td>
                  <FailureChips f={a.failures} />
                </Td>
                <Td className="text-right">
                  <Button size="sm" variant="ghost" onClick={() => void inject(String(a.id), "clear")}>
                    Clear
                  </Button>
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      </div>
    </Card>
  );
}

function FailureChips({ f }: { f: FailureState }) {
  const chips: string[] = [];
  if (f.forced_down) chips.push(f.forced_down_remaining_s !== null ? `down ${f.forced_down_remaining_s}s` : "down");
  if (f.reject_next) chips.push(`reject ×${f.reject_next}`);
  if (f.partial_next) chips.push(`partial ×${f.partial_next}`);
  if (f.latency_ms) chips.push(`+${f.latency_ms} ms`);
  if (f.duplicate_next_events) chips.push(`dup ×${f.duplicate_next_events}`);
  if (!chips.length) return <span className="text-xs text-slate-500">none</span>;
  return (
    <span className="flex flex-wrap gap-1">
      {chips.map((c) => <Badge key={c} tone="amber">{c}</Badge>)}
    </span>
  );
}

