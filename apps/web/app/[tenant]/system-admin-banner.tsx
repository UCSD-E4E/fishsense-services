"use client";

import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";
import type { Banner } from "@/lib/system-admin";
import { unlockTenantAction } from "./unlock-action";

/**
 * Across every page of a tenant a system admin is not in (0056): read-only,
 * with a deliberate unlock behind a warning; or, unlocked, until when.
 */
export function SystemAdminBanner({ slug, name, banner }: { slug: string; name: string; banner: Banner }) {
  const router = useRouter();
  const [confirmed, setConfirmed] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pending, start] = useTransition();

  if (banner.kind === "unlocked") {
    return (
      <div role="status" className="border-b border-warn/50 bg-warn/10 px-6 py-3 text-sm">
        <div className="mx-auto max-w-7xl text-warn">
          <strong>Editing unlocked.</strong> You are changing {name} as a system admin until{" "}
          {new Date(banner.until).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}.
          This is recorded.
        </div>
      </div>
    );
  }
  return (
    <div role="status" className="border-b border-line bg-raised px-6 py-3 text-sm">
      <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-x-4 gap-y-2">
        <span>
          <strong>System admin view.</strong> You are not a member of {name}: you can read it, and
          change nothing. Your visit is recorded.
        </span>
        <details className="ml-auto">
          <summary className="cursor-pointer font-medium text-teal-fg">Unlock editing…</summary>
          <form
            className="mt-3 flex max-w-xl flex-col gap-3 rounded-card border border-warn/50 bg-surface p-4"
            onSubmit={(event) => {
              event.preventDefault();
              setError(null);
              start(async () => {
                try {
                  await unlockTenantAction(slug, confirmed);
                  router.refresh();
                } catch (e) {
                  setError(e instanceof Error ? e.message : "Could not unlock");
                }
              });
            }}
          >
            <p className="text-warn">
              <strong>Warning:</strong> unlocking makes you {name}&apos;s admin for an hour. Anything
              you change is {name}&apos;s data, not yours, and the unlock is recorded.
            </p>
            <label className="flex items-start gap-2">
              <input
                type="checkbox"
                className="mt-1"
                checked={confirmed}
                onChange={(e) => setConfirmed(e.target.checked)}
              />
              <span>I mean to change {name}&apos;s data as a system admin.</span>
            </label>
            {error && <p className="text-error">{error}</p>}
            <button
              type="submit"
              disabled={!confirmed || pending}
              className="inline-flex h-10 w-fit items-center rounded-control bg-teal px-4 font-semibold text-on-teal disabled:opacity-50"
            >
              {pending ? "Unlocking…" : "Unlock editing for an hour"}
            </button>
          </form>
        </details>
      </div>
    </div>
  );
}
