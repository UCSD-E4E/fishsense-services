"use client";

import { useState, useTransition } from "react";
import type { Kind, Level } from "@/lib/members";
import { setSkillAction } from "./actions";

const LEVELS: { value: Level; label: string }[] = [
  { value: "learning", label: "Learning" },
  { value: "trusted", label: "Trusted" },
  { value: "expert", label: "Expert" },
];

/** One member's level in one kind, saved as it is chosen. */
export function SkillSelect({
  slug,
  userId,
  kind,
  level,
  label,
}: {
  slug: string;
  userId: string;
  kind: Kind;
  level: Level;
  label: string;
}) {
  const [value, setValue] = useState(level);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [pending, startTransition] = useTransition();

  return (
    <span className="inline-flex flex-col gap-1">
      <select
        aria-label={label}
        className="rounded-control border border-line bg-surface px-2 py-1 text-sm disabled:opacity-50"
        value={value}
        disabled={pending}
        onChange={(e) => {
          const next = e.target.value as Level;
          const before = value;
          setValue(next);
          setError(null);
          setSaved(false);
          startTransition(async () => {
            const result = await setSkillAction(slug, userId, kind, next);
            if (result.ok) setSaved(true);
            else {
              setValue(before);
              setError(result.error);
            }
          });
        }}
      >
        {LEVELS.map((l) => (
          <option key={l.value} value={l.value}>
            {l.label}
          </option>
        ))}
      </select>
      {error && <span className="text-xs text-error">{error}</span>}
      {saved && <span className="text-xs text-fg-2">Saved</span>}
    </span>
  );
}
