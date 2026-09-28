// Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/app/page.tsx.
import Image from "next/image";
import { getActiveProjects } from "@/lib/active-projects";
import { buildSections, type Section, type SectionLink } from "@/lib/sections";
import { ADMIN_LINKS, RESULTS_LINKS } from "@/lib/static-links";

export const dynamic = "force-dynamic";

const FETCH_REVALIDATE_SECONDS = 300;

export default async function HomePage() {
  const active = await getActiveProjects(FETCH_REVALIDATE_SECONDS);
  const sections = buildSections(active, {
    results: RESULTS_LINKS,
    admin: ADMIN_LINKS,
  });

  return (
    <main className="mx-auto max-w-6xl px-6 py-12">
      <header className="mb-10 flex items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          {/*
            Dark-mode contrast: bg-slate-950 makes the as-authored black
            fish outline nearly invisible. invert turns black->white
            (good) and the brand-critical red laser dot ->cyan (bad);
            the chained 180deg hue-rotate brings cyan back to red while
            leaving white unchanged (white has no chroma to rotate).
            Net effect: outline flips for contrast, red stays red.

            Width/height come from the source viewBox (207.5 x 123,
            ratio ~1.69:1). h-12 (48px) renders ~81px wide; sized via
            the ratio so next/image can reserve the exact box and avoid
            CLS.
          */}
          <Image
            src="/logo.svg"
            alt=""
            aria-hidden
            width={81}
            height={48}
            priority
            // next/image's default loader 400s on SVGs (XSS-safe default
            // — see Next.js docs on dangerouslyAllowSVG). The asset is
            // a hand-extracted vector with no scripts; nothing to
            // optimize either way, so bypass the loader and serve the
            // file from /public/ as-is.
            unoptimized
            className="h-12 w-auto dark:invert dark:hue-rotate-180"
          />
          <h1 className="text-4xl font-semibold tracking-tight">E4E FishSense</h1>
        </div>
        <a
          href="/portal"
          className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white shadow-sm transition hover:bg-slate-800 dark:bg-slate-100 dark:text-slate-900 dark:hover:bg-white"
        >
          Sign in
        </a>
      </header>
      {active.degraded > 0 && <DegradedNotice count={active.degraded} />}
      <div className="space-y-10">
        {sections.map((section) => (
          <SectionView key={section.title} section={section} />
        ))}
      </div>
    </main>
  );
}

/**
 * Says out loud that the list below is short.
 *
 * Without this, Label Studio being unreachable rendered as an absence: on
 * 2026-09-07 all 45 head/tail projects were rate-limited and silently
 * dropped, so the page showed no Head/Tail section at all while labelers had
 * a full queue. A page that could not ask must not present the answer it does
 * have as the whole one.
 */
function DegradedNotice({ count }: { count: number }) {
  return (
    <div
      role="status"
      className="mb-8 rounded-lg border border-amber-300 bg-amber-50 p-4 text-sm text-amber-900 dark:border-amber-700/60 dark:bg-amber-950/40 dark:text-amber-200"
    >
      <span className="font-medium">Some projects could not be loaded.</span>{" "}
      Label Studio did not answer for {count} labeling{" "}
      {count === 1 ? "project" : "projects"}, so this list is incomplete.
      Reload in a moment to see the rest.
    </div>
  );
}

function SectionView({ section }: { section: Section }) {
  return (
    <section>
      <h2 className="mb-4 text-xl font-medium text-slate-700 dark:text-slate-300">
        {section.title}
      </h2>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {section.links.map((link) => (
          <LinkCard key={link.href} link={link} />
        ))}
      </div>
    </section>
  );
}

function LinkCard({ link }: { link: SectionLink }) {
  return (
    <a
      href={link.href}
      target="_blank"
      rel="noreferrer"
      className="block rounded-lg border border-slate-200 bg-white p-5 shadow-sm transition hover:border-slate-300 hover:shadow-md dark:border-slate-800 dark:bg-slate-900 dark:hover:border-slate-700"
    >
      <div className="text-base font-medium">{link.title}</div>
      <div className="mt-1 text-sm text-slate-600 dark:text-slate-400">
        {link.description}
      </div>
    </a>
  );
}
