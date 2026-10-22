import { agreement, statusLabel, type MissionAutomatic } from "@/lib/automatic";

/** Whether a mission has an automatic run worth showing. */
export function hasRun(run: MissionAutomatic | null): run is MissionAutomatic {
  return !!run && (Object.keys(run.by_status).length > 0 || !!run.calibration || run.failures.length > 0);
}

/**
 * A mission's automatic run, for review (the owner, 2026-10-21): what it made
 * of the frames, its label-free calibration, its lengths, how they agree with
 * the human lengths on the same frames, and what failed. Marked automatic and
 * unvalidated; the measurements are elsewhere on the page.
 */
export function AutomaticRun({ run }: { run: MissionAutomatic }) {
  const calibration = run.calibration;
  const refusals = Object.entries(run.refusals);
  return (
    <section className="mb-8 rounded-card border border-line bg-surface px-6 py-5">
      <div className="mb-3 flex flex-wrap items-baseline gap-3">
        <h2 className="text-lg font-semibold">Automatic run</h2>
        <span className="inline-flex h-6 items-center rounded-chip bg-raised px-2.5 text-xs font-semibold text-warn">
          Unvalidated
        </span>
        {run.excluded && <span className="text-sm text-fg-2">Excluded from the backlog: {run.excluded}</span>}
      </div>
      <dl className="grid grid-cols-1 gap-x-6 gap-y-2.5 text-sm sm:grid-cols-[12rem_1fr]">
        <dt className="text-fg-2">Frames</dt>
        <dd>
          <ul className="space-y-0.5">
            {Object.entries(run.by_status).map(([status, n]) => (
              <li key={status}>
                <span className="tabular-nums">{n.toLocaleString()}</span> {statusLabel(status).toLowerCase()}
              </li>
            ))}
            {run.unprocessed > 0 && (
              <li className="text-fg-2">
                <span className="tabular-nums">{run.unprocessed.toLocaleString()}</span> not reached yet
              </li>
            )}
            <li className="text-fg-2">of {run.frames.toLocaleString()} frames</li>
          </ul>
        </dd>
        <dt className="text-fg-2">Label-free calibration</dt>
        <dd>
          {calibration
            ? calibration.outcome === "accepted"
              ? `Accepted: ${calibration.frames_used ?? "?"} frames, ${calibration.pair_count ?? "?"} pairs` +
                (calibration.se_px != null ? `, SE ${calibration.se_px.toFixed(2)} px` : "")
              : `Refused: ${calibration.refusal_reason ?? "no reason given"}`
            : "None"}
        </dd>
        <dt className="text-fg-2">Lengths use</dt>
        <dd>
          {run.lengths_use === "label_free"
            ? "The label-free fit"
            : run.lengths_use === "stored"
              ? "The mission's stored calibration"
              : "No calibration yet"}
        </dd>
        <dt className="text-fg-2">Lengths</dt>
        <dd>
          {run.lengths.toLocaleString()}
          {refusals.length > 0 && (
            <span className="text-fg-2">
              {" "}
              · refused: {refusals.map(([why, n]) => `${n} ${why.replaceAll("_", " ")}`).join(", ")}
            </span>
          )}
        </dd>
        <dt className="text-fg-2">Against the human lengths</dt>
        <dd>{agreement(run)}</dd>
        {run.failures.length > 0 && (
          <>
            <dt className="text-fg-2">Failures</dt>
            <dd>
              <ul className="space-y-0.5 text-error">
                {run.failures.map((f, i) => (
                  <li key={i}>
                    {f.step}: {f.error}{" "}
                    <span className="text-fg-2">({new Date(f.created_at).toLocaleString()})</span>
                  </li>
                ))}
              </ul>
            </dd>
          </>
        )}
      </dl>
    </section>
  );
}
