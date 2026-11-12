import pkg from "../../../package.json";
import { apiClient } from "@/lib/api/client";
import { parseBeacon } from "@/lib/page-timings";
import { getServiceToken } from "@/lib/service-token";

/** A beacon carries at most 50 timings: far less than this. */
const MAX_BODY = 16 * 1024;

/**
 * Where a page's browser sends what it measured (`app/_shell/page-timings.tsx`;
 * migration 0091). The timings are re-checked (`parseBeacon`) and passed to
 * the API as the web's service account, with the web's release, so a change
 * can be compared before and after. Public pages send them too: a signed-out
 * visitor's page is as slow as anyone's.
 *
 * The browser gets 204 whatever the API says: it sent a beacon and reads no
 * answer, and a timing is never worth an error. A failure is logged.
 */
export async function POST(request: Request): Promise<Response> {
  const text = await request.text();
  if (text.length > MAX_BODY) return new Response(null, { status: 413 });
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch {
    return new Response(null, { status: 400 });
  }
  const timings = parseBeacon(raw);
  if (!timings) return new Response(null, { status: 400 });

  const body = { release: pkg.version, timings };
  const send = async (token: string) =>
    apiClient(token).POST("/telemetry/page-timings", { body });
  try {
    let result = await send(await getServiceToken());
    if (result.response.status === 401) {
      // The cached token was revoked or expired early: mint once and ask again.
      result = await send(await getServiceToken(true));
    }
    if (!result.response.ok) {
      console.error("page timings: the API refused them", result.response.status);
    }
  } catch (error) {
    console.error("page timings: could not reach the API", error);
  }
  return new Response(null, { status: 204 });
}
