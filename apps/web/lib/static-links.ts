// Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/lib/static-links.ts.
export type StaticLink = {
  title: string;
  description: string;
  href: string;
};

// Temporal moved to the shared krg-prod cluster at the Incus migration; the
// old in-orchestrator `workflows.fishsense.e4e.ucsd.edu` UI is gone. The krg
// Temporal Web UI is namespace-scoped — the default deep-links to the
// fishsense namespace. Override with WORKFLOWS_URL if the host/path changes.
const WORKFLOWS_URL =
  process.env.WORKFLOWS_URL ?? "https://workflows.krg.ucsd.edu/namespaces/fishsense";

// v1's Superset "Lengths" and "Metrics" links are gone with Superset
// (retired 2026-10-23): the portal's own pages show both.
export const ADMIN_LINKS: StaticLink[] = [
  {
    title: "Workflows",
    description: "Temporal Workflows",
    href: WORKFLOWS_URL,
  },
];
