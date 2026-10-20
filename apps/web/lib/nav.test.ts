import { describe, expect, it } from "vitest";
import { navItems } from "./nav";

describe("the main navigation (docs/design/README.md, Navigation)", () => {
  it("is Today, Fish and Data for a member", () => {
    expect(navItems("/", { staff: false }).map((i) => i.label)).toEqual([
      "Today",
      "Fish",
      "Data",
    ]);
  });

  it("adds Labelling for staff", () => {
    expect(navItems("/", { staff: true }).map((i) => i.label)).toEqual([
      "Today",
      "Fish",
      "Data",
      "Labelling",
    ]);
  });

  it("marks the section a page is in, and only that one", () => {
    const active = (pathname: string) =>
      navItems(pathname, { staff: true })
        .filter((i) => i.active)
        .map((i) => i.label);

    expect(active("/")).toEqual(["Today"]);
    expect(active("/fish")).toEqual(["Fish"]);
    expect(active("/fish/lab/c/f")).toEqual(["Fish"]);
    // Data holds deployments and missions until they share one route.
    expect(active("/deployments/lab/4")).toEqual(["Data"]);
    expect(active("/missions")).toEqual(["Data"]);
    expect(active("/portal/triage")).toEqual(["Labelling"]);
    // A prefix that is not a path segment is not the section.
    expect(active("/fishing")).toEqual([]);
  });
});
