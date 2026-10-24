"use client";

import { useState, useTransition } from "react";
import type { NewToken, Scope } from "@/lib/api-tokens";
import { mcpCommand } from "@/lib/mcp-command";
import { createTokenAction, revokeTokenAction } from "./actions";

const LIFETIMES = [
  { days: 30, label: "30 days" },
  { days: 90, label: "90 days" },
  { days: 365, label: "1 year" },
];

/** Make a token: read-only unless the person opts into edit. The secret is
 *  shown here once, with the command that connects an agent to it. */
export function TokenForm({ mcpUrl }: { mcpUrl: string }) {
  const [name, setName] = useState("");
  const [scope, setScope] = useState<Scope>("read");
  const [days, setDays] = useState(90);
  const [made, setMade] = useState<NewToken | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  return (
    <div className="space-y-4">
      <form
        className="space-y-4 rounded-card border border-line bg-surface p-4"
        onSubmit={(e) => {
          e.preventDefault();
          setError(null);
          setMade(null);
          startTransition(async () => {
            const result = await createTokenAction(name, scope, days);
            if (result.ok) {
              setMade(result.token);
              setName("");
              setScope("read");
            } else setError(result.error);
          });
        }}
      >
        <label className="block text-sm">
          <span className="font-medium">Name</span>
          <input
            required
            maxLength={100}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Claude Code on my laptop"
            className="mt-1 block w-full max-w-md rounded-control border border-line bg-surface px-3 py-2"
          />
        </label>
        <fieldset className="text-sm">
          <legend className="font-medium">Kind</legend>
          <label className="mt-2 flex items-start gap-2">
            <input type="radio" name="scope" checked={scope === "read"} onChange={() => setScope("read")} />
            <span>
              Read-only <span className="text-fg-2">— reads what you can read. Use this one for agents (MCP).</span>
            </span>
          </label>
          <label className="mt-2 flex items-start gap-2">
            <input type="radio" name="scope" checked={scope === "edit"} onChange={() => setScope("edit")} />
            <span>
              Edit <span className="text-fg-2">— for scripts that call the API directly. MCP won&apos;t accept it.</span>
            </span>
          </label>
          {scope === "edit" && (
            <p role="alert" className="mt-2 max-w-2xl rounded-card border border-line bg-raised p-3 text-fg">
              An edit token can change data in every tenant you belong to, as you, until it expires or you revoke
              it. Its kind can&apos;t be changed later. Make it only for a script that needs to write.
            </p>
          )}
        </fieldset>
        <label className="block text-sm">
          <span className="font-medium">Expires after</span>
          <select
            value={days}
            onChange={(e) => setDays(Number(e.target.value))}
            className="mt-1 block rounded-control border border-line bg-surface px-2 py-1"
          >
            {LIFETIMES.map((l) => (
              <option key={l.days} value={l.days}>
                {l.label}
              </option>
            ))}
          </select>
        </label>
        <button
          type="submit"
          disabled={pending}
          className="inline-flex h-10 items-center rounded-control border border-line px-3 text-sm text-fg hover:bg-raised disabled:opacity-50"
        >
          {scope === "edit" ? "Make edit token" : "Make read-only token"}
        </button>
        {error && <p className="text-sm text-error">{error}</p>}
      </form>

      {made && (
        <div className="space-y-2 rounded-card border border-line bg-raised p-4 text-sm">
          <p className="font-medium">
            Your new {made.scope === "edit" ? "edit" : "read-only"} token. Copy it now: it won&apos;t be shown again.
          </p>
          <code className="block overflow-x-auto rounded-control bg-surface p-2 font-mono">{made.token}</code>
          {made.scope === "read" ? (
            <>
              <p className="text-fg-2">To connect Claude Code:</p>
              <code className="block overflow-x-auto rounded-control bg-surface p-2 font-mono">
                {mcpCommand(mcpUrl, made.token)}
              </code>
            </>
          ) : (
            <p className="text-fg-2">
              Send it as <code className="font-mono">Authorization: Bearer …</code> to the REST API.
            </p>
          )}
        </div>
      )}
    </div>
  );
}

export function RevokeButton({ tokenId, name }: { tokenId: string; name: string }) {
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();
  return (
    <span className="inline-flex flex-col gap-1">
      <button
        type="button"
        aria-label={`Revoke ${name}`}
        disabled={pending}
        onClick={() => {
          if (!window.confirm(`Revoke "${name}"? Anything using it stops working.`)) return;
          setError(null);
          startTransition(async () => {
            const result = await revokeTokenAction(tokenId);
            if (!result.ok) setError(result.error);
          });
        }}
        className="rounded-control border border-line px-2 py-1 text-sm hover:bg-raised disabled:opacity-50"
      >
        Revoke
      </button>
      {error && <span className="text-xs text-error">{error}</span>}
    </span>
  );
}
