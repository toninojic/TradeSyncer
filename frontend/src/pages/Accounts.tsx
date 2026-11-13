import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Pencil, PlugZap, Plus, Trash2 } from "lucide-react";
import { useState, type FormEvent } from "react";
import { ConnectionBadge, RoleBadge } from "@/components/status";
import {
  Badge,
  Button,
  Card,
  ConfirmDialog,
  Empty,
  Field,
  Input,
  Modal,
  PageHeader,
  Select,
  Table,
  Td,
  Th,
  Toggle,
  useToast,
} from "@/components/ui";
import { api } from "@/lib/api";
import { money } from "@/lib/format";
import { useLive } from "@/lib/live";
import type { Account, BrokerType, Role } from "@/lib/types";

interface TestResult {
  ok: boolean;
  message: string;
  latency_ms: number | null;
}

export function AccountsPage() {
  const toast = useToast();
  const queryClient = useQueryClient();
  const { snapshot, refresh } = useLive();
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: () => api.get<Account[]>("/api/accounts") });
  const [editing, setEditing] = useState<Account | "new" | null>(null);
  const [deleting, setDeleting] = useState<Account | null>(null);
  const [tests, setTests] = useState<Record<number, TestResult | "running">>({});

  const liveConnection = (a: Account) => snapshot?.accounts.find((s) => s.id === a.id)?.connection ?? a.connection;

  const invalidate = async () => {
    await queryClient.invalidateQueries({ queryKey: ["accounts"] });
    await refresh();
  };

  const toggleEnabled = useMutation({
    mutationFn: (a: Account) => api.put<Account>(`/api/accounts/${a.id}`, { enabled: !a.enabled }),
    onSuccess: (a) => {
      toast("success", `${a.name} ${a.enabled ? "enabled" : "disabled"}`);
      void invalidate();
    },
    onError: (err: Error) => toast("error", err.message),
  });

  const remove = useMutation({
    mutationFn: (a: Account) => api.del(`/api/accounts/${a.id}`),
    onSuccess: () => {
      toast("success", "Account deleted");
      setDeleting(null);
      void invalidate();
    },
    onError: (err: Error) => toast("error", err.message),
  });

  async function testConnection(a: Account) {
    setTests((t) => ({ ...t, [a.id]: "running" }));
    try {
      const result = await api.post<TestResult>(`/api/accounts/${a.id}/test-connection`);
      setTests((t) => ({ ...t, [a.id]: result }));
    } catch (err) {
      setTests((t) => ({ ...t, [a.id]: { ok: false, message: (err as Error).message, latency_ms: null } }));
    }
  }

  return (
    <div>
      <PageHeader
        title="Accounts"
        description="One enabled leader, any number of followers. Changes to role, broker or enabled state restart the copier (simulated positions are kept)."
        actions={
          <Button variant="primary" onClick={() => setEditing("new")}>
            <Plus size={16} /> Add account
          </Button>
        }
      />
      <Card bodyClassName="p-0">
        {accounts.isLoading ? (
          <Empty>Loading…</Empty>
        ) : !accounts.data?.length ? (
          <Empty>No accounts yet.</Empty>
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Name</Th>
                <Th>Role</Th>
                <Th>Broker</Th>
                <Th>Enabled</Th>
                <Th>Connection</Th>
                <Th>Credentials</Th>
                <Th>Test connection</Th>
                <Th />
              </tr>
            </thead>
            <tbody>
              {accounts.data.map((a) => {
                const test = tests[a.id];
                return (
                  <tr key={a.id}>
                    <Td>
                      <div className="font-medium text-slate-100">{a.name}</div>
                      <div className="text-xs text-slate-500">
                        #{a.id}
                        {a.broker_type === "sim" && ` · start balance ${money(a.sim_starting_balance)}`}
                      </div>
                    </Td>
                    <Td>
                      <RoleBadge role={a.role} />
                    </Td>
                    <Td>
                      <Badge tone={a.broker_type === "sim" ? "amber" : "sky"}>
                        {a.broker_type === "sim" ? "Simulated" : "Tradovate (stub)"}
                      </Badge>
                    </Td>
                    <Td>
                      <Toggle
                        checked={a.enabled}
                        disabled={toggleEnabled.isPending}
                        onChange={() => toggleEnabled.mutate(a)}
                      />
                    </Td>
                    <Td>
                      <ConnectionBadge connection={liveConnection(a)} />
                    </Td>
                    <Td className="text-xs text-slate-400">{a.has_credentials ? "Saved (encrypted)" : "–"}</Td>
                    <Td>
                      <div className="flex items-center gap-2">
                        <Button size="sm" onClick={() => void testConnection(a)} loading={test === "running"}>
                          <PlugZap size={14} /> Test
                        </Button>
                        {test && test !== "running" && (
                          <span className={`max-w-[260px] truncate text-xs ${test.ok ? "text-emerald-400" : "text-rose-400"}`} title={test.message}>
                            {test.message}
                            {test.latency_ms !== null && ` (${test.latency_ms} ms)`}
                          </span>
                        )}
                      </div>
                    </Td>
                    <Td className="text-right">
                      <div className="flex justify-end gap-1">
                        <Button size="sm" variant="ghost" onClick={() => setEditing(a)} aria-label={`Edit ${a.name}`}>
                          <Pencil size={14} />
                        </Button>
                        <Button size="sm" variant="ghost" onClick={() => setDeleting(a)} aria-label={`Delete ${a.name}`}>
                          <Trash2 size={14} className="text-rose-400" />
                        </Button>
                      </div>
                    </Td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
        )}
      </Card>

      {editing !== null && (
        <AccountForm
          account={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            void invalidate();
          }}
        />
      )}
      <ConfirmDialog
        open={deleting !== null}
        title={`Delete ${deleting?.name ?? ""}?`}
        danger
        confirmLabel="Delete"
        loading={remove.isPending}
        message="The account and its settings are removed. Trade and system log entries are kept."
        onClose={() => setDeleting(null)}
        onConfirm={() => deleting && remove.mutate(deleting)}
      />
    </div>
  );
}

const CREDENTIAL_FIELDS: { key: string; label: string; secret?: boolean }[] = [
  { key: "username", label: "Username" },
  { key: "password", label: "Password", secret: true },
  { key: "app_id", label: "App ID" },
  { key: "cid", label: "Client ID (cid)", secret: true },
  { key: "secret", label: "API secret", secret: true },
];

function AccountForm({ account, onClose, onSaved }: { account: Account | null; onClose: () => void; onSaved: () => void }) {
  const toast = useToast();
  const [name, setName] = useState(account?.name ?? "");
  const [role, setRole] = useState<Role>(account?.role ?? "follower");
  const [broker, setBroker] = useState<BrokerType>(account?.broker_type ?? "sim");
  const [enabled, setEnabled] = useState(account?.enabled ?? true);
  const [balance, setBalance] = useState(String(account?.sim_starting_balance ?? 50000));
  const [creds, setCreds] = useState<Record<string, string>>({ environment: "demo" });
  const [clearCreds, setClearCreds] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    const body: Record<string, unknown> = {
      name: name.trim(),
      role,
      broker_type: broker,
      enabled,
      sim_starting_balance: Number(balance) || 50000,
    };
    const filled = Object.fromEntries(Object.entries(creds).filter(([k, v]) => v && (k !== "environment" || broker === "tradovate")));
    if (broker === "tradovate") {
      const hasSecrets = CREDENTIAL_FIELDS.some((f) => creds[f.key]);
      if (clearCreds) body.credentials = {};
      else if (hasSecrets) body.credentials = filled;
    } else if (account?.has_credentials) {
      body.credentials = {};
    }
    setSaving(true);
    try {
      if (account) await api.put(`/api/accounts/${account.id}`, body);
      else await api.post("/api/accounts", body);
      toast("success", account ? "Account saved" : "Account created");
      onSaved();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal
      open
      wide
      onClose={onClose}
      title={account ? `Edit ${account.name}` : "Add account"}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" type="submit" form="account-form" loading={saving} disabled={!name.trim()}>
            Save
          </Button>
        </>
      }
    >
      <form id="account-form" onSubmit={submit} className="grid gap-4 sm:grid-cols-2">
        <Field label="Name" className="sm:col-span-2">
          <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. F4 - 2x MNQ" autoFocus />
        </Field>
        <Field label="Role" hint="Only one leader can be enabled.">
          <Select value={role} onChange={(e) => setRole(e.target.value as Role)}>
            <option value="follower">Follower</option>
            <option value="leader">Leader</option>
          </Select>
        </Field>
        <Field label="Broker" hint="Real brokers are not connected in the MVP.">
          <Select
            value={broker}
            onChange={(e) => {
              const value = e.target.value as BrokerType;
              setBroker(value);
              // The Tradovate adapter is a stub that cannot connect; keep new ones disabled.
              if (!account && value === "tradovate") setEnabled(false);
            }}
          >
            <option value="sim">Simulated</option>
            <option value="tradovate">Tradovate (stub, no real calls)</option>
          </Select>
        </Field>
        {broker === "sim" && (
          <Field label="Starting balance (USD)">
            <Input type="number" min={1} step={1000} value={balance} onChange={(e) => setBalance(e.target.value)} />
          </Field>
        )}
        <div className="flex items-end pb-1">
          <Toggle checked={enabled} onChange={setEnabled} label="Enabled" />
        </div>

        {broker === "tradovate" && (
          <div className="space-y-3 rounded-lg border border-slate-800 bg-slate-950/50 p-4 sm:col-span-2">
            <p className="text-xs text-slate-400">
              Credentials are encrypted before they are stored and are never shown again.
              {account?.has_credentials && " Leave the fields empty to keep the saved credentials."}
            </p>
            <div className="grid gap-3 sm:grid-cols-2">
              {CREDENTIAL_FIELDS.map((f) => (
                <Field key={f.key} label={f.label}>
                  <Input
                    type={f.secret ? "password" : "text"}
                    autoComplete="off"
                    value={creds[f.key] ?? ""}
                    onChange={(e) => setCreds((c) => ({ ...c, [f.key]: e.target.value }))}
                  />
                </Field>
              ))}
              <Field label="Environment">
                <Select value={creds.environment} onChange={(e) => setCreds((c) => ({ ...c, environment: e.target.value }))}>
                  <option value="demo">Demo</option>
                  <option value="live">Live</option>
                </Select>
              </Field>
            </div>
            {account?.has_credentials && (
              <Toggle checked={clearCreds} onChange={setClearCreds} label="Remove saved credentials" />
            )}
          </div>
        )}
        {error && <p className="rounded-md bg-rose-950 px-3 py-2 text-sm text-rose-300 sm:col-span-2">{error}</p>}
      </form>
    </Modal>
  );
}
