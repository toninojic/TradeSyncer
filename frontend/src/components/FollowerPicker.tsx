import clsx from "clsx";
import type { Account } from "@/lib/types";
import { Badge } from "./ui";

export function FollowerPicker({
  followers,
  selected,
  onSelect,
}: {
  followers: Account[];
  selected: number | null;
  onSelect: (id: number) => void;
}) {
  return (
    <div className="flex flex-col gap-1">
      {followers.map((f) => (
        <button
          key={f.id}
          onClick={() => onSelect(f.id)}
          className={clsx(
            "flex items-center justify-between gap-2 rounded-md px-3 py-2 text-left text-sm transition-colors",
            selected === f.id ? "bg-slate-800 text-white" : "text-slate-300 hover:bg-slate-800/60",
          )}
        >
          <span className="truncate">{f.name}</span>
          {!f.enabled && <Badge>off</Badge>}
        </button>
      ))}
    </div>
  );
}
