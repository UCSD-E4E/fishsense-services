"use client";

// Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/app/error.tsx.

import { useEffect } from "react";

// Next.js App Router error boundary. Catches uncaught errors thrown by
// SSR (e.g. fishsense-api / Label Studio fetch failures bubbling out of
// HomePage) and any client-side exceptions in the same segment. The
// digest field is set by Next on server-side errors and matches the
// `[Error: ... digest=...]` line Next.js writes to stderr — logging it
// here lets operators correlate a user-facing error page with the
// server log entry.
export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error("[error-boundary] rendering fallback", {
      message: error.message,
      digest: error.digest,
    });
  }, [error]);

  return (
    <main className="mx-auto max-w-2xl px-6 py-16 text-center">
      <h1 className="text-3xl font-semibold tracking-tight">
        Something went wrong
      </h1>
      <p className="mt-4 text-slate-600 dark:text-slate-400">
        The page failed to load. This is usually transient — try again, and if
        it persists, check that fishsense-api and Label Studio are reachable.
      </p>
      {error.digest ? (
        <p className="mt-2 text-xs text-slate-500">
          Error reference: <code>{error.digest}</code>
        </p>
      ) : null}
      <button
        type="button"
        onClick={reset}
        className="mt-8 rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white shadow-sm transition hover:bg-slate-800 dark:bg-slate-100 dark:text-slate-900 dark:hover:bg-white"
      >
        Try again
      </button>
    </main>
  );
}
