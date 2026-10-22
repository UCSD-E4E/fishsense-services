/**
 * The theme the user picked in the header (System / Light / Dark). It is kept
 * in a cookie the server reads, so the page arrives in the right theme, and
 * set as `data-theme` on <html>: tokens.css and Tailwind's `dark:` variant
 * (globals.css) follow it, and the system's setting when there is none.
 */

export type Theme = "system" | "light" | "dark";

export const THEME_COOKIE = "fs-theme";
export const THEMES: Theme[] = ["system", "light", "dark"];

const YEAR_SECONDS = 60 * 60 * 24 * 365;

/** The cookie's pick; anything else is the system's setting. */
export function parseTheme(raw: string | undefined): Theme {
  return raw === "light" || raw === "dark" || raw === "system" ? raw : "system";
}

/** `data-theme` on <html>: the pick, or none, so the system decides. */
export function themeAttribute(theme: Theme): "light" | "dark" | undefined {
  return theme === "system" ? undefined : theme;
}

export function themeCookie(theme: Theme): string {
  return `${THEME_COOKIE}=${theme}; Path=/; Max-Age=${YEAR_SECONDS}; SameSite=Lax`;
}

type Page = {
  documentElement: { setAttribute(name: string, value: string): void; removeAttribute(name: string): void };
  cookie: string;
};

/** Switch the open page to `theme` and remember it, without a reload. */
export function applyTheme(theme: Theme, page: Page): void {
  const attribute = themeAttribute(theme);
  if (attribute) page.documentElement.setAttribute("data-theme", attribute);
  else page.documentElement.removeAttribute("data-theme");
  page.cookie = themeCookie(theme);
}
