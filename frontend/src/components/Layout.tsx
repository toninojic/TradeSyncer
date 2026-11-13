import clsx from "clsx";
import {
  Activity,
  FlaskConical,
  LayoutDashboard,
  ListOrdered,
  LogOut,
  Menu,
  ScrollText,
  Settings2,
  ShieldAlert,
  Users,
} from "lucide-react";
import { useState, type ReactNode } from "react";
import { NavLink } from "react-router-dom";
import { useAuth } from "@/lib/auth";
import { useLive } from "@/lib/live";
import { CopierBadge } from "./status";

const NAV = [
  { to: "/", label: "Dashboard", icon: LayoutDashboard, end: true },
  { to: "/accounts", label: "Accounts", icon: Users },
  { to: "/copy-settings", label: "Copy settings", icon: Settings2 },
  { to: "/risk", label: "Risk", icon: ShieldAlert },
  { to: "/trades", label: "Trade log", icon: ListOrdered },
  { to: "/logs", label: "System log", icon: ScrollText },
  { to: "/simulator", label: "Simulator", icon: FlaskConical },
];

export function Layout({ children }: { children: ReactNode }) {
  const { state, logout } = useAuth();
  const { snapshot, connected, recentAlerts } = useLive();
  const [menuOpen, setMenuOpen] = useState(false);
  const user = state.status === "authenticated" ? state.user : "";
  const errorCount = recentAlerts.filter((a) => a.level === "error" && Date.now() / 1000 - a.ts < 3600).length;

  const nav = (
    <nav className="flex flex-col gap-1 p-3">
      {NAV.map(({ to, label, icon: Icon, end }) => (
        <NavLink
          key={to}
          to={to}
          end={end}
          onClick={() => setMenuOpen(false)}
          className={({ isActive }) =>
            clsx(
              "flex items-center gap-3 rounded-md px-3 py-2 text-sm transition-colors",
              isActive ? "bg-slate-800 text-white" : "text-slate-400 hover:bg-slate-800/60 hover:text-slate-100",
            )
          }
        >
          <Icon size={17} />
          <span className="flex-1">{label}</span>
          {to === "/logs" && errorCount > 0 && (
            <span className="rounded-full bg-rose-600 px-1.5 text-[10px] font-semibold text-white">{errorCount}</span>
          )}
        </NavLink>
      ))}
    </nav>
  );

  return (
    <div className="flex min-h-screen">
      <aside className="hidden w-56 shrink-0 border-r border-slate-800 bg-slate-950 lg:block">
        <Brand />
        {nav}
      </aside>
      {menuOpen && (
        <div className="fixed inset-0 z-40 bg-black/60 lg:hidden" onClick={() => setMenuOpen(false)}>
          <aside className="h-full w-60 border-r border-slate-800 bg-slate-950" onClick={(e) => e.stopPropagation()}>
            <Brand />
            {nav}
          </aside>
        </div>
      )}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex h-14 items-center gap-3 border-b border-slate-800 bg-slate-950/90 px-4 backdrop-blur">
          <button className="rounded p-1.5 text-slate-300 hover:bg-slate-800 lg:hidden" onClick={() => setMenuOpen(true)} aria-label="Menu">
            <Menu size={20} />
          </button>
          <div className="flex items-center gap-2 text-sm text-slate-400">
            <span className="hidden sm:inline">Copier</span>
            {snapshot ? <CopierBadge state={snapshot.copier.state} /> : <span>…</span>}
          </div>
          <div className="ml-auto flex items-center gap-4">
            <span
              className={clsx("flex items-center gap-1.5 text-xs", connected ? "text-emerald-400" : "text-amber-400")}
              title={connected ? "Live updates connected" : "Reconnecting live updates…"}
            >
              <Activity size={14} />
              <span className="hidden sm:inline">{connected ? "Live" : "Reconnecting"}</span>
            </span>
            <span className="hidden text-sm text-slate-400 sm:inline">{user}</span>
            <button
              onClick={() => void logout()}
              className="flex items-center gap-1.5 rounded px-2 py-1 text-sm text-slate-400 hover:bg-slate-800 hover:text-white"
            >
              <LogOut size={15} /> <span className="hidden sm:inline">Log out</span>
            </button>
          </div>
        </header>
        <main className="mx-auto w-full max-w-[1400px] flex-1 p-4 sm:p-6">{children}</main>
      </div>
    </div>
  );
}

function Brand() {
  return (
    <div className="flex h-14 items-center gap-2 border-b border-slate-800 px-5">
      <svg viewBox="0 0 32 32" className="h-6 w-6" aria-hidden>
        <rect width="32" height="32" rx="7" fill="#0f172a" />
        <path d="M7 21l6-6 4 4 8-9" stroke="#34d399" strokeWidth="3" fill="none" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
      <span className="font-semibold tracking-tight text-white">TradeCopier</span>
      <span className="ml-auto rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-amber-300">sim</span>
    </div>
  );
}
