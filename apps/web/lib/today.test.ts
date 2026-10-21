import { describe, expect, it } from "vitest";
import { recentDeployments } from "./today";
import type { Deployment } from "./mission-facts";

const d = (number: number, started_on: string | null) =>
  ({ number, started_on, name: `d${number}`, missions: 1 }) as unknown as Deployment;

describe("Today's recent deployments", () => {
  it("are the newest trips first, undated last, at most n", () => {
    const list = [d(1, "2023-08-03"), d(2, null), d(3, "2025-01-17"), d(4, "2024-12-10")];

    expect(recentDeployments(list, 3).map((x) => x.number)).toEqual([3, 4, 1]);
    expect(recentDeployments([d(2, null), d(5, null)], 3).map((x) => x.number)).toEqual([5, 2]);
  });
});
