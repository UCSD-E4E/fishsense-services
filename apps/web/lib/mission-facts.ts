/**
 * Missions and deployments, as the web shows and edits them (PLAN.md §4.9,
 * Phase 2b): what a mission is for, its camera port and what said so, its
 * place, and the field trip (deployment) it belongs to.
 *
 * Every call is made as the signed-in user, in the tenant named by the URL;
 * the API decides what they may see and checks their admin role on writes.
 * Values from the URL or a form are validated before they reach a request.
 *
 * The port is the owner's rule where one applies (2026-10-16): a D-labelled
 * device, or anything ingested before ports were recorded, is the corrective
 * optic, and the API refuses to contradict it. Nothing else is known: a
 * missing D does not mean flat.
 */
import { apiClient, failure, type Schemas } from "./api/client";
import { safeSlug } from "./ids";

export type MissionRow = Schemas["Mission"];
export type MissionDetail = Schemas["MissionDetail"];
export type Deployment = Schemas["Deployment"];
export type DeploymentDetail = Schemas["DeploymentDetail"];
export type NewDeployment = Schemas["NewDeployment"];
export type Port = MissionRow["port"];
export type PortSource = NonNullable<MissionRow["port_source"]>;
export type Purpose = NonNullable<MissionRow["purpose"]>;

export const PORTS: readonly Port[] = ["corrective_optic", "flat", "dome", "unknown"];
export const PURPOSES: readonly Purpose[] = [
  "survey",
  "lens_calibration",
  "laser_calibration",
  "known_length",
  "other",
];

function safeNumber(value: number, what: string): number {
  const n = Number(value);
  if (!Number.isInteger(n) || n < 0) throw new Error(`Invalid ${what}: ${value}`);
  return n;
}

/** A mission or deployment number from a form or the URL. */
export function parseMissionNumber(value: string): number {
  if (!/^\d+$/.test(value)) throw new Error(`Invalid number: ${JSON.stringify(value)}`);
  return Number(value);
}

export function parsePort(value: string): Port {
  const port = PORTS.find((p) => p === value);
  if (!port) throw new Error(`Invalid port: ${JSON.stringify(value)}`);
  return port;
}

/** An empty value is "not stated". */
export function parsePurpose(value: string): Purpose | null {
  if (value === "") return null;
  const purpose = PURPOSES.find((p) => p === value);
  if (!purpose) throw new Error(`Invalid purpose: ${JSON.stringify(value)}`);
  return purpose;
}

const PORT_NAMES: Record<Port, string> = {
  corrective_optic: "Corrective optic",
  flat: "Flat port",
  dome: "Dome port",
  unknown: "Unknown",
};

const SOURCE_NAMES: Record<PortSource, string> = {
  device_suffix: "D-labelled device",
  backfill: "ingested before the port was recorded",
  operator: "stated by an operator",
  calibration: "inferred from a lens calibration",
};

export function portLabel(port: Port, source: PortSource | null | undefined): string {
  return source ? `${PORT_NAMES[port]} (${SOURCE_NAMES[source]})` : PORT_NAMES[port];
}

export type Source = MissionRow["source"];

/** The missions page's `?source=` (phase 7b: "Phone" is where the capture
 *  list's old links land); anything else shows every mission. */
export function sourceFilter(value: string | undefined): Source | undefined {
  return value === "lite" || value === "mobile" ? value : undefined;
}

/** A phone's outing rather than a folder off a Lite camera (0057). */
export function isPhoneMission(m: Pick<MissionRow, "source">): boolean {
  return m.source === "mobile";
}

/** A Lite mission nothing has said the port of. A phone has no camera port. */
export function portUnknown(m: Pick<MissionRow, "source" | "port">): boolean {
  return !isPhoneMission(m) && m.port === "unknown";
}

/** What took the mission: the phone, or the Lite camera by its name. */
export function cameraLabel(m: Pick<MissionRow, "source" | "device_name">): string {
  return isPhoneMission(m) ? "Phone" : (m.device_name ?? "—");
}

export function purposeLabel(purpose: Purpose | null | undefined): string {
  if (!purpose) return "Not stated";
  const words = purpose.replace(/_/g, " ");
  return words[0].toUpperCase() + words.slice(1);
}

