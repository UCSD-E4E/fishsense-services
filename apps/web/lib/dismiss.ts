/**
 * Close a <details> menu (the tenant switcher) on a click outside it or on
 * Escape. A native <details> closes only from its own summary, so a click
 * anywhere else left it open (the owner, 2026-10-23). Returns the cleanup.
 */

type Menu = { open: boolean; contains(node: unknown): boolean };
type Listener = (event: { target?: unknown; key?: string }) => void;
type Page = {
  addEventListener(type: string, listener: Listener): void;
  removeEventListener(type: string, listener: Listener): void;
};

export function dismissOnOutside(menu: Menu, page: Page): () => void {
  const onPointer: Listener = (event) => {
    if (menu.open && !menu.contains(event.target)) menu.open = false;
  };
  const onKey: Listener = (event) => {
    if (menu.open && event.key === "Escape") menu.open = false;
  };
  page.addEventListener("pointerdown", onPointer);
  page.addEventListener("keydown", onKey);
  return () => {
    page.removeEventListener("pointerdown", onPointer);
    page.removeEventListener("keydown", onKey);
  };
}
