/**
 * The portal's main navigation (docs/design/README.md, Navigation): Today,
 * Fish, Data, and Labelling for staff. Map and Settings join when their pages
 * exist, so the nav never links to a page that is not there.
 *
 * Data holds deployments and missions; they keep their own routes until the
 * tenant moves into the path (the design's next step).
 */

export type NavItem = { label: string; href: string; active: boolean };

type Section = { label: string; href: string; owns: string[]; staff?: boolean };

const SECTIONS: Section[] = [
  { label: "Today", href: "/", owns: [] },
  { label: "Fish", href: "/fish", owns: ["/fish"] },
  { label: "Data", href: "/deployments", owns: ["/deployments", "/missions"] },
  { label: "Labelling", href: "/portal", owns: ["/portal"], staff: true },
];

function within(pathname: string, prefix: string): boolean {
  return pathname === prefix || pathname.startsWith(`${prefix}/`);
}

export function navItems(pathname: string, { staff }: { staff: boolean }): NavItem[] {
  return SECTIONS.filter((s) => !s.staff || staff).map((s) => ({
    label: s.label,
    href: s.href,
    active: s.owns.length === 0 ? pathname === "/" : s.owns.some((p) => within(pathname, p)),
  }));
}
