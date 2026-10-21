/**
 * How Data's pages show deployments and missions (docs/design/README.md, the
 * "Web · Data" board): a deployment's missions by day, what it holds, and how
 * it was made -- an inferred trip must never pass for a planned one.
 */
import type { MissionRow } from "./mission-facts";

export type Day = { date: string; label: string; missions: MissionRow[] };

/** By the camera's own date: its local clock is stored as UTC, so the UTC date
 *  is the dive's (`deployment_inference.py`). */
export function byDay(missions: MissionRow[]): Day[] {
  const days = new Map<string, MissionRow[]>();
  for (const m of [...missions].sort((a, b) => a.started_at.localeCompare(b.started_at))) {
    const date = m.started_at.slice(0, 10);
    days.set(date, [...(days.get(date) ?? []), m]);
  }
  return [...days.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([date, list]) => ({ date, label: dayLabel(date), missions: list }));
}

function dayLabel(date: string): string {
  return new Date(`${date}T00:00:00Z`).toLocaleDateString("en-GB", {
    timeZone: "UTC",
    weekday: "short",
    day: "numeric",
    month: "short",
    year: "numeric",
  }).replace(",", "");
}

export function deploymentFacts(missions: MissionRow[]): {
  missions: number;
  frames: number;
  cameras: string[];
} {
  const cameras = new Set(missions.map((m) => m.device_name).filter((n): n is string => !!n));
  return {
    missions: missions.length,
    frames: missions.reduce((sum, m) => sum + m.captures, 0),
    cameras: [...cameras].sort(),
  };
}

export type Provenance = { kind: "dataset" | "inferred"; label: string };

/** `infer-deployments` writes its note on each trip it makes. */
const INFERRED = /^inferred from folder names and capture dates/;

export function provenance(d: { source_path: string | null; notes: string | null }): Provenance | null {
  if (d.source_path) return { kind: "dataset", label: "From its e4edm dataset" };
  if (d.notes && INFERRED.test(d.notes)) {
    return { kind: "inferred", label: "Inferred from folder names and dates" };
  }
  return null;
}
