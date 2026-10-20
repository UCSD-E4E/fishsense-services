/**
 * The Deep water design tokens (docs/design/README.md, agreed 2026-10-20) as
 * CSS variables: `--fs-<name>` per colour, light by default, dark when the
 * system is dark, and either when a page pins `data-theme` on `<html>`.
 * globals.css maps them onto Tailwind's theme (`bg-surface`, `text-fg-2`, …).
 *
 * The JSON in docs/design is the one source; the image builds from apps/web
 * alone, so `app/tokens.css` is committed and design-tokens.test.ts holds it
 * to the JSON. Regenerate with `npm run tokens:generate`.
 */

type Palette = Record<string, string>;

export type DesignTokens = {
  color: { dark: Palette; light: Palette; overlay: Palette };
  radius: Record<string, number>;
};

function kebab(name: string): string {
  return name.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`);
}

function variables(palette: Palette, prefix = ""): string {
  return Object.entries(palette)
    .map(([name, value]) => `  --fs-${prefix}${kebab(name)}: ${value};`)
    .join("\n");
}

export function tokensToCss(tokens: DesignTokens): string {
  const { light, dark, overlay } = tokens.color;
  const radii = Object.entries(tokens.radius)
    .map(([name, px]) => `  --fs-radius-${kebab(name)}: ${px}px;`)
    .join("\n");
  return [
    "/* Generated from docs/design/tokens.json by `npm run tokens:generate`. Do not edit. */",
    "",
    ":root {",
    variables(light),
    variables(overlay, "overlay-"),
    radii,
    "}",
    "",
    "@media (prefers-color-scheme: dark) {",
    '  :root:not([data-theme="light"]) {',
    variables(dark).replace(/^/gm, "  "),
    "  }",
    "}",
    "",
    ':root[data-theme="dark"] {',
    variables(dark),
    "}",
    "",
  ].join("\n");
}
