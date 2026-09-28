// Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/lib/portal-sections.ts.
/**
 * The pages the portal index links to.
 *
 * The index is a hub — it does no work of its own, it routes. Keeping the
 * destinations in one array rather than hand-writing cards means adding a
 * page is a single edit, and a page that exists as a route but is missing
 * here is one nobody can navigate to.
 *
 * Every entry must stay under `/portal/`: the index is authorization-gated,
 * and a link off it would take a labeler somewhere that gate does not cover.
 */
export type PortalSection = {
  title: string;
  /** Absolute path under `/portal/`. */
  href: string;
  /** What the page is *for* — a hub of bare nouns is not navigable. */
  description: string;
};

export const PORTAL_SECTIONS: PortalSection[] = [
  {
    title: "Laser triage",
    href: "/portal/triage",
    description:
      "Accept or skip the model's laser predictions, one frame at a time. Built for a phone: pinch to zoom, and the magnification carries between frames.",
  },
  {
    title: "Dive calibration links",
    href: "/portal/calibration",
    description:
      "Link a dive with no slate of its own to a sibling slate dive shot on the same rig, so it can be measured without a slate in frame.",
  },
];
