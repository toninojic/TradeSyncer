import { useQuery } from "@tanstack/react-query";
import { Download } from "lucide-react";
import { Fragment, useMemo, useState } from "react";
import { RecordStatusBadge } from "@/components/status";
import { Badge, Button, Card, Empty, PageHeader, Select, Table, Td, Th, Toggle, type Tone } from "@/components/ui";
import { api, queryString } from "@/lib/api";
import { ms, price, smartTime, SYMBOLS } from "@/lib/format";
import { useLiveEvents } from "@/lib/live";
import type { Account, FollowerOrder, LeaderEvent, TradeItem } from "@/lib/types";

const RANGES: { value: string; label: string; seconds: number | null }[] = [
  { value: "1h", label: "Last hour", seconds: 3600 },
  { value: "today", label: "Today", seconds: null },
  { value: "24h", label: "Last 24 hours", seconds: 86400 },
  { value: "7d", label: "Last 7 days", seconds: 7 * 86400 },
  { value: "all", label: "Everything", seconds: null },
];

const ACTIONS = ["copy_fill", "mirror_entry", "mirror_exit", "modify", "cancel", "reconcile", "flatten", "skipped"];
const STATUSES = ["filled", "working", "partially_filled", "ok", "cancelled", "skipped", "rejected", "failed", "blocked"];

function since(range: string): number | undefined {
  if (range === "all") return undefined;
  if (range === "today") {
    const d = new Date();
    d.setHours(0, 0, 0, 0);
    return d.getTime() / 1000;
  }
  const r = RANGES.find((x) => x.value === range);
  return r?.seconds ? Date.now() / 1000 - r.seconds : undefined;
}

