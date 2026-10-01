// Ported from fishsense-lite@77e8f8e5
// apps/fishsense-lite-web/app/portal/calibration/actions.test.ts.
//
// v2 changes, each pinned below: "authorized" is the tenant admin role the v2
// API reports (not an Authentik group), the write goes to the API as the
// signed-in user -- who the API checks again -- and a stale access token is
// refreshed before the write rather than sent to be refused.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Hoisted so the module mocks below can reference them.
const { auth, unstable_update, setCalibrationSource, clearCalibrationSource, revalidatePath } =
  vi.hoisted(() => ({
    auth: vi.fn(),
    unstable_update: vi.fn(),
    setCalibrationSource: vi.fn(),
    clearCalibrationSource: vi.fn(),
    revalidatePath: vi.fn(),
  }));

vi.mock("@/auth", () => ({ auth, unstable_update }));
vi.mock("@/lib/dives", () => ({ setCalibrationSource, clearCalibrationSource }));
vi.mock("next/cache", () => ({ revalidatePath }));

import {
  clearCalibrationSourceAction,
  setCalibrationSourceAction,
} from "./actions";

const FAR_FUTURE = Math.floor(Date.now() / 1000) + 3600;
const SIGNED_IN = {
  user: { name: "Alice", email: "a@e.com", groups: [] },
  accessToken: "alice-token",
  accessTokenExpiresAt: FAR_FUTURE,
  role: "admin",
  isAdmin: true,
};
/** Signed in, and a member of the tenant, but not its admin. */
const SIGNED_IN_UNAUTHORIZED = {
  ...SIGNED_IN,
  user: { name: "Mallory", email: "m@e.com", groups: ["FishSense-Prod-Admins"] },
  accessToken: "mallory-token",
  role: "member",
  isAdmin: false,
};

beforeEach(() => {
  auth.mockResolvedValue(SIGNED_IN);
  unstable_update.mockResolvedValue(SIGNED_IN);
  setCalibrationSource.mockResolvedValue(undefined);
  clearCalibrationSource.mockResolvedValue(undefined);
});

afterEach(() => {
  vi.clearAllMocks();
});

// ── the auth gate ─────────────────────────────────────────────────────
//
// Server actions are ordinary public HTTP endpoints. `/portal/page.tsx`
// redirecting signed-out users does NOT protect them — anyone can POST an
// action directly. The v2 API checks the role again on the write, but the
// action must not lean on that: it is where the user's token is spent.
//
// Authentication alone is not enough: the Authentik realm is the whole SSO
// population, so a signed-in stranger must still be refused.

describe("auth gate", () => {
  it("refuses to set a calibration source for a signed-in member who is not an admin", async () => {
    auth.mockResolvedValue(SIGNED_IN_UNAUTHORIZED);

    const result = await setCalibrationSourceAction(9, 5);

    expect(result).toEqual({ ok: false, error: "Not authorized" });
    expect(setCalibrationSource).not.toHaveBeenCalled();
  });

  it("refuses to clear a calibration source for a signed-in member who is not an admin", async () => {
    auth.mockResolvedValue(SIGNED_IN_UNAUTHORIZED);

    const result = await clearCalibrationSourceAction(9);

    expect(result).toEqual({ ok: false, error: "Not authorized" });
    expect(clearCalibrationSource).not.toHaveBeenCalled();
  });

  it("refuses every write for a user with no membership in the tenant (fails closed)", async () => {
    auth.mockResolvedValue({ ...SIGNED_IN, role: null, isAdmin: false });

    expect(await setCalibrationSourceAction(9, 5)).toEqual({
      ok: false,
      error: "Not authorized",
    });
    expect(await clearCalibrationSourceAction(9)).toEqual({
      ok: false,
      error: "Not authorized",
    });
    expect(setCalibrationSource).not.toHaveBeenCalled();
    expect(clearCalibrationSource).not.toHaveBeenCalled();
  });

  it("refuses to set a calibration source when there is no session", async () => {
    auth.mockResolvedValue(null);

    const result = await setCalibrationSourceAction(9, 5);

    expect(result).toEqual({ ok: false, error: "Not authenticated" });
    expect(setCalibrationSource).not.toHaveBeenCalled();
  });

  it("refuses to clear a calibration source when there is no session", async () => {
    auth.mockResolvedValue(null);

    const result = await clearCalibrationSourceAction(9);

    expect(result).toEqual({ ok: false, error: "Not authenticated" });
    expect(clearCalibrationSource).not.toHaveBeenCalled();
  });

  it("refuses when a session exists but carries no user", async () => {
    auth.mockResolvedValue({ expires: "later" });

    expect(await setCalibrationSourceAction(9, 5)).toEqual({
      ok: false,
      error: "Not authenticated",
    });
    expect(setCalibrationSource).not.toHaveBeenCalled();
  });

  it("checks the session BEFORE touching the API, not after", async () => {
    // Ordering matters: a check that ran after the write would still return
    // an error while having already mutated prod.
    auth.mockResolvedValue(null);

    await setCalibrationSourceAction(9, 5);
    await clearCalibrationSourceAction(9);

    expect(auth).toHaveBeenCalledTimes(2);
    expect(setCalibrationSource).not.toHaveBeenCalled();
    expect(clearCalibrationSource).not.toHaveBeenCalled();
  });
});

