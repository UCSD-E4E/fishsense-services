// The one place a tenant slug and a uuid from a URL or the environment are
// checked before they go into an API path (request forgery). captures.ts,
// observations.ts and env.ts all use these, so tightening a rule here tightens
// it everywhere.
import { describe, expect, it } from "vitest";
import { isSlug, isUuid, safeSlug, safeUuid } from "./ids";

const UUID = "6f1c8a52-9a4e-4c43-9b8e-4f4e2d1a0b11";

describe("isSlug / safeSlug", () => {
  it("accepts one path segment of lowercase letters, digits and dashes", () => {
    expect(isSlug("conservation-angler")).toBe(true);
    expect(safeSlug("lab")).toBe("lab");
  });

  it.each(["", "../admin", "Lab", "-lab", "a/b", "lab?x=1"])(
    "refuses %j",
    (slug) => {
      expect(isSlug(slug)).toBe(false);
      expect(() => safeSlug(slug)).toThrow(/tenant/);
    },
  );
});

describe("isUuid / safeUuid", () => {
  it("accepts a uuid", () => {
    expect(isUuid(UUID)).toBe(true);
    expect(safeUuid(UUID, "capture")).toBe(UUID);
  });

  it.each(["", "not-a-uuid", "../x", `${UUID}/..`, "1 OR 1"])(
    "refuses %j, naming what it was",
    (id) => {
      expect(isUuid(id)).toBe(false);
      expect(() => safeUuid(id, "fish")).toThrow(/fish/);
    },
  );
});
