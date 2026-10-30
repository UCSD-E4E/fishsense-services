import Link from "next/link";
import { formatLength, formatLengthWithError } from "@/lib/captures";
import { againstHuman, automaticOverlay, statusLabel, type AutomaticFrame } from "@/lib/automatic";
import { colorLabel } from "@/lib/laser-color";

/**
 * What the automatic run made of this frame (the owner, 2026-10-21: review the
 * automatic process here). Always marked automatic and unvalidated, and kept
 * apart from the measurement above. The points and mask are drawn on the
 * render they were predicted on: SAM's mask in teal, the laser dot, and the
 * head (snout color) and tail (fork color).
 */
export function AutomaticSection({
  frame,
  humanLengthM,
  figure,
  reviewHref,
}: {
  frame: AutomaticFrame;
  humanLengthM: number | null | undefined;
  /** In place of the drawing: the frame page's review workspace. */
  figure?: React.ReactNode;
  /** Where this frame is reviewed (its frame page), from the fish page. */
  reviewHref?: string | null;
}) {
  const overlay = automaticOverlay(frame);
  const against = againstHuman(frame.length_m, frame.length_sd_m, humanLengthM);
  const signed = against ? `${against.percent >= 0 ? "+" : ""}${against.percent.toFixed(1)}%` : "";
  // How the run reads against the measurement above: agreeing within its error
  // bar, differing outside it, or (no error bar) measured separately -- never
  // that it "isn't the measurement above" when the two agree (the owner,
  // 2026-10-30).
  const summary =
    against?.kind === "agrees"
      ? "What the automatic run found on this frame. It agrees with the measurement above, within its error."
      : against?.kind === "differs"
        ? `What the automatic run found on this frame. It differs from the measurement above by ${signed}, outside its error.`
        : against
          ? "What the automatic run found on this frame, measured separately from the measurement above."
          : "What the automatic run found on this frame.";
  return (
    <section className="mt-5 rounded-card border border-line bg-surface px-6 py-5">
      <div className="mb-3 flex flex-wrap items-baseline gap-3">
        <h2 className="text-lg font-semibold">Automatic</h2>
        <span className="inline-flex h-6 items-center rounded-chip bg-raised px-2.5 text-xs font-semibold text-warn">
          Unvalidated
        </span>
        <span className="text-sm text-fg-2">
          {figure
            ? "What the automatic run found on this frame. Review it here."
            : summary}
        </span>
        {reviewHref && (
          <Link href={reviewHref} className="ml-auto text-sm font-medium text-teal-fg hover:underline">
            Review this frame →
          </Link>
        )}
      </div>
      <div className="flex flex-wrap items-start gap-6">
        {figure ? (
          <div className="min-w-0 flex-[999_1_480px]">{figure}</div>
        ) : (
        <figure className="m-0 min-w-0 flex-[999_1_480px]">
          {overlay ? (
            <div className="relative overflow-hidden rounded-card bg-raised">
              {/* Presigned, short-lived Garage URLs: not for next/image's optimizer. */}
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src={frame.render_url!} alt="The frame the automatic run saw" className="block w-full" />
              <svg
                viewBox={`0 0 ${overlay.width} ${overlay.height}`}
                preserveAspectRatio="none"
                className="absolute inset-0 h-full w-full"
                role="img"
                aria-label="The automatic result: the fish mask, the laser dot, and the head and tail"
              >
                <defs>
                  {/* The mask is grayscale, 255 on the fish: its brightness becomes teal's opacity. */}
                  <filter id="automatic-mask" colorInterpolationFilters="sRGB">
                    <feColorMatrix type="matrix" values="0 0 0 0 0  0 0 0 0 0  0 0 0 0 0  1 0 0 0 0" result="alpha" />
                    <feFlood floodColor="#00AAA5" />
                    <feComposite in2="alpha" operator="in" />
                  </filter>
                </defs>
                {overlay.maskUrl && (
                  <image
                    href={overlay.maskUrl}
                    width={overlay.width}
                    height={overlay.height}
                    filter="url(#automatic-mask)"
                    opacity={0.35}
                  />
                )}
                <Points overlay={overlay} />
              </svg>
            </div>
          ) : (
            <div className="flex aspect-4/3 items-center justify-center rounded-card border border-dashed border-line bg-raised text-sm text-fg-2">
              The render this result was made on isn&apos;t available
            </div>
          )}
          <figcaption className="mt-2 flex flex-wrap gap-4 text-sm text-fg-2">
            <Key className="bg-snout">Head</Key>
            <Key className="bg-fork">Tail</Key>
            <Key className="bg-laser">Laser dot</Key>
            {overlay?.maskUrl && <Key className="bg-teal">Fish mask</Key>}
          </figcaption>
        </figure>
        )}
        <dl className="grid min-w-0 flex-[1_1_260px] grid-cols-[max-content_minmax(0,1fr)] gap-x-5 gap-y-2.5 text-sm">
          <Row label="Result">{statusLabel(frame.status)}</Row>
          <Row label="Length">
            {frame.length_m != null
              ? formatLengthWithError(frame.length_m, frame.length_sd_m)
              : frame.refusal ?? "—"}
          </Row>
          {against && (
            <Row label="Against the human">
              {signed} ({formatLength(humanLengthM)})
              {against.kind === "agrees" && " · within the error"}
              {against.kind === "differs" && " · outside the error"}
            </Row>
          )}
          <Row label="Laser depth">{frame.depth_m != null ? `${frame.depth_m.toFixed(2)} m` : "—"}</Row>
          {frame.laser && (
            <Row label="Laser color">{colorLabel(frame.laser_color, frame.laser_color_margin, frame.laser_color_source)}</Row>
          )}
          <Row label="Calibration">
            {frame.calibration_source === "label_free"
              ? frame.length_sd_m != null
                ? "Label-free fit (± is the calibration's error, 1 SD)"
                : "Label-free fit"
              : frame.calibration_source === "stored"
                ? "The mission's stored calibration"
                : "—"}
          </Row>
          <Row label="Mask score">{frame.sam_score != null ? frame.sam_score.toFixed(2) : "—"}</Row>
          <Row label="Species guess">
            {frame.species
              ? `${frame.species}${frame.species_probability != null ? ` (${Math.round(frame.species_probability * 100)}%)` : ""}`
              : "—"}
          </Row>
          <Row label="Model">
            <span className="font-mono text-[13px] text-fg-2">
              {[frame.checkpoint, frame.core_version && `core ${frame.core_version}`].filter(Boolean).join(" · ") || "—"}
            </span>
          </Row>
          <Row label="Run">{new Date(frame.predicted_at).toLocaleString()}</Row>
        </dl>
      </div>
    </section>
  );
}

