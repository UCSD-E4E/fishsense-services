// Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/app/page.tsx: the
// labeling projects with work waiting, and the dashboards and workflows.
// Shown on Today: the labeling to staff, and the dashboards (Superset,
// Temporal) to system admins only (0056; the owner, 2026-10-20).
import { getActiveProjects } from "@/lib/active-projects";
import { buildSections, type Section, type SectionLink } from "@/lib/sections";
import { ADMIN_LINKS, RESULTS_LINKS } from "@/lib/static-links";

const FETCH_REVALIDATE_SECONDS = 300;

export async function WorkSections({ dashboards }: { dashboards: boolean }) {
  const active = await getActiveProjects(FETCH_REVALIDATE_SECONDS);
  const sections = buildSections(active, {
    results: dashboards ? RESULTS_LINKS : [],
    admin: dashboards ? ADMIN_LINKS : [],
  });
  return (
    <>
      {active.unavailable.length > 0 && <UnavailableNotice />}
      {active.degraded > 0 && <DegradedNotice count={active.degraded} />}
      <div className="space-y-10">
        {sections.map((section) => (
          <SectionView key={section.title} section={section} />
        ))}
      </div>
    </>
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
      className="mb-8 rounded-card border border-warn/50 bg-warn/10 p-4 text-sm text-warn"
    >
      <span className="font-medium">Some projects could not be loaded.</span>{" "}
      Label Studio did not answer for {count} labeling{" "}
      {count === 1 ? "project" : "projects"}, so this list is incomplete. Reload
      in a moment to see the rest.
    </div>
  );
}

/**
 * Says the labeling sections could not be listed at all.
 *
 * v2: the web asks the API with its own Authentik service token, so an
 * Authentik or API outage leaves some kinds unknown. Their sections are
 * missing because nothing could be asked, not because nothing is
 * outstanding -- the same lie DegradedNotice exists to prevent.
 */
function UnavailableNotice() {
  return (
    <div
      role="status"
      className="mb-8 rounded-card border border-warn/50 bg-warn/10 p-4 text-sm text-warn"
    >
      <span className="font-medium">Labeling work could not be listed.</span>{" "}
      FishSense did not answer, so some labeling sections are missing below.
      Reload in a moment to see them.
    </div>
  );
}

function SectionView({ section }: { section: Section }) {
  return (
    <section>
      <h2 className="mb-4 text-xl font-semibold">
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
      className="block rounded-card border border-line bg-surface p-5 transition hover:border-fg-3"
    >
      <div className="text-base font-medium">{link.title}</div>
      <div className="mt-1 text-sm text-fg-2">
        {link.description}
      </div>
    </a>
  );
}
