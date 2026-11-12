import { describe, expect, it } from "vitest";
import { navItems } from "./nav";

const labels = (items: ReturnType<typeof navItems>) => items.map((i) => i.label);

describe("the main navigation (docs/design/README.md, Navigation)", () => {
  it("is Today, Fish and Data for a member", () => {
    expect(labels(navItems("/", { staff: false, lastTenant: null }))).toEqual([
      "Today",
      "Fish",
      "Data",
    ]);
  });

  it("adds Labeling for staff", () => {
    expect(labels(navItems("/", { staff: true, lastTenant: null }))).toEqual([
      "Today",
      "Fish",
      "Data",
      "Labeling",
    ]);
  });

  it("links into the tenant the page is in", () => {
    const items = navItems("/lab/data/missions/383", { staff: false, lastTenant: "angler" });

    expect(items.find((i) => i.label === "Today")?.href).toBe("/lab");
    expect(items.find((i) => i.label === "Fish")?.href).toBe("/lab/fish");
    expect(items.find((i) => i.label === "Data")?.href).toBe("/lab/data");
  });

  it("links into the last tenant from a page that is nobody's", () => {
    const items = navItems("/portal", { staff: true, lastTenant: "angler" });

    expect(items.find((i) => i.label === "Fish")?.href).toBe("/angler/fish");
  });

  it("asks which tenant when there is none to go to", () => {
    // The old index pages sign the user in and take their first tenant.
    const items = navItems("/", { staff: false, lastTenant: null });

    expect(items.find((i) => i.label === "Fish")?.href).toBe("/fish");
    expect(items.find((i) => i.label === "Data")?.href).toBe("/deployments");
  });

  it("marks the section a page is in, and only that one", () => {
    const active = (pathname: string) =>
      navItems(pathname, { staff: true, lastTenant: null })
        .filter((i) => i.active)
        .map((i) => i.label);

    expect(active("/")).toEqual(["Today"]);
    expect(active("/lab")).toEqual(["Today"]);
    expect(active("/lab/fish")).toEqual(["Fish"]);
    expect(active("/lab/fish/c/f")).toEqual(["Fish"]);
    expect(active("/lab/data")).toEqual(["Data"]);
    expect(active("/lab/data/missions/383")).toEqual(["Data"]);
    expect(active("/lab/data/captures/u")).toEqual(["Data"]);
    expect(active("/portal/triage")).toEqual(["Labeling"]);
    // A prefix that is not a path segment is not the section.
    expect(active("/lab/fishing")).toEqual([]);
  });

  it("adds Dataset for a system admin, and for nobody else", () => {
    // The whole dataset, every tenant's (the owner, 2026-11-11).
    const admin = navItems("/", { staff: true, lastTenant: null, systemAdmin: true });

    expect(labels(admin)).toEqual(["Today", "Fish", "Data", "Labeling", "Dataset"]);
    expect(admin.find((i) => i.label === "Dataset")?.href).toBe("/admin/dataset");
    expect(labels(navItems("/", { staff: true, lastTenant: null }))).not.toContain("Dataset");
  });

  it("marks Dataset on its page", () => {
    const active = navItems("/admin/dataset", { staff: false, lastTenant: "lab", systemAdmin: true })
      .filter((i) => i.active)
      .map((i) => i.label);

    expect(active).toEqual(["Dataset"]);
  });
});
