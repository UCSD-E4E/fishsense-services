/**
 * The laser's color, as the web shows it (0072; the owner, 2026-10-24).
 *
 * One rig shoots one color for a whole dive. A dot's color is what the laser
 * kernel read off its pixels (the automatic dot, the human path's laser
 * stage) or what a labeler chose. A mission's colors come as counts from
 * each source; when sources mostly read different colors the page says so,
 * because a majority across them would hide exactly the case worth looking at.
 */
import type { Schemas } from "./api/client";

export type LaserColors = Schemas["LaserColoursView"];
type Counts = Schemas["ColourCountsView"];

const SOURCES: { key: keyof LaserColors; label: string }[] = [
  { key: "labels", label: "Labelers" },
  { key: "predictions", label: "Laser stage" },
  { key: "automatic", label: "Automatic dot" },
];

/** A dot's color as a word; "Not read" when the dot gave no reading. */
export function colorLabel(color: string | null | undefined, margin?: number | null): string {
  if (color !== "red" && color !== "green") return "Not read";
  const word = color === "red" ? "Red" : "Green";
  return margin != null ? `${word} (margin ${margin.toFixed(2)})` : word;
}

/** One row per source, labelers first. */
export function colorRows(
  colors: LaserColors,
): { source: string; red: number; green: number; unread: number; total: number }[] {
  return SOURCES.map(({ key, label }) => {
    const c: Counts = colors[key];
    return { source: label, red: c.red, green: c.green, unread: c.unread, total: c.red + c.green + c.unread };
  });
}

function mostly(c: Counts): "red" | "green" | null {
  if (c.red === c.green) return null;
  return c.red > c.green ? "red" : "green";
}

/** Two sources that read a color mostly read different ones. */
export function sourcesDisagree(colors: LaserColors): boolean {
  const read = new Set(SOURCES.map(({ key }) => mostly(colors[key])).filter((c) => c !== null));
  return read.size > 1;
}
