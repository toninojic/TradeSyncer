import clsx from "clsx";
import { useState } from "react";
import { api } from "@/lib/api";
import { money } from "@/lib/format";
import { useLive } from "@/lib/live";
import type { AccountStatus, RiskStatus } from "@/lib/types";
import { Button, ConfirmDialog, useToast } from "./ui";

/** How much of the daily loss allowance is used. */
export function LossMeter({ risk, compact }: { risk: RiskStatus | undefined; compact?: boolean }) {
  if (!risk || risk.daily_loss_limit === null) return compact ? null : <span className="text-slate-500">Off</span>;
  const used = risk.loss_used ?? 0;
  const ratio = Math.min(1, used / risk.daily_loss_limit);
  const tone = risk.locked || ratio >= 1 ? "bg-rose-500" : ratio >= 0.75 ? "bg-amber-500" : "bg-emerald-500";
  return (
    <div className={clsx("num", compact ? "w-28" : "w-44")} title="Loss used / daily loss limit">
      <div className="h-1.5 overflow-hidden rounded-full bg-slate-800">
        <div className={clsx("h-full rounded-full", tone)} style={{ width: `${Math.max(ratio * 100, used > 0 ? 3 : 0)}%` }} />
      </div>
      <div className="mt-1 text-xs text-slate-400">
        {money(used)} / {money(risk.daily_loss_limit)}
      </div>
    </div>
  );
}

/** Button + confirmation to lift a daily-loss lock by hand. */
export function ReEnableButton({ account, size = "sm" }: { account: AccountStatus; size?: "sm" | "md" }) {
  const toast = useToast();
  const { refresh } = useLive();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const limit = account.risk?.daily_loss_limit ?? null;

  async function confirm() {
    setBusy(true);
    try {
      await api.post(`/api/accounts/${account.id}/risk/unlock`);
      toast("success", `${account.name}: copying re-enabled`);
      await refresh();
    } catch (err) {
      toast("error", (err as Error).message);
    } finally {
      setBusy(false);
      setOpen(false);
    }
  }

  return (
    <>
      <Button size={size} variant="primary" onClick={() => setOpen(true)}>
        Re-enable
      </Button>
      <ConfirmDialog
        open={open}
        title={`Re-enable copying for ${account.name}?`}
        confirmLabel="Re-enable"
        loading={busy}
        onClose={() => setOpen(false)}
        onConfirm={() => void confirm()}
        message={
          <>
            This account hit its daily loss limit today. Re-enabling gives it a fresh loss allowance
            {limit !== null && <> of {money(limit)}</>} for the rest of the trading day, measured from its current
            P&L. It starts copying from the leader's next trade.
          </>
        }
      />
    </>
  );
}
