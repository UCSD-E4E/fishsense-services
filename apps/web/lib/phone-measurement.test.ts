import { describe, expect, it } from "vitest";
import { phoneCardLabel, phoneSpeciesLabel, phoneStatusLabel } from "./phone-measurement";

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

describe("phoneSpeciesLabel", () => {
  it("names the species BioCLIP chose, with how sure it was", () => {
    expect(
      phoneSpeciesLabel({ scientific_name: "Sebastes dallii", common_name: "Calico Rockfish", probability: 0.814 }),
    ).toEqual({ name: "Calico Rockfish", detail: "Sebastes dallii · 81%" });
  });

  it("falls back to the scientific name", () => {
    expect(phoneSpeciesLabel({ scientific_name: "Sebastes dallii", common_name: null, probability: null })).toEqual({
      name: "Sebastes dallii",
      detail: null,
    });
  });

  it("is nothing when no species was predicted", () => {
    expect(phoneSpeciesLabel(null)).toBeNull();
    expect(phoneSpeciesLabel(undefined)).toBeNull();
  });
});
