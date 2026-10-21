import { redirect } from "next/navigation";
import { auth } from "@/auth";
import { capturesGate } from "@/lib/authz";

/** Signed in with a usable token; see `capturesGate`. */
export async function requireSignedIn(path: string) {
  const session = await auth();
  const gate = capturesGate(session, path);
  if (!gate.ok) redirect(gate.redirect);
  return session!;
}
