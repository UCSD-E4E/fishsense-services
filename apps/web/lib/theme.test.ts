// The theme the user picked (the header's System / Light / Dark switch).
//
// Pinned here:
//  * anything but "light" or "dark" in the cookie is the system's setting;
//  * a pick is an attribute on <html> (`data-theme`), none for the system,
//    so the server renders the same page the switch will show (no flash);
//  * the switch writes the cookie and the attribute at once, no reload.
import { describe, expect, it } from "vitest";
import { applyTheme, parseTheme, THEME_COOKIE, themeAttribute, themeCookie } from "./theme";

describe("parseTheme", () => {
  it.each([
    ["light", "light"],
    ["dark", "dark"],
    ["system", "system"],
    [undefined, "system"],
    ["", "system"],
    ["sepia", "system"],
  ])("reads %j as %s", (raw, theme) => {
    expect(parseTheme(raw)).toBe(theme);
  });
});

describe("themeAttribute", () => {
  it("is the pick, or nothing for the system", () => {
    expect(themeAttribute("light")).toBe("light");
    expect(themeAttribute("dark")).toBe("dark");
    expect(themeAttribute("system")).toBeUndefined();
  });
});

describe("themeCookie", () => {
  it("keeps the pick for a year, site-wide", () => {
    expect(themeCookie("dark")).toBe(`${THEME_COOKIE}=dark; Path=/; Max-Age=31536000; SameSite=Lax`);
  });
});

describe("applyTheme", () => {
  function page() {
    const attributes = new Map<string, string>();
    const root = {
      setAttribute: (k: string, v: string) => void attributes.set(k, v),
      removeAttribute: (k: string) => void attributes.delete(k),
    };
    const doc = { documentElement: root, cookie: "" };
    return { doc, attributes };
  }

  it("pins a pick on the page and remembers it", () => {
    const { doc, attributes } = page();

    applyTheme("dark", doc);

    expect(attributes.get("data-theme")).toBe("dark");
    expect(doc.cookie).toBe(themeCookie("dark"));
  });

  it("hands the page back to the system", () => {
    const { doc, attributes } = page();
    applyTheme("light", doc);

    applyTheme("system", doc);

    expect(attributes.has("data-theme")).toBe(false);
    expect(doc.cookie).toBe(themeCookie("system"));
  });
});
