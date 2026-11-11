import { formatLength } from "@/lib/captures";
import { phoneStatusLabel, type PhoneMeasurement } from "@/lib/phone-measurement";

/**
 * What the server measured on a phone capture nobody measured (the owner,
 * 2026-11-10), as FishSense Mobile measures: fishial's mask, its snout and
 * fork, and the LiDAR depth at each. Always marked unvalidated: nobody has
 * reviewed it, and it is never counted as a measured fish. Drawn on the
 * capture's own JPEG, whose full-frame pixels the points and mask are in.
 */
export function PhoneSection({
  measurement,
  maskSrc,
}: {
  measurement: PhoneMeasurement;
  maskSrc: string | null;
}) {
  const m = measurement;
  const width = m.mask_width ?? 1920;
  const height = m.mask_height ?? 1440;
  const r = width / 110;
  return (
    <section className="rounded-card border border-line bg-surface px-6 py-5">
      <div className="mb-3 flex flex-wrap items-baseline gap-3">
        <h2 className="text-lg font-semibold">Measured by the server</h2>
        <span className="inline-flex h-6 items-center rounded-chip bg-raised px-2.5 text-xs font-semibold text-warn">
          Unvalidated
        </span>
        <span className="text-sm text-fg-2">
          Measured as FishSense Mobile measures, from the photo and the phone&apos;s depth. Nobody has
          reviewed it.
        </span>
      </div>
      <div className="flex flex-wrap items-start gap-6">
        <figure className="m-0 min-w-0 flex-[999_1_480px]">
          {m.photo_url ? (
            <div className="relative overflow-hidden rounded-card bg-raised">
              {/* Presigned, short-lived Garage URLs: not for next/image's optimizer. */}
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src={m.photo_url} alt="The phone's photo" className="block w-full" />
              <svg
                viewBox={`0 0 ${width} ${height}`}
                preserveAspectRatio="none"
                className="absolute inset-0 h-full w-full"
                role="img"
                aria-label="The server's measurement: the fish mask, the snout and the fork"
              >
                <defs>
                  {/* The mask is grayscale, 255 on the fish: its brightness becomes teal's opacity. */}
                  <filter id="phone-mask" colorInterpolationFilters="sRGB">
                    <feColorMatrix type="matrix" values="0 0 0 0 0  0 0 0 0 0  0 0 0 0 0  1 0 0 0 0" result="alpha" />
                    <feFlood floodColor="#00AAA5" />
                    <feComposite in2="alpha" operator="in" />
                  </filter>
                </defs>
                {maskSrc && (
                  <image href={maskSrc} width={width} height={height} filter="url(#phone-mask)" opacity={0.35} />
                )}
                {m.snout && m.fork && (
                  <line
                    x1={m.snout.x}
                    y1={m.snout.y}
                    x2={m.fork.x}
                    y2={m.fork.y}
                    stroke="#FFFFFF"
                    strokeWidth={width / 400}
                    strokeDasharray={`${width / 120} ${width / 150}`}
                  />
                )}
                {m.snout && (
                  <circle cx={m.snout.x} cy={m.snout.y} r={r} className="fill-snout stroke-ground" strokeWidth={r / 2.5} />
                )}
                {m.fork && (
                  <circle cx={m.fork.x} cy={m.fork.y} r={r} className="fill-fork stroke-ground" strokeWidth={r / 2.5} />
                )}
              </svg>
            </div>
          ) : (
            <div className="flex aspect-4/3 items-center justify-center rounded-card border border-dashed border-line bg-raised text-sm text-fg-2">
              The photo isn&apos;t available
            </div>
          )}
          <figcaption className="mt-2 flex flex-wrap gap-4 text-sm text-fg-2">
            <Key className="bg-snout">Snout</Key>
            <Key className="bg-fork">Fork</Key>
            {maskSrc && <Key className="bg-teal">Fish mask</Key>}
          </figcaption>
        </figure>
        <dl className="grid min-w-0 flex-[1_1_260px] grid-cols-[max-content_minmax(0,1fr)] gap-x-5 gap-y-2.5 text-sm">
          <Row label="Result">{phoneStatusLabel(m.status)}</Row>
          <Row label="Length">{m.length_m != null ? formatLength(m.length_m) : "—"}</Row>
          <Row label="Depth">
            {m.snout_depth_m != null && m.fork_depth_m != null
              ? `snout ${m.snout_depth_m.toFixed(2)} m · fork ${m.fork_depth_m.toFixed(2)} m`
              : "—"}
          </Row>
          {m.error && <Row label="Why">{m.error}</Row>}
          <Row label="Model">
            <span className="font-mono text-[13px] text-fg-2">
              {[m.model_id, m.core_version && `core ${m.core_version}`, m.algorithm].filter(Boolean).join(" · ")}
            </span>
          </Row>
          <Row label="Measured">{new Date(m.measured_at).toLocaleString()}</Row>
        </dl>
      </div>
    </section>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <>
      <dt className="font-medium text-fg-2">{label}</dt>
      <dd className="min-w-0">{children}</dd>
    </>
  );
}

function Key({ className, children }: { className: string; children: React.ReactNode }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span aria-hidden className={`inline-block h-2.5 w-2.5 rounded-full ${className}`} />
      {children}
    </span>
  );
}
