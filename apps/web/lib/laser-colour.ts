/**
 * The laser's colour, as the web shows it (0072; the owner, 2026-10-24).
 *
 * One rig shoots one colour for a whole dive. A dot's colour is what the laser
 * kernel read off its pixels (the automatic dot, the human path's laser
 * stage) or what a labeller chose. A mission's colours come as counts from
 * each source; when sources mostly read different colours the page says so,
 * because a majority across them would hide exactly the case worth looking at.
 */
import type { Schemas } from "./api/client";

export type LaserColours = Schemas["LaserColoursView"];
type Counts = Schemas["ColourCountsView"];

const SOURCES: { key: keyof LaserColours; label: string }[] = [
  { key: "labels", label: "Labellers" },
  { key: "predictions", label: "Laser stage" },
  { key: "automatic", label: "Automatic dot" },
];

/** How far apart the red and green channels were at the dot: fishsense-core's
 *  signed margin (positive for red) as a size, in 8-bit levels. */
function apart(margin: number): string {
  return `${Math.round(Math.abs(margin))} levels apart`;
}

/**
 * A dot's colour in words (the owner, 2026-10-26: the margin as a size, not a
 * signed number). A reading too close to call says so; "Not read" is for a
 * dot that gave no reading at all. ``source`` ``render``: read off the render
 * later by the backfill (0076), not with the dot.
 */
export function colourLabel(
  color: string | null | undefined,
  margin?: number | null,
  source?: string | null,
): string {
  if (color !== "red" && color !== "green") {
    return margin != null ? `Too close to call (${apart(margin)})` : "Not read";
  }
  const word = color === "red" ? "Red" : "Green";
  const said = margin != null ? `${word}, ${apart(margin)}` : word;
  return source === "render" ? `${said} (read from the render)` : said;
}

/** One row per source, labellers first. */
export function colourRows(
  colours: LaserColours,
): { source: string; red: number; green: number; unread: number; total: number }[] {
  return SOURCES.map(({ key, label }) => {
    const c: Counts = colours[key];
    return { source: label, red: c.red, green: c.green, unread: c.unread, total: c.red + c.green + c.unread };
  });
}

function mostly(c: Counts): "red" | "green" | null {
  if (c.red === c.green) return null;
  return c.red > c.green ? "red" : "green";
}

/** Two sources that read a colour mostly read different ones. */
export function sourcesDisagree(colours: LaserColours): boolean {
  const read = new Set(SOURCES.map(({ key }) => mostly(colours[key])).filter((c) => c !== null));
  return read.size > 1;
}
