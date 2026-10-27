// The website is written in American English (the owner, 2026-10-26: "I am
// american, use american spelling"), in what it shows and in its code. This
// scans the web's own source for British spellings so they can't drift back
// in. What isn't ours to spell stays as its owner spells it, named below:
//  * the API's schema names (`LaserColoursView`, `ColourCountsView`), which
//    come from the API's Python;
//  * Label Studio's `was_cancelled` field;
//  * HTML's own `aria-labelledby`.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";

const ROOT = join(__dirname, "..");
const SCANNED = ["app", "lib"];
const SKIPPED = new Set(["__fixtures__", "node_modules"]);
const SKIPPED_FILES = new Set(["lib/api/schema.d.ts", "lib/american-spelling.test.ts"]);

/** Spelled by someone else: left exactly as they spell it. */
const THEIRS = ["LaserColoursView", "ColourCountsView", "was_cancelled", "aria-labelledby"];

/** British spellings, each with its American one. */
const BRITISH: [RegExp, string][] = [
  [/colour/i, "color"],
  [/behaviour/i, "behavior"],
  [/honour/i, "honor"],
  [/neighbour/i, "neighbor"],
  [/favour/i, "favor"],
  [/flavour/i, "flavor"],
  [/harbour/i, "harbor"],
  [/metre/i, "meter"],
  [/\bcentr(e|ed|es|ing)\b/i, "center"],
  [/labell(ed|ing|er)/i, "labeled, labeling, labeler"],
  [/cancell(ed|ing)/i, "canceled, canceling"],
  [/travell(ed|ing|er)/i, "traveled"],
  [/modell(ed|ing|er)/i, "modeled"],
  [/judgement/i, "judgment"],
  [/\bgrey/i, "gray"],
  [/catalogue/i, "catalog"],
  [/\bdialogue/i, "dialog"],
  [/licence/i, "license"],
  [/defence|offence/i, "defense, offense"],
  [/artefact/i, "artifact"],
  [/\bwhilst\b|\bamongst\b/i, "while, among"],
  [/\b(organ|recogn|summar|normal|author|custom|final|minim|maxim|initial|visual|categor|apolog|util|synchron|serial|standard|real|prioriti|optim)is(e|ed|es|ing|ation|ations)\b/i, "-ize, -ization"],
  [/\banalys(e|ed|es|ing)\b/i, "analyze"],
];

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (SKIPPED.has(name)) return [];
    if (statSync(path).isDirectory()) return sources(path);
    return /\.(ts|tsx|css)$/.test(name) ? [path] : [];
  });
}

/** Every British spelling in `text`, as "line: word (use ...)". */
function britishSpellings(text: string): string[] {
  let scrubbed = text;
  for (const word of THEIRS) scrubbed = scrubbed.split(word).join("");
  const found: string[] = [];
  scrubbed.split("\n").forEach((line, i) => {
    for (const [pattern, american] of BRITISH) {
      const m = line.match(pattern);
      if (m) found.push(`${i + 1}: ${m[0]} (use ${american})`);
    }
  });
  return found;
}

describe("American spelling", () => {
  it("finds British spellings, and leaves alone what others spell", () => {
    expect(britishSpellings("the laser colour\nis centred")).toEqual([
      "1: colour (use color)",
      "2: centred (use center)",
    ]);
    expect(britishSpellings("Labellers organised it")).toHaveLength(2);
    expect(britishSpellings('Schemas["LaserColoursView"]; was_cancelled; aria-labelledby')).toEqual([]);
    expect(britishSpellings("color, center, labeled, optimistically, realize")).toEqual([]);
  });

  it("is how the website is written", () => {
    const found = SCANNED.flatMap((dir) => sources(join(ROOT, dir)))
      .filter((path) => !SKIPPED_FILES.has(relative(ROOT, path)))
      .flatMap((path) => britishSpellings(readFileSync(path, "utf8")).map((hit) => `${relative(ROOT, path)}:${hit}`));
    expect(found).toEqual([]);
  });
});