function Points({ overlay }: { overlay: NonNullable<ReturnType<typeof automaticOverlay>> }) {
  const r = overlay.width / 110;
  const { head, tail, laser } = overlay;
  return (
    <>
      {head && tail && (
        <line
          x1={head.x}
          y1={head.y}
          x2={tail.x}
          y2={tail.y}
          stroke="#FFFFFF"
          strokeWidth={overlay.width / 400}
          strokeDasharray={`${overlay.width / 120} ${overlay.width / 150}`}
        />
      )}
      {laser && (
        <circle cx={laser.x} cy={laser.y} r={r * 0.7} className="fill-laser stroke-ground" strokeWidth={r / 2.5} />
      )}
      {head && <circle cx={head.x} cy={head.y} r={r} className="fill-snout stroke-ground" strokeWidth={r / 2.5} />}
      {tail && <circle cx={tail.x} cy={tail.y} r={r} className="fill-fork stroke-ground" strokeWidth={r / 2.5} />}
    </>
  );
}

function Key({ className, children }: { className: string; children: React.ReactNode }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className={`size-2.5 rounded-full ${className}`} aria-hidden />
      {children}
    </span>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <>
      <dt className="text-fg-2">{label}</dt>
      <dd className="min-w-0 [overflow-wrap:anywhere]">{children}</dd>
    </>
  );
}
