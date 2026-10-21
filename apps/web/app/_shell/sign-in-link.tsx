"use client";

import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { signInHref } from "@/lib/shell";

const STYLE =
  "inline-flex h-10 items-center rounded-control bg-teal px-4 text-sm font-semibold text-on-teal";

/** Sign in, and come back to this page (`signInHref`). */
export function SignInLink() {
  const pathname = usePathname() ?? "/";
  const search = useSearchParams()?.toString() ?? "";
  return (
    <Link href={signInHref(pathname, search ? `?${search}` : "")} prefetch={false} className={STYLE}>
      Sign in
    </Link>
  );
}

/** Before the URL is known (static rendering): sign in, back to home. */
export function SignInFallback() {
  return (
    <Link href={signInHref("/", "")} prefetch={false} className={STYLE}>
      Sign in
    </Link>
  );
}
