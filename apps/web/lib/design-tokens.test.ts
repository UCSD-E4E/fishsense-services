import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { tokensToCss, type DesignTokens } from "./design-tokens";

const repo = path.resolve(__dirname, "../../..");
const tokens: DesignTokens = JSON.parse(
  readFileSync(path.join(repo, "docs/design/tokens.json"), "utf8"),
);

describe("the web's theme is docs/design/tokens.json", () => {
  it("app/tokens.css is generated from it, unchanged", () => {
    // The image builds from apps/web alone, so the CSS is committed; this
    // keeps it the JSON's (regenerate with `npm run tokens:generate`).
    const committed = readFileSync(path.join(__dirname, "../app/tokens.css"), "utf8");

    expect(committed).toBe(tokensToCss(tokens));
  });

  it("gives every colour a variable in both themes", () => {
    const css = tokensToCss(tokens);
    const [light, dark] = css.split("@media (prefers-color-scheme: dark)");

    for (const name of Object.keys(tokens.color.light)) {
      const variable = `--fs-${kebab(name)}:`;
      expect(light).toContain(variable);
      expect(dark).toContain(variable);
    }
  });

  it("follows the system theme, and a page can pin one", () => {
    const css = tokensToCss(tokens);

    expect(css).toContain(":root:not([data-theme=\"light\"])");
    expect(css).toContain(":root[data-theme=\"dark\"]");
    expect(css).toContain(`--fs-ground: ${tokens.color.dark.ground}`);
    expect(css).toContain(`--fs-ground: ${tokens.color.light.ground}`);
  });

  it("carries the overlay colours, shared by both themes", () => {
    const css = tokensToCss(tokens);

    expect(css).toContain(`--fs-overlay-snout: ${tokens.color.overlay.snout}`);
    expect(css).toContain(`--fs-overlay-laser: ${tokens.color.overlay.laser}`);
  });
});

function kebab(name: string): string {
  return name.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`);
}
