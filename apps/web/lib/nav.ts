/**
 * The portal's main navigation (docs/design/README.md, Navigation): Today
 * (`/lab`), Fish, Data, and Labeling for staff. Map and Settings join when their pages
 * exist, so the nav never links to a page that is not there.
 *
 * Fish and Data are a tenant's (`/lab/fish`, `/lab/data`): they link into the
 * tenant the page is in, else the one used last, else the old index pages,
 * which sign the user in and take their first tenant.
 */
import { paths, tenantOf } from "./tenant-paths";

export type NavItem = { label: string; href: string; active: boolean };

type Context = { staff: boolean; lastTenant: string | null };

function segments(pathname: string): string[] {
  return pathname.split("/").filter(Boolean);
}

export function navItems(pathname: string, { staff, lastTenant }: Context): NavItem[] {
  const here = tenantOf(pathname);
  const tenant = here ?? lastTenant;
  const section = here ? segments(pathname)[1] : undefined;
  const items: NavItem[] = [
    {
      label: "Today",
      href: tenant ? paths.tenant(tenant) : "/",
      active: pathname === "/" || (here !== null && section === undefined),
    },
    {
      label: "Fish",
      href: tenant ? paths.fish(tenant) : "/fish",
      active: section === "fish",
    },
    {
      label: "Data",
      href: tenant ? paths.data(tenant) : "/deployments",
      active: section === "data",
    },
  ];
  if (staff) {
    items.push({
      label: "Labeling",
      href: "/portal",
      active: pathname === "/portal" || pathname.startsWith("/portal/"),
    });
  }
  return items;
}
