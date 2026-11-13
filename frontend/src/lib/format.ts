export function money(value: number | null | undefined, opts: { sign?: boolean } = {}): string {
  if (value === null || value === undefined) return "–";
  const formatted = Math.abs(value).toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  if (value < 0) return `-${formatted}`;
  return opts.sign && value > 0 ? `+${formatted}` : formatted;
}

export function pnlClass(value: number | null | undefined): string {
  if (!value) return "text-slate-300";
  return value > 0 ? "text-emerald-400" : "text-rose-400";
}

export function price(value: number | null | undefined): string {
  if (value === null || value === undefined) return "–";
  return value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export function signedQty(qty: number): string {
  return qty > 0 ? `+${qty}` : String(qty);
}

export function time(ts: number | null | undefined, withMs = false): string {
  if (!ts) return "–";
  const d = new Date(ts * 1000);
  const base = d.toLocaleTimeString("en-GB", { hour12: false });
  return withMs ? `${base}.${String(d.getMilliseconds()).padStart(3, "0")}` : base;
}

export function dateTime(ts: number | null | undefined): string {
  if (!ts) return "–";
  const d = new Date(ts * 1000);
  return `${d.toLocaleDateString("en-CA")} ${time(ts, true)}`;
}

/** Time with milliseconds; the date is added only when it is not today. */
export function smartTime(ts: number | null | undefined): string {
  if (!ts) return "–";
  const d = new Date(ts * 1000);
  const today = new Date();
  return d.toDateString() === today.toDateString() ? time(ts, true) : dateTime(ts);
}

export function ms(value: number | null | undefined): string {
  if (value === null || value === undefined) return "–";
  return value < 10 ? `${value.toFixed(2)} ms` : `${Math.round(value)} ms`;
}

export const UNDERLYING_LABEL: Record<string, string> = {
  NASDAQ100: "NQ / MNQ",
  SP500: "ES / MES",
  DOW30: "YM / MYM",
  RUSSELL2000: "RTY / M2K",
};

export const UNDERLYING_SYMBOL: Record<string, string> = {
  NASDAQ100: "NQ",
  SP500: "ES",
  DOW30: "YM",
  RUSSELL2000: "RTY",
};

export const SYMBOLS = ["NQ", "MNQ", "ES", "MES", "YM", "MYM", "RTY", "M2K"] as const;

/** Same rounding rule as the engine: floor(|qty| x multiplier x ratio), capped. */
export function followerTarget(leaderQty: number, multiplier: number, ratio: number, max: number | null): number {
  const raw = Math.floor(Math.abs(leaderQty) * multiplier * ratio + 1e-9);
  const capped = max !== null ? Math.min(raw, max) : raw;
  return Math.sign(leaderQty) * capped;
}
