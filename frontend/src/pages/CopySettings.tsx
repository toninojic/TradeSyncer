import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, Plus, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { FollowerPicker } from "@/components/FollowerPicker";
import { Button, Card, Empty, Field, Input, PageHeader, Select, Table, Td, Th, Toggle, useToast } from "@/components/ui";
import { api } from "@/lib/api";
import { SYMBOLS, followerTarget } from "@/lib/format";
import { useLive } from "@/lib/live";
import type { Account, ContractInfo, CopySettings, Mapping, MismatchMode } from "@/lib/types";

export function CopySettingsPage() {
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: () => api.get<Account[]>("/api/accounts") });
  const followers = useMemo(() => (accounts.data ?? []).filter((a) => a.role === "follower"), [accounts.data]);
  const [selected, setSelected] = useState<number | null>(null);

  useEffect(() => {
    if (selected === null && followers.length) setSelected(followers[0].id);
  }, [followers, selected]);

  return (
    <div>
      <PageHeader
        title="Copy settings"
        description="How each follower sizes and copies the leader's trades. Changes apply immediately to the running copier."
      />
      {accounts.isLoading ? (
        <Empty>Loading…</Empty>
      ) : followers.length === 0 ? (
        <Empty>No follower accounts. Add one on the Accounts page.</Empty>
      ) : (
        <div className="grid gap-5 lg:grid-cols-[220px_1fr]">
          <Card title="Followers" bodyClassName="p-2">
            <FollowerPicker followers={followers} selected={selected} onSelect={setSelected} />
          </Card>
          {selected !== null && <CopySettingsForm key={selected} accountId={selected} name={followers.find((f) => f.id === selected)?.name ?? ""} />}
        </div>
      )}
    </div>
  );
}

