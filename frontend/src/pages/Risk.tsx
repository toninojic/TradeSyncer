import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { FollowerPicker } from "@/components/FollowerPicker";
import { LossMeter, ReEnableButton } from "@/components/RiskControls";
import { Badge, Button, Card, Empty, Field, Input, PageHeader, Table, Td, Th, Toggle, useToast } from "@/components/ui";
import { api, queryString } from "@/lib/api";
import { RULE_LABEL, shortDateTime, smartTime } from "@/lib/format";
import { useLive, useLiveEvents } from "@/lib/live";
import type { Account, AccountStatus, RiskEvent, RiskRules } from "@/lib/types";

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

export function RiskPage() {
  const { snapshot } = useLive();
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: () => api.get<Account[]>("/api/accounts") });
  const followers = useMemo(() => (accounts.data ?? []).filter((a) => a.role === "follower"), [accounts.data]);
  const names = useMemo(() => Object.fromEntries((accounts.data ?? []).map((a) => [a.id, a.name])), [accounts.data]);
  const [selected, setSelected] = useState<number | null>(null);
  const [todayOnly, setTodayOnly] = useState(true);

  const events = useQuery({
    queryKey: ["risk-events", todayOnly],
    queryFn: () => api.get<RiskEvent[]>(`/api/risk/events${queryString({ day: todayOnly ? "today" : undefined, limit: 300 })}`),
    staleTime: 0, // events can arrive while this page is closed: always reload when it opens
  });
  useLiveEvents(["risk_event"], () => void events.refetch(), 400);

  useEffect(() => {
    if (selected === null && followers.length) setSelected(followers[0].id);
  }, [followers, selected]);

  const live = (snapshot?.accounts ?? []).filter((a) => a.role === "follower" && a.enabled);

  return (
    <div className="space-y-5">
      <PageHeader
        title="Risk"
        description={
          snapshot ? (
            <>
              Trading day <span className="num text-slate-200">{snapshot.trading_day}</span> (CME session). Daily P&L,
              loss limits and locks reset <span className="text-slate-200">{shortDateTime(snapshot.next_reset)}</span>{" "}
              your time (17:00 Chicago).
            </>
          ) : undefined
        }
      />

      <Card title="Today" description="Live state of every enabled follower's rules.">
        {live.length === 0 ? (
          <Empty>No enabled followers.</Empty>
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Follower</Th>
                <Th>State</Th>
                <Th>Daily loss used</Th>
                <Th>Position / max</Th>
                <Th>Trading hours (ET)</Th>
                <Th>Triggered today</Th>
                <Th />
              </tr>
            </thead>
            <tbody>
              {live.map((a) => (
                <TodayRow key={a.id} account={a} />
              ))}
            </tbody>
          </Table>
        )}
      </Card>

      {followers.length === 0 ? (
        <Empty>No follower accounts.</Empty>
      ) : (
        <div className="grid gap-5 lg:grid-cols-[220px_1fr]">
          <Card title="Rules for" bodyClassName="p-2">
            <FollowerPicker followers={followers} selected={selected} onSelect={setSelected} />
          </Card>
          {selected !== null && <RiskForm key={selected} accountId={selected} name={names[selected] ?? ""} />}
        </div>
      )}

      <Card
        title="Triggered rules"
        description="Each rule is listed once per follower per trading day; the count is in the table above."
        actions={<Toggle checked={todayOnly} onChange={setTodayOnly} label="Today only" />}
      >
        {!events.data?.length ? (
          <Empty>{todayOnly ? "No rule has been triggered today." : "No rule has been triggered."}</Empty>
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
                  <Td className="num text-xs text-slate-400">{smartTime(e.ts)}</Td>
                  <Td className="num text-xs">{e.trading_day}</Td>
                  <Td>{names[e.account_id] ?? `#${e.account_id}`}</Td>
                  <Td>
                    <Badge tone={e.rule === "daily_loss_limit" ? "red" : "amber"}>{RULE_LABEL[e.rule] ?? e.rule}</Badge>
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

function TodayRow({ account }: { account: AccountStatus }) {
  const risk = account.risk;
  const maxHeld = account.positions.reduce((m, p) => Math.max(m, Math.abs(p.qty)), 0);
  const triggered = Object.entries(risk?.triggered_today ?? {});
  return (
    <tr>
      <Td className="font-medium text-slate-100">{account.name}</Td>
      <Td>
        {!risk ? (
          <Badge>No rules</Badge>
        ) : risk.locked ? (
          <span title={risk.reason ?? undefined}>
            <Badge tone="red" dot>
              Locked until next trading day
            </Badge>
          </span>
        ) : account.copy?.reduce_only ? (
          <Badge tone="amber" dot>
            Exits only
          </Badge>
        ) : (
          <Badge tone="green" dot>
            Active
          </Badge>
        )}
      </Td>
      <Td>
        <LossMeter risk={risk} />
      </Td>
      <Td className="num">
        {risk?.max_position != null ? (
          <span className={maxHeld >= risk.max_position ? "text-amber-300" : "text-slate-200"}>
            {maxHeld} / {risk.max_position}
          </span>
        ) : (
          <span className="text-slate-500">Off</span>
        )}
      </Td>
      <Td>
        {risk?.trading_hours_enabled ? (
          <span className="flex items-center gap-2">
            <span className="num text-slate-200">
              {risk.trading_start}–{risk.trading_end}
            </span>
            <Badge tone={risk.in_trading_hours ? "green" : "amber"}>{risk.in_trading_hours ? "Open" : "Closed"}</Badge>
          </span>
        ) : (
          <span className="text-slate-500">Off</span>
        )}
      </Td>
      <Td>
        {triggered.length === 0 ? (
          <span className="text-slate-500">–</span>
        ) : (
          <span className="flex flex-wrap gap-1">
            {triggered.map(([rule, count]) => (
              <Badge key={rule} tone={rule === "daily_loss_limit" ? "red" : "amber"}>
                {RULE_LABEL[rule] ?? rule} ×{count}
              </Badge>
            ))}
          </span>
        )}
      </Td>
      <Td className="text-right">{risk?.locked && <ReEnableButton account={account} />}</Td>
    </tr>
  );
}

function RiskForm({ accountId, name }: { accountId: number; name: string }) {
  const toast = useToast();
  const queryClient = useQueryClient();
  const { refresh } = useLive();
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
      toast("success", `Risk rules saved for ${name}. They apply immediately.`);
      await refresh();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card title={name} actions={<Button variant="primary" onClick={() => void save()} loading={saving}>Save</Button>}>
      <div className="grid gap-5 md:grid-cols-2">
        <Field
          label="Daily loss limit (USD)"
          hint="Realized + unrealized P&L of the trading day. When it is reached the follower is flattened and stops copying until the next trading day. Empty = off."
        >
          <Input type="number" min="1" step="50" value={lossText} placeholder="Off" onChange={(e) => setLossText(e.target.value)} />
        </Field>
        <Field
          label="Max position (contracts)"
          hint="Hard cap per symbol. The follower holds at most this many contracts even when the leader holds more. Empty = off."
        >
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
          <p className="text-xs text-slate-500">
            Outside these hours exits are still copied (closing, SL/TP), so an open position stays managed. New entries
            are not copied, and the follower does not join a trade that is already open when the window starts.
          </p>
        </div>
      </div>
      {error && <p className="mt-3 rounded-md bg-rose-950 px-3 py-2 text-sm text-rose-300">{error}</p>}
    </Card>
  );
}
