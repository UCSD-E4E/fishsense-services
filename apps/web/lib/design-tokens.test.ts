import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { kebab, tokensToCss, type DesignTokens } from "./design-tokens";

const repo = path.resolve(__dirname, "../../..");
const tokens: DesignTokens = JSON.parse(
  readFileSync(path.join(repo, "docs/design/tokens.json"), "utf8"),
);
const read = (file: string) => readFileSync(path.join(__dirname, "..", file), "utf8");

describe("the web's theme is docs/design/tokens.json", () => {
  it("app/tokens.css is generated from it, unchanged", () => {
    // The image builds from apps/web alone, so the CSS is committed; this
    // keeps it the JSON's (regenerate with `npm run tokens:generate`).
    expect(read("app/tokens.css")).toBe(tokensToCss(tokens));
  });

  it("maps every generated variable onto Tailwind's theme", () => {
    // globals.css's @theme is written by hand: a token it leaves out is a
    // utility a later page cannot use, so it hardcodes the value instead.
    const generated = new Set(read("app/tokens.css").match(/--fs-[a-z0-9-]+(?=:)/g));
    const mapped = new Set(read("app/globals.css").match(/var\((--fs-[a-z0-9-]+)\)/g)?.map(
      (v) => v.slice(4, -1),
    ));

    expect([...generated].filter((v) => !mapped.has(v))).toEqual([]);
  });

  it("switches every colour with the system theme, unless the user picked one", () => {
    const css = tokensToCss(tokens);
    const [light, rest] = css.split("@media (prefers-color-scheme: dark)");
    const [system, pinned] = rest.split(':root[data-theme="dark"]');

    // The system's dark, for anyone who did not pick light (the header's
    // switch, lib/theme.ts); and dark for anyone who picked it, any system.
    expect(system).toContain(':root:not([data-theme="light"])');
    for (const [name, value] of Object.entries(tokens.color.dark)) {
      expect(light).toContain(`--fs-${kebab(name)}: ${tokens.color.light[name]};`);
      expect(system).toContain(`--fs-${kebab(name)}: ${value};`);
      expect(pinned).toContain(`--fs-${kebab(name)}: ${value};`);
    }
  });

  it("keys Tailwind's dark: variant to the same pick", () => {
    // Pages still carry `dark:` utilities; keyed to the system alone, a
    // picked theme would split a page in two.
    const globals = read("app/globals.css");

    expect(globals).toContain("@custom-variant dark");
    expect(globals).toContain('[data-theme="dark"]');
    expect(globals).toContain(':not([data-theme="light"])');
  });

  it("names variables in kebab case", () => {
    expect(kebab("tealText")).toBe("teal-text");
    expect(kebab("maskEdge")).toBe("mask-edge");
    expect(kebab("text2")).toBe("text2");
  });
});
