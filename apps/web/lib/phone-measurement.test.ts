import { describe, expect, it } from "vitest";
import { phoneCardLabel, phoneStatusLabel } from "./phone-measurement";

describe("phoneCardLabel", () => {
  it("shows a measured length, marked unvalidated", () => {
    expect(phoneCardLabel({ status: "measured", length_m: 0.2843 })).toEqual({
      text: "284 mm",
      unvalidated: true,
    });
  });

  it("says why there is no length", () => {
    expect(phoneCardLabel({ status: "no_fish", length_m: null })).toEqual({
      text: "No fish found",
      unvalidated: false,
    });
  });

  it("is nothing when the server never measured the capture", () => {
    expect(phoneCardLabel(null)).toBeNull();
    expect(phoneCardLabel(undefined)).toBeNull();
  });
});

describe("phoneStatusLabel", () => {
  it("names every status the server records", () => {
    for (const status of ["measured", "no_fish", "headtail_failed", "depth_failed", "image_unavailable", "decode_failed"]) {
      expect(phoneStatusLabel(status)).not.toBe(status);
    }
  });

  it("passes an unknown status through", () => {
    expect(phoneStatusLabel("something_new")).toBe("something_new");
  });
});
