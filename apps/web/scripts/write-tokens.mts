// Writes app/tokens.css from docs/design/tokens.json: `npm run tokens:generate`.
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { tokensToCss } from "../lib/design-tokens.ts";

const here = path.dirname(fileURLToPath(import.meta.url));
const tokens = JSON.parse(
  readFileSync(path.resolve(here, "../../../docs/design/tokens.json"), "utf8"),
);
writeFileSync(path.resolve(here, "../app/tokens.css"), tokensToCss(tokens));
