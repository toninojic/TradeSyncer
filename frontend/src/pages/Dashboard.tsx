import { OctagonX, Pause, Play, RefreshCw } from "lucide-react";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { LossMeter, ReEnableButton } from "@/components/RiskControls";
import { ConnectionBadge, CopierBadge, LevelBadge, SyncBadge } from "@/components/status";
import { Badge, Button, Card, ConfirmDialog, Empty, PageHeader, Table, Td, Th, useToast } from "@/components/ui";
import { api } from "@/lib/api";
import { UNDERLYING_LABEL, money, ms, pnlClass, price, shortDateTime, signedQty, time } from "@/lib/format";
import { useLive } from "@/lib/live";
import type { AccountStatus } from "@/lib/types";

export function DashboardPage() {
  const { snapshot, recentAlerts, latencies, refresh } = useLive();
  const toast = useToast();
  const [confirmKill, setConfirmKill] = useState(false);
  const [flattenTarget, setFlattenTarget] = useState<AccountStatus | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const names = useMemo(
    () => Object.fromEntries((snapshot?.accounts ?? []).map((a) => [a.id, a.name])),
    [snapshot?.accounts],
  );

  if (!snapshot) return <Empty>Loading…</Empty>;
  const state = snapshot.copier.state;
  const leader = snapshot.accounts.find((a) => a.role === "leader" && a.enabled);
  const followers = snapshot.accounts.filter((a) => a.role === "follower");

  async function run(key: string, fn: () => Promise<unknown>, success: string) {
    setBusy(key);
    try {
      await fn();
      toast("success", success);
      await refresh();
    } catch (err) {
      toast("error", err instanceof Error ? err.message : "Request failed");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="space-y-5">
      <PageHeader title="Dashboard" description="Live state of the copier, the leader and every follower." />

      <div className="grid gap-5 lg:grid-cols-3">
        {/* ------------------------------------------------ copier controls */}
        <Card title="Copier" actions={<CopierBadge state={state} />}>
          {snapshot.copier.note && (
            <p className="mb-3 rounded-md bg-amber-950/60 px-3 py-2 text-sm text-amber-300">
              {snapshot.copier.note}. <Link to="/accounts" className="underline">Open accounts</Link>
            </p>
          )}
          {state === "killed" && (
            <p className="mb-3 rounded-md bg-rose-950/60 px-3 py-2 text-sm text-rose-300">
              Kill switch is active: followers were flattened and copying is stopped. Resume to copy again; followers
              join from the next leader trade.
            </p>
          )}
          <div className="flex flex-wrap gap-2">
            {state === "running" ? (
              <Button
                onClick={() => run("pause", () => api.post("/api/copier/pause"), "Copying paused")}
                loading={busy === "pause"}
              >
                <Pause size={15} /> Pause
              </Button>
            ) : (
              <Button
                variant="success"
                disabled={state === "no_leader"}
                onClick={() => run("resume", () => api.post("/api/copier/resume"), "Copying resumed")}
                loading={busy === "resume"}
              >
                <Play size={15} /> Resume
              </Button>
            )}
            <Button
              variant="ghost"
              disabled={state === "no_leader"}
              onClick={() => run("reconcile", () => api.post("/api/copier/reconcile"), "Reconciliation started")}
              loading={busy === "reconcile"}
            >
              <RefreshCw size={15} /> Reconcile now
            </Button>
          </div>
          <button
            onClick={() => setConfirmKill(true)}
            disabled={state === "no_leader"}
            className="mt-4 flex h-16 w-full items-center justify-center gap-3 rounded-lg bg-rose-600 text-lg font-bold tracking-wider text-white shadow-lg shadow-rose-900/40 transition hover:bg-rose-500 focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-rose-400 disabled:opacity-40"
          >
            <OctagonX size={26} /> KILL SWITCH
          </button>
          <p className="mt-2 text-xs text-slate-500">Flattens every follower and stops copying. The leader is not touched.</p>
          <p className="mt-3 border-t border-slate-800 pt-3 text-xs text-slate-400">
            Trading day <span className="num text-slate-200">{snapshot.trading_day}</span> · daily P&L and loss limits reset{" "}
            <span className="text-slate-200">{shortDateTime(snapshot.next_reset)}</span>
          </p>
        </Card>

        {/* ------------------------------------------------ leader */}
        <Card
          title={leader ? `Leader · ${leader.name}` : "Leader"}
          actions={leader && <ConnectionBadge connection={leader.connection} />}
        >
          {leader ? (
            <>
              <div className="mb-3 flex items-baseline justify-between">
                <span className="text-xs uppercase tracking-wide text-slate-400">Today's P&L</span>
                <span className={`num text-2xl font-semibold ${pnlClass(leader.day_pnl)}`}>
                  {money(leader.day_pnl, { sign: true })}
                </span>
              </div>
              <PositionList account={leader} />
              <p className="mt-3 text-xs text-slate-500">
                Working orders: {leader.working_orders} · Balance {money(leader.balance)}
              </p>
            </>
          ) : (
            <Empty>No enabled leader account.</Empty>
          )}
        </Card>

        {/* ------------------------------------------------ prices */}
        <Card
          title="Market (simulated)"
          actions={<Badge tone={snapshot.random_walk ? "green" : "slate"}>{snapshot.random_walk ? "Random walk on" : "Manual prices"}</Badge>}
        >
          <dl className="grid grid-cols-2 gap-3">
            {Object.entries(snapshot.prices).map(([underlying, value]) => (
              <div key={underlying} className="rounded-lg border border-slate-800 bg-slate-950/60 p-3">
                <dt className="text-xs text-slate-400">{UNDERLYING_LABEL[underlying] ?? underlying}</dt>
                <dd className="num mt-1 text-lg font-semibold text-white">{price(value)}</dd>
              </div>
            ))}
          </dl>
          <Link to="/simulator" className="mt-3 inline-block text-xs text-sky-400 hover:underline">
            Move prices or place leader trades in the simulator →
          </Link>
        </Card>
      </div>

      {/* ------------------------------------------------ followers */}
      <Card title="Followers" description="Position vs. target (what the copier wants the follower to hold).">
        {followers.length === 0 ? (
          <Empty>No follower accounts yet.</Empty>
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Account</Th>
                <Th>Connection</Th>
                <Th>Sync</Th>
                <Th>Position / target</Th>
                <Th className="text-right">Today's P&L · loss limit</Th>
                <Th className="text-right">Latency (last / avg)</Th>
                <Th className="text-right">Orders</Th>
                <Th />
              </tr>
            </thead>
            <tbody>
              {followers.map((f) => (
                <tr key={f.id} className={f.enabled ? "" : "opacity-50"}>
                  <Td>
                    <div className="font-medium text-slate-100">{f.name}</div>
                    <div className="text-xs text-slate-500">
                      {f.copy ? (f.copy.mismatch_mode === "alert_only" ? "alert only" : "auto-correct") : "not in copier"}
                    </div>
                  </Td>
                  <Td>
                    <ConnectionBadge connection={f.connection} />
                  </Td>
                  <Td>
                    <div className="flex flex-wrap items-center gap-1.5">
                      {f.enabled ? <SyncBadge status={f.copy?.sync_status} /> : <Badge>Disabled</Badge>}
                      {f.copy?.reduce_only && !f.risk?.locked && (
                        <span title="Outside the allowed trading hours: exits are copied, new entries are not">
                          <Badge tone="amber">Exits only</Badge>
                        </span>
                      )}
                    </div>
                  </Td>
                  <Td>
                    <PositionVsTarget account={f} />
                  </Td>
                  <Td className="text-right">
                    <div className={`num font-medium ${pnlClass(f.day_pnl)}`}>{money(f.day_pnl, { sign: true })}</div>
                    <div className="mt-1 flex justify-end">
                      <LossMeter risk={f.risk} compact />
                    </div>
                  </Td>
                  <Td className="num text-right text-slate-300">
                    {ms(f.copy?.last_latency_ms)} / {ms(f.copy?.avg_latency_ms)}
                  </Td>
                  <Td className="num text-right">{f.working_orders}</Td>
                  <Td className="text-right">
                    <div className="flex justify-end gap-1.5">
                      {f.risk?.locked && <ReEnableButton account={f} />}
                      {f.copy && (f.copy.sync_status === "out_of_sync" || f.copy.correction_attempts > 0) && (
                        <Button
                          size="sm"
                          onClick={() => run(`retry-${f.id}`, () => api.post(`/api/accounts/${f.id}/retry-sync`), `Retrying sync for ${f.name}`)}
                          loading={busy === `retry-${f.id}`}
                        >
                          Retry sync
                        </Button>
                      )}
                      <Button size="sm" variant="ghost" onClick={() => setFlattenTarget(f)}>
                        Flatten
                      </Button>
                    </div>
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>

      <div className="grid gap-5 lg:grid-cols-2">
        <Card title="Recent copy latency" description="Leader event received → follower order acknowledged (last 30 copies).">
          <LatencyTable followers={followers} latencies={latencies} />
        </Card>
        <Card title="Recent alerts" actions={<Link to="/logs" className="text-xs text-sky-400 hover:underline">System log →</Link>}>
          {recentAlerts.length === 0 ? (
            <Empty>No warnings or errors.</Empty>
          ) : (
            <ul className="divide-y divide-slate-800">
              {recentAlerts.slice(0, 8).map((alert, i) => (
                <li key={`${alert.ts}-${i}`} className="flex items-start gap-3 py-2 text-sm">
                  <span className="num w-16 shrink-0 text-xs text-slate-500">{time(alert.ts)}</span>
                  <LevelBadge level={alert.level} />
                  <span className="min-w-0 flex-1 text-slate-300">
                    {alert.account_id !== null && (
                      <span className="text-slate-400">{names[alert.account_id] ?? `#${alert.account_id}`}: </span>
                    )}
                    {alert.message}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      <ConfirmDialog
        open={confirmKill}
        title="Activate the kill switch?"
        danger
        confirmLabel="Flatten followers and stop"
        loading={busy === "kill"}
        message={
          <>
            All follower positions are closed at market, their working orders are cancelled and copying stops.
            The leader account is not touched.
          </>
        }
        onClose={() => setConfirmKill(false)}
        onConfirm={() =>
          run("kill", () => api.post("/api/copier/kill"), "Kill switch activated").finally(() => setConfirmKill(false))
        }
      />
      <ConfirmDialog
        open={flattenTarget !== null}
        title={`Flatten ${flattenTarget?.name ?? ""}?`}
        danger
        confirmLabel="Flatten"
        loading={busy === "flatten"}
        message={
          flattenTarget?.role === "leader"
            ? "Closes the leader's positions; followers copy the exit."
            : "Closes this account's positions and cancels its working orders. It rejoins copying when the leader is flat."
        }
        onClose={() => setFlattenTarget(null)}
        onConfirm={() =>
          flattenTarget &&
          run("flatten", () => api.post(`/api/accounts/${flattenTarget.id}/flatten`), `${flattenTarget.name} flattened`).finally(() =>
            setFlattenTarget(null),
          )
        }
      />
    </div>
  );
}

function PositionList({ account }: { account: AccountStatus }) {
  if (account.positions.length === 0) return <p className="text-sm text-slate-500">Flat</p>;
  return (
    <table className="w-full text-sm">
      <tbody>
        {account.positions.map((p) => (
          <tr key={p.symbol} className="border-t border-slate-800">
            <td className="py-1.5 font-medium text-slate-100">{p.symbol}</td>
            <td className={`num py-1.5 font-semibold ${p.qty > 0 ? "text-emerald-400" : "text-rose-400"}`}>{signedQty(p.qty)}</td>
            <td className="num py-1.5 text-slate-400">@ {price(p.avg_price)}</td>
            <td className={`num py-1.5 text-right ${pnlClass(p.unrealized_pnl)}`}>{money(p.unrealized_pnl, { sign: true })}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function PositionVsTarget({ account }: { account: AccountStatus }) {
  const actual = Object.fromEntries(account.positions.map((p) => [p.symbol, p.qty]));
  const targets = account.copy?.targets ?? {};
  // A gap is only a problem while the follower is actively copying.
  const active = ["in_sync", "pending", "out_of_sync"].includes(account.copy?.sync_status ?? "");
  const symbols = Array.from(new Set([...Object.keys(actual), ...Object.keys(targets)])).sort();
  if (symbols.length === 0) return <span className="text-slate-500">Flat</span>;
  return (
    <div className="flex flex-col gap-0.5">
      {symbols.map((s) => {
        const a = actual[s] ?? 0;
        const t = targets[s] ?? 0;
        const ok = a === t;
        return (
          <span key={s} className="num whitespace-nowrap text-sm">
            <span className="text-slate-400">{s}</span>{" "}
            <span className={ok || !active ? "text-slate-100" : "font-semibold text-rose-400"}>{signedQty(a)}</span>
            <span className="text-slate-500"> / {signedQty(t)}</span>
          </span>
        );
      })}
    </div>
  );
}

function LatencyTable({ followers, latencies }: { followers: AccountStatus[]; latencies: Record<number, number[]> }) {
  const rows = followers.filter((f) => (latencies[f.id] ?? []).length > 0);
  if (rows.length === 0) return <Empty>No copies yet in this browser session.</Empty>;
  return (
    <Table>
      <thead>
        <tr>
          <Th>Follower</Th>
          <Th className="text-right">Copies</Th>
          <Th className="text-right">Last</Th>
          <Th className="text-right">Average</Th>
          <Th className="text-right">Worst</Th>
        </tr>
      </thead>
      <tbody>
        {rows.map((f) => {
          const values = latencies[f.id];
          const avg = values.reduce((a, b) => a + b, 0) / values.length;
          return (
            <tr key={f.id}>
              <Td>{f.name}</Td>
              <Td className="num text-right">{values.length}</Td>
              <Td className="num text-right">{ms(values[values.length - 1])}</Td>
              <Td className="num text-right">{ms(avg)}</Td>
              <Td className="num text-right">{ms(Math.max(...values))}</Td>
            </tr>
          );
        })}
      </tbody>
    </Table>
  );
}