function CopySettingsForm({ accountId, name }: { accountId: number; name: string }) {
  const toast = useToast();
  const queryClient = useQueryClient();
  const { refresh } = useLive();
  const meta = useQuery({ queryKey: ["meta"], queryFn: () => api.get<{ contracts: ContractInfo[] }>("/api/meta"), staleTime: Infinity });
  const settings = useQuery({
    queryKey: ["copy-settings", accountId],
    queryFn: () => api.get<CopySettings>(`/api/accounts/${accountId}/copy-settings`),
  });
  const [form, setForm] = useState<CopySettings | null>(null);
  const [maxText, setMaxText] = useState("");
  const [multText, setMultText] = useState("1");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (settings.data) {
      setForm(settings.data);
      setMaxText(settings.data.max_contracts === null ? "" : String(settings.data.max_contracts));
      setMultText(String(settings.data.multiplier));
    }
  }, [settings.data]);

  const underlying = useMemo(
    () => Object.fromEntries((meta.data?.contracts ?? []).map((c) => [c.symbol, c.underlying])),
    [meta.data],
  );

  if (!form) return <Card><Empty>Loading…</Empty></Card>;

  const multiplier = Number(multText);
  const maxContracts = maxText.trim() === "" ? null : Number(maxText);
  const problems: string[] = [];
  if (!(multiplier > 0)) problems.push("Multiplier must be greater than 0.");
  if (maxContracts !== null && !(Number.isInteger(maxContracts) && maxContracts >= 1)) problems.push("Max contracts must be a whole number ≥ 1 (or empty for no cap).");
  const leaderSymbols = new Set<string>();
  form.mappings.forEach((m, i) => {
    if (leaderSymbols.has(m.leader_symbol)) problems.push(`Row ${i + 1}: ${m.leader_symbol} is mapped twice.`);
    leaderSymbols.add(m.leader_symbol);
    if (underlying[m.leader_symbol] && underlying[m.leader_symbol] !== underlying[m.follower_symbol])
      problems.push(`Row ${i + 1}: ${m.leader_symbol} and ${m.follower_symbol} are different indexes.`);
    if (!(m.ratio > 0)) problems.push(`Row ${i + 1}: ratio must be greater than 0.`);
  });

  const update = (patch: Partial<CopySettings>) => setForm((f) => (f ? { ...f, ...patch } : f));
  const updateMapping = (index: number, patch: Partial<Mapping>) =>
    update({ mappings: form.mappings.map((m, i) => (i === index ? { ...m, ...patch } : m)) });

  async function save() {
    if (!form) return;
    setSaving(true);
    setError(null);
    try {
      const saved = await api.put<CopySettings>(`/api/accounts/${accountId}/copy-settings`, {
        ...form,
        multiplier,
        max_contracts: maxContracts,
      });
      queryClient.setQueryData(["copy-settings", accountId], saved);
      toast("success", `Copy settings saved for ${name}`);
      await refresh();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSaving(false);
    }
  }

  const previewSymbols = form.mappings.length ? form.mappings : [{ leader_symbol: "NQ", follower_symbol: "NQ", ratio: 1 }];

  return (
    <div className="space-y-5">
      <Card
        title={name}
        actions={
          <Button variant="primary" onClick={() => void save()} loading={saving} disabled={problems.length > 0}>
            Save
          </Button>
        }
      >
        <div className="grid gap-5 md:grid-cols-2">
          <Field label="Multiplier" hint="Follower size = floor(leader size × multiplier × ratio).">
            <Input type="number" step="0.1" min="0" value={multText} onChange={(e) => setMultText(e.target.value)} />
          </Field>
          <Field label="Max contracts" hint="Cap per follower symbol. Empty = no cap.">
            <Input type="number" step="1" min="1" value={maxText} placeholder="No cap" onChange={(e) => setMaxText(e.target.value)} />
          </Field>
          <Field label="When the follower drifts from the leader">
            <Select value={form.mismatch_mode} onChange={(e) => update({ mismatch_mode: e.target.value as MismatchMode })}>
              <option value="auto_correct">Auto-correct (send orders to fix it)</option>
              <option value="alert_only">Alert only (log it, do not trade)</option>
            </Select>
          </Field>
          <div className="flex flex-col justify-end gap-3 pb-1">
            <Toggle checked={form.copy_enabled} onChange={(v) => update({ copy_enabled: v })} label="Copying enabled" />
            <Toggle checked={form.copy_sl_tp} onChange={(v) => update({ copy_sl_tp: v })} label="Copy stop loss / take profit orders" />
          </div>
        </div>
      </Card>

      <Card
        title="Contract mapping"
        description="Symbols without a row are copied 1:1. Example: NQ → MNQ with ratio 10 trades 10 MNQ per NQ."
        actions={
          <Button size="sm" onClick={() => update({ mappings: [...form.mappings, { leader_symbol: "NQ", follower_symbol: "MNQ", ratio: 10 }] })}>
            <Plus size={14} /> Add mapping
          </Button>
        }
      >
        {form.mappings.length === 0 ? (
          <p className="text-sm text-slate-500">No mappings: every symbol is copied as the same symbol.</p>
        ) : (
          <div className="space-y-2">
            {form.mappings.map((m, i) => (
              <div key={i} className="flex flex-wrap items-center gap-2">
                <Select className="w-28" value={m.leader_symbol} onChange={(e) => updateMapping(i, { leader_symbol: e.target.value })} aria-label="Leader symbol">
                  {SYMBOLS.map((s) => <option key={s}>{s}</option>)}
                </Select>
                <ArrowRight size={16} className="text-slate-500" />
                <Select className="w-28" value={m.follower_symbol} onChange={(e) => updateMapping(i, { follower_symbol: e.target.value })} aria-label="Follower symbol">
                  {SYMBOLS.filter((s) => !underlying[m.leader_symbol] || underlying[s] === underlying[m.leader_symbol]).map((s) => (
                    <option key={s}>{s}</option>
                  ))}
                </Select>
                <span className="text-sm text-slate-400">ratio</span>
                <Input className="w-24" type="number" step="0.5" min="0" value={m.ratio} onChange={(e) => updateMapping(i, { ratio: Number(e.target.value) })} aria-label="Ratio" />
                <Button size="sm" variant="ghost" onClick={() => update({ mappings: form.mappings.filter((_, j) => j !== i) })} aria-label="Remove mapping">
                  <Trash2 size={14} className="text-rose-400" />
                </Button>
              </div>
            ))}
          </div>
        )}
        {(problems.length > 0 || error) && (
          <ul className="mt-3 space-y-1 rounded-md bg-rose-950/60 px-3 py-2 text-sm text-rose-300">
            {problems.map((p) => <li key={p}>{p}</li>)}
            {error && <li>{error}</li>}
          </ul>
        )}
      </Card>

      <Card title="Sizing preview" description="What this follower holds for a given leader position (with the values above, before saving).">
        <Table>
          <thead>
            <tr>
              <Th>Leader position</Th>
              {[1, 2, 3, 4, 5, 10].map((q) => <Th key={q} className="text-right">{q}</Th>)}
            </tr>
          </thead>
          <tbody>
            {previewSymbols.map((m) => (
              <tr key={m.leader_symbol}>
                <Td>
                  <span className="text-slate-300">{m.leader_symbol}</span>
                  <span className="text-slate-500"> → {m.follower_symbol}</span>
                </Td>
                {[1, 2, 3, 4, 5, 10].map((q) => {
                  const t = multiplier > 0 && m.ratio > 0 ? followerTarget(q, multiplier, m.ratio, maxContracts) : 0;
                  return (
                    <Td key={q} className={`num text-right ${t === 0 ? "text-slate-600" : "text-slate-100"}`}>
                      {t}
                    </Td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </Table>
      </Card>
    </div>
  );
}
