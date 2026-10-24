// A menu built on <details> (the tenant switcher) opens and closes only from
// its own summary. Elsewhere, a click left it open, so it felt stuck (the owner,
// 2026-10-23). It now closes on a click outside it, or on Escape.
import { describe, expect, it } from "vitest";
import { dismissOnOutside } from "./dismiss";

type Handler = (event: { target?: unknown; key?: string }) => void;

function page() {
  const handlers = new Map<string, Handler>();
  const doc = {
    addEventListener: (type: string, h: Handler) => void handlers.set(type, h),
    removeEventListener: (type: string, h: Handler) => {
      if (handlers.get(type) === h) handlers.delete(type);
    },
  };
  const inside = { name: "a link in the menu" };
  const menu = { open: true, contains: (node: unknown) => node === menu || node === inside };
  return { doc, menu, inside, handlers };
}

describe("dismissOnOutside", () => {
  it("closes the menu on a click anywhere else on the page", () => {
    const { doc, menu, handlers } = page();
    dismissOnOutside(menu, doc);

    handlers.get("pointerdown")!({ target: { name: "the page" } });

    expect(menu.open).toBe(false);
  });

  it("leaves it open for a click inside it", () => {
    const { doc, menu, inside, handlers } = page();
    dismissOnOutside(menu, doc);

    handlers.get("pointerdown")!({ target: inside });

    expect(menu.open).toBe(true);
  });

  it("closes it on Escape, and no other key", () => {
    const { doc, menu, handlers } = page();
    dismissOnOutside(menu, doc);

    handlers.get("keydown")!({ key: "Tab" });
    expect(menu.open).toBe(true);
    handlers.get("keydown")!({ key: "Escape" });
    expect(menu.open).toBe(false);
  });

  it("stops listening when the menu goes away", () => {
    const { doc, menu, handlers } = page();

    dismissOnOutside(menu, doc)();

    expect(handlers.size).toBe(0);
  });
});
