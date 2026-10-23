import Link from "next/link";
import { notFound } from "next/navigation";
import { requireSignedIn } from "@/app/_shell/guard";
import { tenantFor } from "@/app/_shell/tenant";
import { isSlug } from "@/lib/ids";
import { getMembers, memberLabel, type Kind } from "@/lib/members";
import { paths } from "@/lib/tenant-paths";
import { SkillSelect } from "./skill-select";

export const dynamic = "force-dynamic";

type Props = { params: Promise<{ tenant: string }> };

const KINDS: { kind: Kind; label: string }[] = [
  { kind: "laser", label: "Laser dot" },
  { kind: "head_tail", label: "Head and tail" },
  { kind: "species", label: "Species" },
];

/**
 * The tenant's members and how far their reviews are trusted (0063, 0065),
 * for its admins. A Trusted or Expert review counts at once; a Learning one
 * waits for a Trusted reviewer, or a second learner who agrees. Names and
 * emails are as each person's sign-in last gave them (the owner, 2026-10-22).
 */
export default async function MembersPage({ params }: Props) {
  const { tenant: slug } = await params;
  if (!isSlug(slug)) notFound();
  const session = await requireSignedIn(paths.members(slug));
  const token = session.accessToken ?? "";
  const tenant = await tenantFor(token, slug);
  const members = tenant.isAdmin ? await getMembers(token, slug) : null;

  return (
    <main className="mx-auto max-w-5xl px-6 py-12">
      <nav aria-label="Breadcrumb" className="mb-6 text-sm text-fg-2">
        <Link href={paths.data(slug)} className="text-teal-fg hover:underline">
          Data
        </Link>{" "}
        <span aria-hidden>›</span> Members
      </nav>
      <h1 className="text-3xl font-semibold tracking-tight">Members</h1>
      <p className="mt-1 mb-6 max-w-3xl text-sm text-fg-2">
        How far each member&apos;s reviews of the automatic results are trusted. A Trusted or Expert review
        counts at once and becomes the frame&apos;s label; a Learning one waits until a Trusted reviewer
        confirms it, or another learner agrees. Names are as each person last signed in.
      </p>

      {!members ? (
        <p className="rounded-card border border-line bg-raised p-4 text-sm text-fg-2">
          Only {tenant.name}&apos;s admins can see its members.
        </p>
      ) : (
        <div className="overflow-x-auto rounded-card border border-line bg-surface">
          <table className="w-full text-sm">
            <thead className="text-left text-fg-2">
              <tr className="border-b border-line">
                <th className="px-4 py-2.5 font-medium">Member</th>
                <th className="px-4 py-2.5 font-medium">Role</th>
                {KINDS.map((k) => (
                  <th key={k.kind} className="px-4 py-2.5 font-medium">
                    {k.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {members.map((m) => (
                <tr key={m.user_id}>
                  <td className="px-4 py-2.5">
                    <span className="font-medium">{memberLabel(m)}</span>
                    {m.name && m.email && <span className="block text-xs text-fg-2">{m.email}</span>}
                  </td>
                  <td className="px-4 py-2.5 capitalize">{m.role}</td>
                  {KINDS.map((k) => (
                    <td key={k.kind} className="px-4 py-2.5">
                      <SkillSelect
                        slug={slug}
                        userId={m.user_id}
                        kind={k.kind}
                        level={m.skills[k.kind] ?? "learning"}
                        label={`${memberLabel(m)}: ${k.label}`}
                      />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </main>
  );
}
