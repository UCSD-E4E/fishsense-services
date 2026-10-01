// Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/lib/sections.ts.
import type { ProjectsByKind } from "./active-projects";
import type { StaticLink } from "./static-links";

// User-facing base for Label Studio project links. Derived from the LS
// instance URL (LABEL_STUDIO_URL, e.g. https://app.heartex.com) so the
// cards point at the same instance the API resolves names from; overridable
// via LABELER_BASE. Read at call time so it tracks the runtime env.
function labelerBase(): string {
  return (
    process.env.LABELER_BASE ||
    process.env.LABEL_STUDIO_URL ||
    "https://app.heartex.com"
  );
}

export type SectionLink = {
  title: string;
  description: string;
  href: string;
};

export type Section = {
  title: string;
  links: SectionLink[];
};

const LABELING_KINDS: { key: keyof ProjectsByKind; title: string }[] = [
  { key: "laser", title: "Laser Labeling" },
  { key: "headtail", title: "Head/Tail Labeling" },
  { key: "species", title: "Species Labeling" },
  { key: "slate", title: "Slate Labeling" },
];

export function buildSections(
  active: ProjectsByKind,
  staticLinks: { results: StaticLink[]; admin: StaticLink[] },
): Section[] {
  const sections: Section[] = [];
  const base = labelerBase();

  for (const { key, title } of LABELING_KINDS) {
    const projects = active[key];
    if (projects.length === 0) continue;
    sections.push({
      title,
      links: projects.map((p) => ({
        title: p.title,
        description: `${p.title} labeling project`,
        href: `${base}/projects/${p.id}`,
      })),
    });
  }

  if (staticLinks.results.length > 0) {
    sections.push({ title: "Results", links: staticLinks.results });
  }
  if (staticLinks.admin.length > 0) {
    sections.push({ title: "Administration", links: staticLinks.admin });
  }

  return sections;
}
