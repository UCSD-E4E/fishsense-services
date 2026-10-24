import { requireSignedIn } from "@/app/_shell/guard";
import { listTokens, mcpCommand } from "@/lib/api-tokens";
import { RevokeButton, TokenForm } from "./token-form";

export const dynamic = "force-dynamic";

const when = (iso: string | null) =>
  iso ? new Date(iso).toLocaleDateString("en-US", { dateStyle: "medium", timeZone: "America/Los_Angeles" }) : "—";

function state(t: { revoked_at: string | null; expires_at: string }): string {
  if (t.revoked_at) return "Revoked";
  return new Date(t.expires_at) <= new Date() ? "Expired" : "Live";
}

/**
 * Personal API tokens (0066; the owner, 2026-10-23): each acts as the person
 * who made it, read-only unless they opted into edit. Agents use a read token
 * with the MCP endpoint; edit tokens are for scripts calling the REST API.
 */
export default async function TokensPage() {
  const session = await requireSignedIn("/tokens");
  const tokens = await listTokens(session.accessToken ?? "");

  return (
    <main className="mx-auto max-w-5xl space-y-8 px-6 py-12">
      <div>
        <h1 className="text-3xl font-semibold tracking-tight">API tokens</h1>
        <p className="mt-1 max-w-3xl text-sm text-fg-2">
          A token lets a script or an AI agent use FishSense as you. A read-only token reads what you can read:
          your tenants, or every tenant if you are a system admin, and nothing more. Agents connect to FishSense&apos;s
          MCP endpoint with one. An edit token can also change data, through the API only.
        </p>
      </div>

      <TokenForm mcpCommandFor={mcpCommand("TOKEN")} />

      <div className="overflow-x-auto rounded-card border border-line bg-surface">
        <table className="w-full text-sm">
          <thead className="text-left text-fg-2">
            <tr className="border-b border-line">
              <th className="px-4 py-2.5 font-medium">Name</th>
              <th className="px-4 py-2.5 font-medium">Kind</th>
              <th className="px-4 py-2.5 font-medium">Starts with</th>
              <th className="px-4 py-2.5 font-medium">Last used</th>
              <th className="px-4 py-2.5 font-medium">Expires</th>
              <th className="px-4 py-2.5 font-medium">State</th>
              <th className="px-4 py-2.5" />
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {tokens.length === 0 && (
              <tr>
                <td colSpan={7} className="px-4 py-2.5 text-fg-2">
                  No tokens yet.
                </td>
              </tr>
            )}
            {tokens.map((t) => (
              <tr key={t.id}>
                <td className="px-4 py-2.5 font-medium">{t.name}</td>
                <td className="px-4 py-2.5">{t.scope === "edit" ? "Edit" : "Read-only"}</td>
                <td className="px-4 py-2.5 font-mono">{t.prefix}…</td>
                <td className="px-4 py-2.5">{when(t.last_used_at)}</td>
                <td className="px-4 py-2.5">{when(t.expires_at)}</td>
                <td className="px-4 py-2.5">{state(t)}</td>
                <td className="px-4 py-2.5 text-right">
                  {state(t) === "Live" && <RevokeButton tokenId={t.id} name={t.name} />}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </main>
  );
}