function client(accessToken: string) {
  return apiClient(accessToken);
}

export async function getTenantMissions(
  accessToken: string,
  slug: string,
): Promise<MissionRow[]> {
  const { data, error, response } = await client(accessToken).GET(
    "/tenants/{slug}/missions",
    { params: { path: { slug: safeSlug(slug) } } },
  );
  if (data === undefined) throw failure("fishsense-api missions failed", response, error);
  return data;
}

export async function getMission(
  accessToken: string,
  slug: string,
  missionNumber: number,
): Promise<MissionDetail | null> {
  const number = safeNumber(missionNumber, "mission");
  const { data, error, response } = await client(accessToken).GET(
    "/tenants/{slug}/missions/{number}",
    { params: { path: { slug: safeSlug(slug), number } } },
  );
  if (data !== undefined) return data;
  if (response.status === 404) return null;
  throw failure("fishsense-api mission failed", response, error);
}

export async function getDeployments(
  accessToken: string,
  slug: string,
): Promise<Deployment[]> {
  const { data, error, response } = await client(accessToken).GET(
    "/tenants/{slug}/deployments",
    { params: { path: { slug: safeSlug(slug) } } },
  );
  if (data === undefined) throw failure("fishsense-api deployments failed", response, error);
  return data;
}

export async function getDeployment(
  accessToken: string,
  slug: string,
  deploymentNumber: number,
): Promise<DeploymentDetail | null> {
  const number = safeNumber(deploymentNumber, "deployment");
  const { data, error, response } = await client(accessToken).GET(
    "/tenants/{slug}/deployments/{number}",
    { params: { path: { slug: safeSlug(slug), number } } },
  );
  if (data !== undefined) return data;
  if (response.status === 404) return null;
  throw failure("fishsense-api deployment failed", response, error);
}

export async function setMissionPort(
  accessToken: string,
  slug: string,
  missionNumber: number,
  port: Port,
  note: string | null,
): Promise<MissionDetail> {
  const number = safeNumber(missionNumber, "mission");
  const { data, error, response } = await client(accessToken).PUT(
    "/tenants/{slug}/missions/{number}/port",
    { params: { path: { slug: safeSlug(slug), number } }, body: { port, note } },
  );
  if (data === undefined) throw failure("Could not set the port", response, error);
  return data;
}

export async function setMissionPurpose(
  accessToken: string,
  slug: string,
  missionNumber: number,
  purpose: Purpose | null,
): Promise<MissionDetail> {
  const number = safeNumber(missionNumber, "mission");
  const { data, error, response } = await client(accessToken).PUT(
    "/tenants/{slug}/missions/{number}/purpose",
    { params: { path: { slug: safeSlug(slug), number } }, body: { purpose } },
  );
  if (data === undefined) throw failure("Could not set the purpose", response, error);
  return data;
}

/** Into deployment `deploymentNumber`, or out of any with null. */
export async function setMissionDeployment(
  accessToken: string,
  slug: string,
  missionNumber: number,
  deploymentNumber: number | null,
): Promise<void> {
  const number = safeNumber(missionNumber, "mission");
  const path = { slug: safeSlug(slug), number };
  if (deploymentNumber === null) {
    const { error, response } = await client(accessToken).DELETE(
      "/tenants/{slug}/missions/{number}/deployment",
      { params: { path } },
    );
    if (!response.ok) throw failure("Could not clear the deployment", response, error);
    return;
  }
  const deployment_number = safeNumber(deploymentNumber, "deployment");
  const { data, error, response } = await client(accessToken).PUT(
    "/tenants/{slug}/missions/{number}/deployment/{deployment_number}",
    { params: { path: { ...path, deployment_number } } },
  );
  if (data === undefined) throw failure("Could not set the deployment", response, error);
}

export async function createDeployment(
  accessToken: string,
  slug: string,
  deployment: NewDeployment,
): Promise<Deployment> {
  const { data, error, response } = await client(accessToken).POST(
    "/tenants/{slug}/deployments",
    { params: { path: { slug: safeSlug(slug) } }, body: deployment },
  );
  if (data === undefined) throw failure("Could not create the deployment", response, error);
  return data;
}
