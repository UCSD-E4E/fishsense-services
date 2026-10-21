"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { navItems } from "@/lib/nav";

/** The main navigation, marking the section the current page is in. */
export function MainNav({ staff, lastTenant }: { staff: boolean; lastTenant: string | null }) {
  const pathname = usePathname() ?? "/";
  return (
    <ul className="flex flex-wrap gap-1">
      {navItems(pathname, { staff, lastTenant }).map((item) => (
        <li key={item.href}>
          <Link
            href={item.href}
            aria-current={item.active ? "page" : undefined}
            className={
              item.active
                ? "inline-flex h-10 items-center rounded-control bg-teal-tint px-3 font-semibold text-teal-fg"
                : "inline-flex h-10 items-center rounded-control px-3 text-fg-2 hover:bg-raised hover:text-fg"
            }
          >
            {item.label}
          </Link>
        </li>
      ))}
    </ul>
  );
}
