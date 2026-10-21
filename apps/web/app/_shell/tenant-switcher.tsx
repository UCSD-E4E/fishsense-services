"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { switchHref, tenantOf } from "@/lib/tenant-paths";

export type SwitcherTenant = { slug: string; name: string; role: string | null };

/**
 * The tenant beside the mark (docs/design/README.md, Tenants): its name, and
 * for a member of several, the others with the user's role in each. Switching
 * keeps the section (`switchHref`), not the record.
 */
export function TenantSwitcher({
  tenants,
  lastTenant,
}: {
  tenants: SwitcherTenant[];
  lastTenant: string | null;
}) {
  const pathname = usePathname() ?? "/";
  const slug = tenantOf(pathname) ?? lastTenant;
  const current = tenants.find((t) => t.slug === slug);
  if (!current) return null;
  const badge = (
    <span
      aria-hidden
      className="inline-flex size-6 items-center justify-center rounded-md bg-teal-tint text-xs font-bold text-teal-fg"
    >
      {current.name.slice(0, 1).toUpperCase()}
    </span>
  );
  if (tenants.length === 1) {
    return (
      <span className="inline-flex h-10 items-center gap-2 px-1 font-semibold">
        {badge}
        {current.name}
      </span>
    );
  }
  return (
    <details className="group relative">
      <summary className="inline-flex h-10 cursor-pointer list-none items-center gap-2 rounded-control border border-line px-2.5 font-semibold hover:bg-raised group-open:border-teal">
        {badge}
        {current.name}
        <svg viewBox="0 0 24 24" aria-hidden className="size-3.5 stroke-fg-2" fill="none" strokeWidth={2.25} strokeLinecap="round">
          <path d="M6 9l6 6 6-6" />
        </svg>
        <span className="sr-only">Switch tenant</span>
      </summary>
      <ul className="absolute left-0 top-12 z-20 w-72 rounded-sheet border border-line bg-surface p-2 shadow-lg">
        <li className="px-2.5 pb-1 pt-1.5 text-xs font-medium uppercase tracking-wide text-fg-2">
          Your tenants
        </li>
        {tenants.map((t) => (
          <li key={t.slug}>
            <Link
              href={switchHref(pathname, t.slug)}
              aria-current={t.slug === current.slug ? "true" : undefined}
              className="flex min-h-12 items-center gap-2.5 rounded-control px-2.5 hover:bg-raised aria-[current=true]:bg-teal-tint"
            >
              <span className="flex flex-1 flex-col">
                <span className="font-semibold">{t.name}</span>
                {t.role && <span className="text-xs capitalize text-fg-2">{t.role}</span>}
              </span>
              {t.slug === current.slug && <span className="text-sm text-teal-fg">Current</span>}
            </Link>
          </li>
        ))}
      </ul>
    </details>
  );
}
