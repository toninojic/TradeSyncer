import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { LevelBadge } from "@/components/status";
import { Button, Card, Empty, Input, PageHeader, Select, Table, Td, Th, Toggle } from "@/components/ui";
import { api, queryString } from "@/lib/api";
import { smartTime } from "@/lib/format";
import { useLiveEvents } from "@/lib/live";
import type { Account, LogEntry } from "@/lib/types";

const CATEGORIES = ["connection", "reconcile", "risk", "order", "engine", "kill_switch"];
const LEVELS = ["info", "warning", "error"] as const;

export function SystemLogPage() {
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: () => api.get<Account[]>("/api/accounts") });
  const names = useMemo(() => Object.fromEntries((accounts.data ?? []).map((a) => [a.id, a.name])), [accounts.data]);
  const [levels, setLevels] = useState<string[]>(["info", "warning", "error"]);
  const [category, setCategory] = useState("");
  const [account, setAccount] = useState("");
  const [searchText, setSearchText] = useState("");
  const [search, setSearch] = useState("");
  const [limit, setLimit] = useState(200);
  const [live, setLive] = useState(true);
  const [expanded, setExpanded] = useState<number | null>(null);

  useEffect(() => {
    const t = window.setTimeout(() => setSearch(searchText), 300);
    return () => window.clearTimeout(t);
  }, [searchText]);

  const params = { level: levels.join(","), category: category || undefined, account_id: account || undefined, search: search || undefined, limit };
  const logs = useQuery({
    queryKey: ["logs", params],
    queryFn: () => api.get<LogEntry[]>(`/api/logs${queryString(params)}`),
    enabled: levels.length > 0,
  });
  useLiveEvents(["log"], () => live && void logs.refetch(), 300);

  const toggleLevel = (level: string) =>
    setLevels((prev) => (prev.includes(level) ? prev.filter((l) => l !== level) : [...prev, level]));

  return (
    <div>
      <PageHeader title="System log & alerts" description="Connections, reconciliation fixes, rejected orders, rule breaches and kill switch events." />
      <Card className="mb-4" bodyClassName="flex flex-wrap items-end gap-3">
        <div className="flex flex-col gap-1">
          <span className="text-xs font-medium uppercase tracking-wide text-slate-400">Level</span>
          <div className="flex gap-1.5">
            {LEVELS.map((l) => (
              <button
                key={l}
                onClick={() => toggleLevel(l)}
                className={`h-9 rounded-md px-3 text-sm capitalize ${
                  levels.includes(l)
                    ? l === "error"
                      ? "bg-rose-600 text-white"
                      : l === "warning"
                        ? "bg-amber-600 text-white"
                        : "bg-slate-600 text-white"
                    : "bg-slate-800 text-slate-500"
                }`}
              >
                {l}
              </button>
            ))}
          </div>
        </div>
        <label className="flex flex-col gap-1 text-xs font-medium uppercase tracking-wide text-slate-400">
          Category
          <Select className="w-44 normal-case" value={category} onChange={(e) => setCategory(e.target.value)}>
            <option value="">All categories</option>
            {CATEGORIES.map((c) => <option key={c} value={c}>{c.replace("_", " ")}</option>)}
          </Select>
        </label>
        <label className="flex flex-col gap-1 text-xs font-medium uppercase tracking-wide text-slate-400">
          Account
          <Select className="w-48 normal-case" value={account} onChange={(e) => setAccount(e.target.value)}>
            <option value="">All accounts</option>
            {(accounts.data ?? []).map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
          </Select>
        </label>
        <label className="flex flex-col gap-1 text-xs font-medium uppercase tracking-wide text-slate-400">
          Search
          <Input className="w-56 normal-case" placeholder="text in message" value={searchText} onChange={(e) => setSearchText(e.target.value)} />
        </label>
        <div className="pb-1.5">
          <Toggle checked={live} onChange={setLive} label="Live" />
        </div>
      </Card>

      <Card bodyClassName="p-0">
        {levels.length === 0 ? (
          <Empty>Select at least one level.</Empty>
        ) : logs.isLoading ? (
          <Empty>Loading…</Empty>
        ) : !logs.data?.length ? (
          <Empty>No entries.</Empty>
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Time</Th>
                <Th>Level</Th>
                <Th>Category</Th>
                <Th>Account</Th>
                <Th>Message</Th>
              </tr>
            </thead>
            <tbody>
              {logs.data.map((entry) => (
                <tr
                  key={entry.id}
                  className={entry.data ? "cursor-pointer hover:bg-slate-800/40" : ""}
                  onClick={() => entry.data && setExpanded(expanded === entry.id ? null : entry.id ?? null)}
                >
                  <Td className="num whitespace-nowrap text-xs text-slate-400">{smartTime(entry.ts)}</Td>
                  <Td>
                    <LevelBadge level={entry.level} />
                  </Td>
                  <Td className="text-xs text-slate-400">{entry.category.replace("_", " ")}</Td>
                  <Td className="text-sm text-slate-300">{entry.account_id !== null ? names[entry.account_id] ?? `#${entry.account_id}` : "–"}</Td>
                  <Td className="min-w-[320px] whitespace-normal text-slate-200">
                    {entry.message}
                    {entry.data && expanded !== entry.id && <span className="ml-2 text-xs text-slate-500">(details)</span>}
                    {entry.data && expanded === entry.id && (
                      <pre className="mt-2 max-w-3xl overflow-x-auto rounded bg-slate-950 p-2 text-xs text-slate-400">
                        {JSON.stringify(entry.data, null, 2)}
                      </pre>
                    )}
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
        {logs.data && logs.data.length >= limit && (
          <div className="border-t border-slate-800 p-3 text-center">
            <Button size="sm" onClick={() => setLimit((l) => l + 200)}>
              Load more
            </Button>
          </div>
        )}
      </Card>
    </div>
  );
}