export function TradeLogPage() {
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: () => api.get<Account[]>("/api/accounts") });
  const followers = (accounts.data ?? []).filter((a) => a.role === "follower");
  const [range, setRange] = useState("today");
  const [follower, setFollower] = useState("");
  const [status, setStatus] = useState("");
  const [action, setAction] = useState("");
  const [symbol, setSymbol] = useState("");
  const [limit, setLimit] = useState(100);
  const [live, setLive] = useState(true);

  const params = useMemo(
    () => ({ since: since(range), follower_id: follower || undefined, status: status || undefined, action: action || undefined, symbol: symbol || undefined }),
    [range, follower, status, action, symbol],
  );
  const trades = useQuery({
    queryKey: ["trades", params, limit],
    queryFn: () => api.get<{ items: TradeItem[]; has_more: boolean }>(`/api/trades${queryString({ ...params, limit })}`),
  });
  useLiveEvents(["leader_event", "follower_order"], () => live && void trades.refetch(), 400);

  const exportUrl = `/api/trades/export.csv${queryString(params)}`;

  return (
    <div>
      <PageHeader
        title="Trade log"
        description="Every leader event and the follower orders it caused, with status, latency and errors."
        actions={
          <Button onClick={() => window.location.assign(exportUrl)}>
            <Download size={15} /> Export CSV
          </Button>
        }
      />
      <Card className="mb-4" bodyClassName="flex flex-wrap items-end gap-3">
        <FilterSelect label="Period" value={range} onChange={setRange}>
          {RANGES.map((r) => <option key={r.value} value={r.value}>{r.label}</option>)}
        </FilterSelect>
        <FilterSelect label="Follower" value={follower} onChange={setFollower}>
          <option value="">All followers</option>
          {followers.map((f) => <option key={f.id} value={f.id}>{f.name}</option>)}
        </FilterSelect>
        <FilterSelect label="Status" value={status} onChange={setStatus}>
          <option value="">Any status</option>
          <option value="error">Errors (rejected, failed, blocked)</option>
          {STATUSES.map((s) => <option key={s} value={s}>{s.replace(/_/g, " ")}</option>)}
        </FilterSelect>
        <FilterSelect label="Action" value={action} onChange={setAction}>
          <option value="">Any action</option>
          {ACTIONS.map((a) => <option key={a} value={a}>{a.replace(/_/g, " ")}</option>)}
        </FilterSelect>
        <FilterSelect label="Symbol" value={symbol} onChange={setSymbol}>
          <option value="">Any symbol</option>
          {SYMBOLS.map((s) => <option key={s}>{s}</option>)}
        </FilterSelect>
        <div className="pb-1.5">
          <Toggle checked={live} onChange={setLive} label="Live" />
        </div>
      </Card>

      <Card bodyClassName="p-0">
        {trades.isLoading ? (
          <Empty>Loading…</Empty>
        ) : !trades.data?.items.length ? (
          <Empty>No entries for these filters.</Empty>
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Time</Th>
                <Th>Source</Th>
                <Th>Order</Th>
                <Th>Status</Th>
                <Th className="text-right">Filled</Th>
                <Th className="text-right">Latency</Th>
                <Th>Details</Th>
              </tr>
            </thead>
            <tbody>
              {trades.data.items.map((item) => (
                <Fragment key={item.event?.id ?? item.orders[0]?.id}>
                  {item.event ? <LeaderRow event={item.event} /> : null}
                  {item.orders.map((o) => (
                    <OrderRow key={o.id} order={o} engine={item.kind === "engine_action"} />
                  ))}
                  {item.event && item.orders.length === 0 && (
                    <tr>
                      <Td />
                      <Td colSpan={6} className="whitespace-normal text-xs text-slate-500">
                        {!item.event.copied
                          ? `Not copied${item.event.note ? `: ${item.event.note}` : ""}`
                          : item.event.type === "order_placed" && item.event.order_type === "market"
                            ? "Market order: followers copy it when it fills (next row)"
                            : "No follower order needed"}
                      </Td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </Table>
        )}
        {trades.data?.has_more && (
          <div className="border-t border-slate-800 p-3 text-center">
            <Button size="sm" onClick={() => setLimit((l) => l + 100)}>
              Load more
            </Button>
          </div>
        )}
      </Card>
    </div>
  );
}

function FilterSelect({ label, value, onChange, children }: { label: string; value: string; onChange: (v: string) => void; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1 text-xs font-medium uppercase tracking-wide text-slate-400">
      {label}
      <Select className="w-48 normal-case tracking-normal" value={value} onChange={(e) => onChange(e.target.value)}>
        {children}
      </Select>
    </label>
  );
}

const EVENT_LABEL: Record<string, { label: string; tone: Tone }> = {
  order_filled: { label: "Fill", tone: "green" },
  order_placed: { label: "Placed", tone: "sky" },
  order_modified: { label: "Modified", tone: "violet" },
  order_cancelled: { label: "Cancelled", tone: "slate" },
  order_rejected: { label: "Rejected", tone: "red" },
};

function describe(side: string | null, qty: number | null, symbol: string | null, type: string | null, px: number | null): string {
  const parts = [side?.toUpperCase(), qty, symbol, type && type !== "market" ? type : type === "market" ? "MKT" : null];
  const text = parts.filter((p) => p !== null && p !== undefined && p !== "").join(" ");
  return px ? `${text} @ ${price(px)}` : text;
}

function LeaderRow({ event }: { event: LeaderEvent }) {
  const meta = EVENT_LABEL[event.type] ?? { label: event.type, tone: "slate" as Tone };
  return (
    <tr className="bg-slate-800/30">
      <Td className="num whitespace-nowrap text-xs text-slate-300">{smartTime(event.received_at)}</Td>
      <Td>
        <span className="flex items-center gap-2">
          <Badge tone="violet">Leader</Badge>
          <Badge tone={meta.tone}>{meta.label}</Badge>
        </span>
      </Td>
      <Td className="font-medium text-slate-100">{describe(event.side, event.qty, event.symbol, event.order_type, event.price)}</Td>
      <Td>{!event.copied && <Badge tone="amber">not copied</Badge>}</Td>
      <Td className="num text-right text-slate-300">
        {event.fill_qty ? `${event.fill_qty} @ ${price(event.fill_price)}` : ""}
      </Td>
      <Td />
      <Td className="min-w-[160px] whitespace-normal text-xs text-slate-400">
        {event.leader_position !== null && `Leader position after: ${event.leader_position}`}
        {event.note && ` · ${event.note}`}
      </Td>
    </tr>
  );
}

function OrderRow({ order, engine }: { order: FollowerOrder; engine: boolean }) {
  return (
    <tr>
      <Td className="num whitespace-nowrap text-xs text-slate-500">{engine ? smartTime(order.created_at) : ""}</Td>
      <Td>
        <span className="flex items-center gap-2 pl-3">
          <span className="text-slate-600">↳</span>
          <span className="text-slate-200">{order.follower_name ?? `#${order.follower_id}`}</span>
          <span className="text-xs text-slate-500">{order.action.replace(/_/g, " ")}</span>
        </span>
      </Td>
      <Td className="text-slate-300">{describe(order.side, order.qty, order.symbol, order.order_type, order.price)}</Td>
      <Td>
        <RecordStatusBadge status={order.status} />
      </Td>
      <Td className="num text-right text-slate-300">
        {order.filled_qty ? `${order.filled_qty} @ ${price(order.avg_fill_price)}` : ""}
      </Td>
      <Td className="num text-right text-slate-300">{ms(order.latency_ms)}</Td>
      <Td className={`min-w-[220px] max-w-md whitespace-normal text-xs ${["rejected", "failed", "blocked"].includes(order.status) ? "text-rose-300" : "text-slate-400"}`}>
        {order.error}
      </Td>
    </tr>
  );
}