// ── the user's token ──────────────────────────────────────────────────

describe("the token the write carries", () => {
  it("is the signed-in user's own", async () => {
    await setCalibrationSourceAction(9, 5);
    await clearCalibrationSourceAction(9);

    expect(setCalibrationSource).toHaveBeenCalledWith("alice-token", 9, 5);
    expect(clearCalibrationSource).toHaveBeenCalledWith("alice-token", 9);
  });

  it("is refreshed first when it has expired", async () => {
    auth.mockResolvedValue({ ...SIGNED_IN, accessToken: "old", accessTokenExpiresAt: 1 });
    unstable_update.mockResolvedValue({ ...SIGNED_IN, accessToken: "refreshed" });

    expect(await setCalibrationSourceAction(9, 5)).toEqual({ ok: true });

    expect(unstable_update).toHaveBeenCalledOnce();
    expect(setCalibrationSource).toHaveBeenCalledWith("refreshed", 9, 5);
  });

  it("refuses, without writing, when the refresh fails", async () => {
    auth.mockResolvedValue({ ...SIGNED_IN, accessTokenExpiresAt: 1 });
    unstable_update.mockResolvedValue({
      ...SIGNED_IN,
      accessTokenExpiresAt: 1,
      error: "RefreshAccessTokenError",
      isAdmin: false,
    });

    const result = await setCalibrationSourceAction(9, 5);

    expect(result).toEqual({
      ok: false,
      error: "Your sign-in has expired. Reload the page to sign in again.",
    });
    expect(setCalibrationSource).not.toHaveBeenCalled();
  });

  it("re-checks the role the refresh brought back", async () => {
    // The refresh re-reads the membership; a role revoked meanwhile stops the
    // write here.
    auth.mockResolvedValue({ ...SIGNED_IN, accessTokenExpiresAt: 1 });
    unstable_update.mockResolvedValue({ ...SIGNED_IN_UNAUTHORIZED });

    expect(await setCalibrationSourceAction(9, 5)).toEqual({
      ok: false,
      error: "Not authorized",
    });
    expect(setCalibrationSource).not.toHaveBeenCalled();
  });
});

// ── the happy path + guards ───────────────────────────────────────────

describe("setCalibrationSourceAction", () => {
  it("links the dive and revalidates the portal", async () => {
    const result = await setCalibrationSourceAction(9, 5);

    expect(result).toEqual({ ok: true });
    expect(setCalibrationSource).toHaveBeenCalledWith("alice-token", 9, 5);
    expect(revalidatePath).toHaveBeenCalledWith("/portal");
  });

  it("rejects a dive borrowing its own calibration", async () => {
    // Self-reference would make laser-extrinsics resolution loop or 404, and
    // the dive would silently never become measurable.
    const result = await setCalibrationSourceAction(9, 9);

    expect(result).toEqual({
      ok: false,
      error: "A dive cannot be its own calibration source",
    });
    expect(setCalibrationSource).not.toHaveBeenCalled();
  });

  it("returns the failure instead of throwing when the API rejects it", async () => {
    setCalibrationSource.mockRejectedValue(new Error("500 Internal Server Error"));

    const result = await setCalibrationSourceAction(9, 5);

    expect(result).toEqual({ ok: false, error: "500 Internal Server Error" });
    expect(revalidatePath).not.toHaveBeenCalled();
  });

  it("does not revalidate when the write failed", async () => {
    setCalibrationSource.mockRejectedValue(new Error("nope"));

    await setCalibrationSourceAction(9, 5);

    expect(revalidatePath).not.toHaveBeenCalled();
  });
});

describe("clearCalibrationSourceAction", () => {
  it("clears the link and revalidates the portal", async () => {
    const result = await clearCalibrationSourceAction(9);

    expect(result).toEqual({ ok: true });
    expect(clearCalibrationSource).toHaveBeenCalledWith("alice-token", 9);
    expect(revalidatePath).toHaveBeenCalledWith("/portal");
  });

  it("returns the failure instead of throwing when the API rejects it", async () => {
    clearCalibrationSource.mockRejectedValue(new Error("503"));

    expect(await clearCalibrationSourceAction(9)).toEqual({
      ok: false,
      error: "503",
    });
  });

  it("reports a non-Error rejection without crashing", async () => {
    clearCalibrationSource.mockRejectedValue("a bare string");

    expect(await clearCalibrationSourceAction(9)).toEqual({
      ok: false,
      error: "Failed",
    });
  });
});
