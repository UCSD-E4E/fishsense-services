/**
 * What "/" does with the visitor (app/page.tsx): signed out, the public page;
 * signed in, their tenant's Today -- with a token the API will take. An
 * expired one is renewed first, through the same gate every signed-in page
 * uses (`capturesGate`), and the visitor comes back here.
 *
 * Before (2026-10-22), home sent the dead token: the API said 401, home fell
 * back to the public page under a header showing the user signed in, and its
 * Sign in started a fresh Authentik login.
 */
import type { Session } from "next-auth";
import { capturesGate } from "./authz";

export type HomeStep =
  | { kind: "public" }
  | { kind: "redirect"; to: string }
  | { kind: "tenant"; accessToken: string };

export function homeStep(
  session: Session | null,
  nowSeconds: number = Math.floor(Date.now() / 1000),
): HomeStep {
  if (!session?.user) return { kind: "public" };
  const gate = capturesGate(session, "/", nowSeconds);
  if (!gate.ok) return { kind: "redirect", to: gate.redirect };
  return { kind: "tenant", accessToken: session.accessToken ?? "" };
}
