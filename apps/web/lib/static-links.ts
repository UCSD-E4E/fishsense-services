// Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/lib/static-links.ts.
export type StaticLink = {
  title: string;
  description: string;
  href: string;
};

// Overridable via env so a host change doesn't need a code deploy.
const ANALYTICS_BASE =
  process.env.ANALYTICS_BASE ??
  "https://analytics.fishsense.e4e.ucsd.edu/superset/dashboard";

// Temporal moved to the shared krg-prod cluster at the Incus migration; the
// old in-orchestrator `workflows.fishsense.e4e.ucsd.edu` UI is gone. The krg
// Temporal Web UI is namespace-scoped — the default deep-links to the
// fishsense namespace. Override with WORKFLOWS_URL if the host/path changes.
const WORKFLOWS_URL =
  process.env.WORKFLOWS_URL ?? "https://workflows.krg.ucsd.edu/namespaces/fishsense";

// Superset dashboard slugs (from the committed asset bundle under
// deploy/incus/superset_volumes/docker/assets/dashboards/). These replaced the
// retired hosted-Superset `reef-smile-*` slugs at the Incus migration.
export const RESULTS_LINKS: StaticLink[] = [
  {
    title: "Lengths",
    description: "Fish length measurements dashboard from Apache Superset",
    href: `${ANALYTICS_BASE}/fishsense-fish-measurements`,
  },
  {
    title: "Metrics",
    description: "Pipeline status dashboard from Apache Superset",
    href: `${ANALYTICS_BASE}/fishsense-pipeline-status`,
  },
];

export const ADMIN_LINKS: StaticLink[] = [
  {
    title: "Workflows",
    description: "Temporal Workflows",
    href: WORKFLOWS_URL,
  },
];
