"use client";

// An admin's edits to a mission's facts: its port (with what they know it
// from), its purpose, and its deployment. Each saves on its own and shows the
// API's words when refused -- a port fixed by the owner's rules is.

import { useState, useTransition } from "react";
import {
  PORTS,
  PURPOSES,
  portLabel,
  purposeLabel,
  type Deployment,
  type MissionDetail,
} from "@/lib/mission-facts";
import { setDeploymentAction, setPortAction, setPurposeAction } from "./actions";

const field =
  "rounded-md border border-slate-300 bg-white px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900";
const button =
  "rounded-md bg-slate-900 px-3 py-1 text-sm font-medium text-white disabled:opacity-50 dark:bg-slate-100 dark:text-slate-900";

function useSave() {
  const [pending, start] = useTransition();
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  function save(action: () => Promise<{ ok: true } | { ok: false; error: string }>) {
    setError(null);
    setSaved(false);
    start(async () => {
      const result = await action();
      if (result.ok) setSaved(true);
      else setError(result.error);
    });
  }
  return { pending, error, saved, save };
}

function Status({ error, saved }: { error: string | null; saved: boolean }) {
  if (error) return <p className="text-sm text-red-700 dark:text-red-400">{error}</p>;
  if (saved) return <p className="text-sm text-emerald-700 dark:text-emerald-400">Saved.</p>;
  return null;
}

export function FactsForms({
  slug,
  mission,
  deployments,
}: {
  slug: string;
  mission: MissionDetail;
  deployments: Deployment[];
}) {
  const [port, setPort] = useState<string>(mission.port);
  // The recorded note, so saving the port again keeps its provenance.
  const [note, setNote] = useState(mission.port_note ?? "");
  const [purpose, setPurpose] = useState<string>(mission.purpose ?? "");
  const [deployment, setDeployment] = useState<string>(
    mission.deployment_number != null ? String(mission.deployment_number) : "",
  );
  const portSave = useSave();
  const purposeSave = useSave();
  const deploymentSave = useSave();
  const ruled = mission.port_source === "backfill" || mission.port_source === "device_suffix";

  return (
    <section className="space-y-6 rounded-lg border border-slate-200 p-5 dark:border-slate-800">
      <h2 className="text-lg font-semibold">Edit (admin)</h2>

      <div className="space-y-2">
        <label className="block text-sm font-medium" htmlFor="port">
          Camera port
        </label>
        {ruled ? (
          <p className="text-sm text-slate-600 dark:text-slate-400">
            {portLabel(mission.port, mission.port_source)} is the owner&apos;s rule for this
            mission and can&apos;t be changed here.
          </p>
        ) : (
          <div className="flex flex-wrap items-center gap-2">
            <select id="port" className={field} value={port} onChange={(e) => setPort(e.target.value)}>
              {PORTS.map((p) => (
                <option key={p} value={p}>
                  {portLabel(p, null)}
                </option>
              ))}
            </select>
            <input
              className={`${field} min-w-64 flex-1`}
              placeholder="How you know (kept with the port)"
              value={note}
              onChange={(e) => setNote(e.target.value)}
            />
            <button
              className={button}
              disabled={portSave.pending}
              onClick={() => portSave.save(() => setPortAction(slug, mission.number, port, note))}
            >
              Save port
            </button>
          </div>
        )}
        <Status error={portSave.error} saved={portSave.saved} />
      </div>

      <div className="space-y-2">
        <label className="block text-sm font-medium" htmlFor="purpose">
          Purpose
        </label>
        <div className="flex flex-wrap items-center gap-2">
          <select id="purpose" className={field} value={purpose} onChange={(e) => setPurpose(e.target.value)}>
            <option value="">Not stated</option>
            {PURPOSES.map((p) => (
              <option key={p} value={p}>
                {purposeLabel(p)}
              </option>
            ))}
          </select>
          <button
            className={button}
            disabled={purposeSave.pending}
            onClick={() => purposeSave.save(() => setPurposeAction(slug, mission.number, purpose))}
          >
            Save purpose
          </button>
        </div>
        <Status error={purposeSave.error} saved={purposeSave.saved} />
      </div>

      <div className="space-y-2">
        <label className="block text-sm font-medium" htmlFor="deployment">
          Deployment
        </label>
        <div className="flex flex-wrap items-center gap-2">
          <select
            id="deployment"
            className={field}
            value={deployment}
            onChange={(e) => setDeployment(e.target.value)}
          >
            <option value="">None</option>
            {deployments.map((d) => (
              <option key={d.number} value={String(d.number)}>
                {d.name}
              </option>
            ))}
          </select>
          <button
            className={button}
            disabled={deploymentSave.pending}
            onClick={() =>
              deploymentSave.save(() => setDeploymentAction(slug, mission.number, deployment))
            }
          >
            Save deployment
          </button>
        </div>
        <Status error={deploymentSave.error} saved={deploymentSave.saved} />
      </div>
    </section>
  );
}
