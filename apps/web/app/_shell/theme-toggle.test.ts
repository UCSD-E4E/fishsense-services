// The header's theme switch: three choices, the current one checked.
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { ThemeToggle } from "./theme-toggle";

function checked(html: string): string[] {
  return [...html.matchAll(/aria-checked="true"[^>]*aria-label="([^"]+)"|aria-label="([^"]+)"[^>]*aria-checked="true"/g)].map(
    (m) => m[1] ?? m[2],
  );
}

describe("ThemeToggle", () => {
  it("offers the system's theme, light and dark, as one choice", () => {
    const html = renderToStaticMarkup(createElement(ThemeToggle, { initial: "system" }));

    expect(html).toContain('role="radiogroup"');
    expect(html).toContain('aria-label="Theme"');
    for (const label of ["Match the system", "Light", "Dark"]) expect(html).toContain(`aria-label="${label}"`);
    expect(checked(html)).toEqual(["Match the system"]);
  });

  it("starts at the user's pick", () => {
    expect(checked(renderToStaticMarkup(createElement(ThemeToggle, { initial: "dark" })))).toEqual(["Dark"]);
  });
});
