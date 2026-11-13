import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Info } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { FollowerPicker } from "@/components/FollowerPicker";
import { Badge, Button, Card, Empty, Field, Input, PageHeader, Table, Td, Th, Toggle, useToast } from "@/components/ui";
import { api } from "@/lib/api";
import { dateTime } from "@/lib/format";
import type { Account, RiskEvent, RiskRules } from "@/lib/types";

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

export function RiskPage() {
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: () => api.get<Account[]>("/api/accounts") });
  const followers = useMemo(() => (accounts.data ?? []).filter((a) => a.role === "follower"), [accounts.data]);
  const names = useMemo(() => Object.fromEntries((accounts.data ?? []).map((a) => [a.id, a.name])), [accounts.data]);
  const events = useQuery({
    queryKey: ["risk-events"],
    queryFn: () => api.get<RiskEvent[]>("/api/risk/events?limit=200"),
    refetchInterval: 5000,
  });
  const [selected, setSelected] = useState<number | null>(null);

  useEffect(() => {
    if (selected === null && followers.length) setSelected(followers[0].id);
  }, [followers, selected]);

  return (
    <div className="space-y-5">
      <PageHeader title="Risk" description="Per-follower limits. The trading day follows the CME session (resets 17:00 Chicago time)." />
      <div className="flex items-start gap-3 rounded-lg border border-sky-900 bg-sky-950/40 px-4 py-3 text-sm text-sky-200">
        <Info size={18} className="mt-0.5 shrink-0" />
        <p>
          Rules are saved now and enforced from milestone 4: daily loss limit (realized + unrealized) flattens the
          follower and stops copying until the next trading day; max position caps every order; trading hours block
          new entries outside the window.
        </p>
      </div>
      {followers.length === 0 ? (
        <Empty>No follower accounts.</Empty>
      ) : (
        <div className="grid gap-5 lg:grid-cols-[220px_1fr]">
          <Card title="Followers" bodyClassName="p-2">
            <FollowerPicker followers={followers} selected={selected} onSelect={setSelected} />
          </Card>
          {selected !== null && <RiskForm key={selected} accountId={selected} name={names[selected] ?? ""} />}
        </div>
      )}
      <Card title="Triggered rules" description="Most recent first.">
        {!events.data?.length ? (
          <Empty>No rule has been triggered.</Empty>
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Time</Th>
                <Th>Trading day</Th>
                <Th>Account</Th>
                <Th>Rule</Th>
                <Th>Details</Th>
              </tr>
            </thead>
            <tbody>
              {events.data.map((e) => (
                <tr key={e.id}>
                  <Td className="num text-xs text-slate-400">{dateTime(e.ts)}</Td>
                  <Td className="num text-xs">{e.trading_day}</Td>
                  <Td>{names[e.account_id] ?? `#${e.account_id}`}</Td>
                  <Td>
                    <Badge tone="red">{e.rule.replace(/_/g, " ")}</Badge>
                  </Td>
                  <Td className="min-w-[260px] whitespace-normal text-slate-300">{e.message}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
    </div>
  );
}

function RiskForm({ accountId, name }: { accountId: number; name: string }) {
  const toast = useToast();
  const queryClient = useQueryClient();
  const rules = useQuery({ queryKey: ["risk-rules", accountId], queryFn: () => api.get<RiskRules>(`/api/accounts/${accountId}/risk-rules`) });
  const [form, setForm] = useState<RiskRules | null>(null);
  const [lossText, setLossText] = useState("");
  const [posText, setPosText] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (rules.data) {
      setForm(rules.data);
      setLossText(rules.data.daily_loss_limit === null ? "" : String(rules.data.daily_loss_limit));
      setPosText(rules.data.max_position === null ? "" : String(rules.data.max_position));
    }
  }, [rules.data]);

  if (!form) return <Card><Empty>Loading…</Empty></Card>;
  const update = (patch: Partial<RiskRules>) => setForm((f) => (f ? { ...f, ...patch } : f));

  async function save() {
    if (!form) return;
    setSaving(true);
    setError(null);
    try {
      const saved = await api.put<RiskRules>(`/api/accounts/${accountId}/risk-rules`, {
        ...form,
        daily_loss_limit: lossText.trim() === "" ? null : Number(lossText),
        max_position: posText.trim() === "" ? null : Number(posText),
      });
      queryClient.setQueryData(["risk-rules", accountId], saved);
      toast("success", `Risk rules saved for ${name}`);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card title={name} actions={<Button variant="primary" onClick={() => void save()} loading={saving}>Save</Button>}>
      <div className="grid gap-5 md:grid-cols-2">
        <Field label="Daily loss limit (USD)" hint="Realized + unrealized P&L of the trading day. Empty = off.">
          <Input type="number" min="1" step="50" value={lossText} placeholder="Off" onChange={(e) => setLossText(e.target.value)} />
        </Field>
        <Field label="Max position (contracts)" hint="Per symbol. Empty = off.">
          <Input type="number" min="1" step="1" value={posText} placeholder="Off" onChange={(e) => setPosText(e.target.value)} />
        </Field>
        <div className="space-y-3 md:col-span-2">
          <Toggle
            checked={form.trading_hours_enabled}
            onChange={(v) => update({ trading_hours_enabled: v })}
            label="Only copy new entries during these hours (New York time)"
          />
          <div className={`flex flex-wrap items-end gap-4 ${form.trading_hours_enabled ? "" : "opacity-50"}`}>
            <Field label="From (ET)">
              <Input type="time" value={form.trading_start} onChange={(e) => update({ trading_start: e.target.value })} disabled={!form.trading_hours_enabled} />
            </Field>
            <Field label="To (ET)">
              <Input type="time" value={form.trading_end} onChange={(e) => update({ trading_end: e.target.value })} disabled={!form.trading_hours_enabled} />
            </Field>
            <div className="flex flex-wrap gap-1.5 pb-1">
              {DAYS.map((d, i) => {
                const on = form.trading_days.includes(i);
                return (
                  <button
                    key={d}
                    type="button"
                    disabled={!form.trading_hours_enabled}
                    onClick={() =>
                      update({ trading_days: on ? form.trading_days.filter((x) => x !== i) : [...form.trading_days, i].sort() })
                    }
                    className={`h-8 w-11 rounded-md text-xs font-medium ${on ? "bg-sky-600 text-white" : "bg-slate-800 text-slate-400"}`}
                  >
                    {d}
                  </button>
                );
              })}
            </div>
          </div>
        </div>
      </div>
      {error && <p className="mt-3 rounded-md bg-rose-950 px-3 py-2 text-sm text-rose-300">{error}</p>}
    </Card>
  );
}
