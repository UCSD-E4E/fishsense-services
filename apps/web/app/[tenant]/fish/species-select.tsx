"use client";

import { useRouter } from "next/navigation";
import { speciesLabel, type SpeciesCount } from "@/lib/observations";

/**
 * Filter the Fish page by species (the owner, 2026-10-23), offering only the
 * species the tenant has. Choosing one goes to its first page; the device
 * filter is kept. A species the current device has none of stays chosen, so
 * the page says so rather than quietly dropping the filter.
 */
export function SpeciesSelect({
  species,
  current,
  hrefFor,
}: {
  species: SpeciesCount[];
  current?: string;
  /** The page's link for a species (none: every species), from the server. */
  hrefFor: Record<string, string>;
}) {
  const router = useRouter();
  const offered =
    current && !species.some((s) => s.scientific_name === current)
      ? [{ scientific_name: current, common_name: null, count: 0 }, ...species]
      : species;
  return (
    <label className="inline-flex items-center gap-2 text-sm">
      <span className="font-medium">Species</span>
      <select
        className="h-11 max-w-80 rounded-control border border-line bg-surface px-3 text-sm"
        value={current ?? ""}
        onChange={(e) => router.push(hrefFor[e.target.value] ?? hrefFor[""])}
      >
        <option value="">All species</option>
        {offered.map((s) => (
          <option key={s.scientific_name} value={s.scientific_name}>
            {speciesLabel(s)}
          </option>
        ))}
      </select>
    </label>
  );
}
