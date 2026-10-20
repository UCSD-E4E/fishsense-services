// Writes app/tokens.css from docs/design/tokens.json: `npm run tokens:generate`.
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { tokensToCss } from "../lib/design-tokens.ts";

const here = path.dirname(new URL(import.meta.url).pathname);
const tokens = JSON.parse(
  readFileSync(path.resolve(here, "../../../docs/design/tokens.json"), "utf8"),
);
writeFileSync(path.resolve(here, "../app/tokens.css"), tokensToCss(tokens));
