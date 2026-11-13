import { Badge, type Tone } from "./ui";
import type { CopierState, SyncStatus } from "@/lib/types";

const COPIER: Record<CopierState, { tone: Tone; label: string }> = {
  running: { tone: "green", label: "Running" },
  paused: { tone: "amber", label: "Paused" },
  killed: { tone: "red", label: "Killed" },
  no_leader: { tone: "slate", label: "No leader" },
};

export function CopierBadge({ state }: { state: CopierState }) {
  const c = COPIER[state];
  return (
    <Badge tone={c.tone} dot>
      {c.label}
    </Badge>
  );
}

const SYNC: Record<SyncStatus, { tone: Tone; label: string }> = {
  in_sync: { tone: "green", label: "In sync" },
  pending: { tone: "sky", label: "Syncing" },
  out_of_sync: { tone: "red", label: "Out of sync" },
  waiting_for_leader_flat: { tone: "amber", label: "Waits for leader flat" },
  paused: { tone: "amber", label: "Paused" },
  disabled: { tone: "slate", label: "Copying off" },
  disconnected: { tone: "red", label: "Disconnected" },
};

export function SyncBadge({ status }: { status: SyncStatus | undefined }) {
  if (!status) return <Badge>–</Badge>;
  const s = SYNC[status] ?? { tone: "slate" as Tone, label: status };
  return (
    <Badge tone={s.tone} dot>
      {s.label}
    </Badge>
  );
}

export function ConnectionBadge({ connection }: { connection: string }) {
  const tone: Tone =
    connection === "connected" ? "green" : connection === "connecting" ? "sky" : connection === "error" ? "red" : "slate";
  const label = connection.charAt(0).toUpperCase() + connection.slice(1);
  return (
    <Badge tone={tone} dot>
      {label}
    </Badge>
  );
}

const RECORD_TONES: Record<string, Tone> = {
  filled: "green",
  ok: "green",
  working: "sky",
  partially_filled: "sky",
  sent: "sky",
  cancelled: "slate",
  skipped: "slate",
  rejected: "red",
  failed: "red",
  blocked: "violet",
};

export function RecordStatusBadge({ status }: { status: string }) {
  return <Badge tone={RECORD_TONES[status] ?? "slate"}>{status.replace(/_/g, " ")}</Badge>;
}

export function LevelBadge({ level }: { level: string }) {
  const tone: Tone = level === "error" ? "red" : level === "warning" ? "amber" : "slate";
  return <Badge tone={tone}>{level}</Badge>;
}

export function RoleBadge({ role }: { role: string }) {
  return <Badge tone={role === "leader" ? "violet" : "sky"}>{role}</Badge>;
}
