import Link from "next/link";
import { notFound } from "next/navigation";
import { requireSignedIn } from "@/app/_shell/guard";
import { getAutomaticFrame } from "@/lib/automatic";
import { describeOrigin, formatLength, getCapture, millimeters, osmLink } from "@/lib/captures";
import { fishEvidence, type Overlay } from "@/lib/fish-evidence";
import { isSlug, isUuid } from "@/lib/ids";
import {
  fishName,
  formatError,
  getObservation,
  knownLengthError,
  methodLabel,
  where,
} from "@/lib/observations";
import { paths } from "@/lib/tenant-paths";
import { AutomaticSection } from "./automatic-section";
import { colorLabel } from "@/lib/laser-color";

export const dynamic = "force-dynamic";

type Props = {
  params: Promise<{ tenant: string; capture: string; fish: string }>;
};

type Point = { x: number; y: number } | null | undefined;

/**
 * One measured fish, with its evidence (docs/design/README.md; PLAN.md §4.9):
 * the photo with the measurement drawn on it, the length, how it was measured,
 * every version of the measurement, and every file behind it with its
 * checksum. A mobile fish's evidence is its capture's, so this page replaced
 * the Mobile captures page for it (retired in phase 7b), and shows what the
 * phone recorded with the capture too. A Lite fish's photo is its frame's render
 * (Phase 4), with its labeled head, tail and laser dot drawn on it
 * (`fish-evidence`: a render is the whole frame, never cropped).
 */
export default async function FishDetailPage({ params }: Props) {
  const { tenant, capture, fish } = await params;
  // A malformed address names no fish: 404, like an unknown one.
  if (!isSlug(tenant) || !isUuid(capture) || !isUuid(fish)) notFound();
  const session = await requireSignedIn(paths.fishDetail(tenant, capture, fish));
  const token = session.accessToken ?? "";
  const detail = await getObservation(token, tenant, capture, fish);
  if (!detail) notFound();

  const o = detail.observation;
  const phone = o.client_capture_uuid
    ? await getCapture(token, tenant, o.client_capture_uuid)
    : null;
  const evidence = fishEvidence(detail, phone);
  // What the automatic run made of a Lite frame, for review (2026-10-21).
  const automatic = o.method === "laser" ? await getAutomaticFrame(token, tenant, capture) : null;
  const photo = phone?.files.find((f) => f.role === "rgb_jpeg")?.url ?? o.photo_url;
  const name = fishName(o);
  const error = knownLengthError(o);
  const place = where(o);

  return (
    <main className="mx-auto max-w-7xl px-6 py-8">
      <nav aria-label="Breadcrumb" className="mb-6 text-sm text-fg-2">
        <Link href={paths.fish(tenant)} className="text-teal-fg hover:underline">
          Fish
        </Link>{" "}
        <span aria-hidden>›</span> {name.name}
      </nav>

      <div className="flex flex-wrap items-start gap-7">
        <figure className="m-0 min-w-0 flex-[999_1_560px]">
          {photo ? (
            <div className="relative overflow-hidden rounded-card bg-raised">
              {/* Presigned, short-lived Garage URLs: not for next/image's optimizer. */}
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src={photo} alt={name.name} className="block w-full" />
              {evidence.overlay && <MeasurementOverlay overlay={evidence.overlay} />}
            </div>
          ) : (
            <div className="flex aspect-4/3 items-center justify-center rounded-card border border-dashed border-line bg-raised text-sm text-fg-2">
              No photo of this frame is available yet
            </div>
          )}
          {evidence.overlay && (
            <figcaption className="mt-2.5 flex flex-wrap gap-4 text-sm text-fg-2">
              <Key className="bg-snout">{evidence.overlay.names.snout}</Key>
              <Key className="bg-fork">{evidence.overlay.names.fork}</Key>
              {evidence.overlay.laser && <Key className="bg-laser">Laser dot</Key>}
              {phone?.current_measurement && (
                <span>Points {describeOrigin(phone.current_measurement.origin)}</span>
              )}
            </figcaption>
          )}
        </figure>

        <aside className="flex min-w-0 flex-[1_1_360px] flex-col gap-5">
          <div className="flex flex-col gap-2 rounded-card border border-line bg-surface p-6">
            <span className="text-sm font-medium uppercase tracking-wide text-fg-2">
              Fork length
            </span>
            <span className="text-[56px] font-semibold leading-none tracking-tight tabular-nums">
              <Length meters={o.length_m} />
            </span>
            <div className="mt-1.5 flex flex-wrap gap-1.5">
              <Chip>{methodLabel(o.method)}</Chip>
            </div>
            <h1 className="mt-3 text-2xl font-semibold">{name.name}</h1>
            {name.detail && (
              <p className="text-fg-2">{o.species ? <i>{name.detail}</i> : name.detail}</p>
            )}
            {error !== null && (
              <p className="mt-2 rounded-control bg-raised p-3 text-sm">
                A fish model of known length {formatLength(o.fish_model?.known_length_m)}: this
                measurement is <strong>{formatError(error)}</strong> against it.
              </p>
            )}
          </div>

          <section className="rounded-card border border-line bg-surface px-6 py-5">
            <h2 className="mb-3 text-sm font-medium uppercase tracking-wide text-fg-2">
              Recorded with
            </h2>
            <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-5 gap-y-2.5">
              {o.mission && (
                <Row label="Mission">
                  {o.mission.number != null ? (
                    <Link
                      href={paths.mission(tenant, o.mission.number)}
                      className="text-teal-fg hover:underline"
                    >
                      {o.mission.number} · {o.mission.name ?? "unnamed"}
                    </Link>
                  ) : (
                    (o.mission.name ?? "—")
                  )}
                </Row>
              )}
              <Row label="Device">
                {[o.device.name, o.device.kind && `(${o.device.kind})`].filter(Boolean).join(" ") ||
                  "—"}
              </Row>
              <Row label="Taken">{new Date(o.captured_at).toLocaleString()}</Row>
              {place && <Row label="Where">{place}</Row>}
            </dl>
          </section>
        </aside>
      </div>

      <div className="mt-7 grid gap-5 [grid-template-columns:repeat(auto-fit,minmax(340px,1fr))]">
        <section className="rounded-card border border-line bg-surface px-6 py-5">
          <h2 className="mb-3 text-lg font-semibold">How it was measured</h2>
          <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-5 gap-y-2.5">
            {detail.laser && (
              <>
                <Row label="Laser depth">{meters(detail.laser.depth_m)}</Row>
                <Row label="Laser range">{meters(detail.laser.range_m)}</Row>
                <Row label="Calibration">{detail.laser.calibration_producer ?? "—"}</Row>
                <Row label="Laser dot">{pixels(detail.laser.dot)}</Row>
                {detail.laser.dot && <Row label="Laser color">{colorLabel(detail.laser.color)}</Row>}
                <Row label="Head">{pixels(detail.laser.head)}</Row>
                <Row label="Tail">{pixels(detail.laser.tail)}</Row>
              </>
            )}
            {detail.device_points && (
              <>
                <Row label="Snout">{pixels(detail.device_points.snout)}</Row>
                <Row label="Fork">{pixels(detail.device_points.fork)}</Row>
                <Row label="Points">{describeOrigin(detail.device_points.origin)}</Row>
              </>
            )}
            <Row label="fishsense-core">{detail.core_version ?? "—"}</Row>
          </dl>
        </section>

        <section className="rounded-card border border-line bg-surface px-6 py-5">
          <h2 className="mb-3 text-lg font-semibold">Measurement history</h2>
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="text-left text-fg-2">
                  <th className="py-1.5 pr-2 font-medium">When</th>
                  <th className="px-2 py-1.5 font-medium">Length</th>
                  <th className="py-1.5 pl-2 font-medium">How</th>
                </tr>
              </thead>
              <tbody>
                {evidence.history.map((h) => (
                  <tr key={h.key} className={`border-t border-line ${h.current ? "" : "text-fg-2"}`}>
                    <td className="py-2.5 pr-2">{new Date(h.when).toLocaleString()}</td>
                    <td className={`whitespace-nowrap px-2 py-2.5 tabular-nums ${h.current ? "font-semibold" : ""}`}>
                      {h.lengthM == null ? "—" : formatLength(h.lengthM)}
                    </td>
                    <td className="py-2.5 pl-2">
                      {h.how}
                      {h.current && (
                        <span className="ml-2 inline-flex h-5.5 items-center rounded-chip bg-teal-tint px-2 text-xs font-semibold text-teal-fg">
                          Current
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      </div>

      {phone && (
        <section className="mt-5 rounded-card border border-line bg-surface px-6 py-5">
          <h2 className="mb-3 text-lg font-semibold">Recorded with the capture</h2>
          <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-5 gap-y-2.5 text-sm">
            <Row label="Phone">{phone.device.model ?? phone.device.info ?? "—"}</Row>
            <Row label="App">{phone.app_version ?? "not recorded"}</Row>
            <Row label="Model">{phone.model_version ?? "—"}</Row>
            <Row label="Location">
              {phone.location ? (
                <a
                  className="text-teal-fg hover:underline"
                  href={osmLink(phone.location.latitude, phone.location.longitude)}
                >
                  {phone.location.latitude.toFixed(5)}, {phone.location.longitude.toFixed(5)}
                  {phone.location.horizontal_accuracy_m != null
                    ? ` (±${phone.location.horizontal_accuracy_m.toFixed(0)} m)`
                    : ""}
                </a>
              ) : (
                "—"
              )}
            </Row>
            <Row label="Orientation">{phone.orientation ?? "—"}</Row>
            <Row label="Depth">
              {phone.depth.width}×{phone.depth.height}, {phone.depth.row_bytes} bytes/row
            </Row>
            <Row label="Intrinsics (K)">
              <span className="font-mono text-[13px]">
                {phone.intrinsics.map((v) => v.toFixed(2)).join(", ")}
              </span>
            </Row>
          </dl>
        </section>
      )}

      {automatic && (
        <AutomaticSection
          frame={automatic}
          humanLengthM={o.length_m}
          reviewHref={o.mission?.number != null ? paths.frame(tenant, o.mission.number, capture) : null}
        />
      )}

      {evidence.files.length > 0 && (
        <section className="mt-5 rounded-card border border-line bg-surface px-6 py-5">
          <div className="mb-3 flex flex-wrap items-baseline gap-3">
            <h2 className="text-lg font-semibold">Files</h2>
            <span className="text-sm text-fg-2">
              Everything needed to reproduce this length, each with its checksum.
            </span>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="text-left text-fg-2">
                  <th className="py-1.5 pr-3 font-medium">File</th>
                  <th className="px-3 py-1.5 font-medium">Size</th>
                  <th className="py-1.5 pl-3 font-medium">Checksum</th>
                </tr>
              </thead>
              <tbody>
                {evidence.files.map((f) => (
                  <tr key={f.label} className="border-t border-line">
                    <td className="py-2.5 pr-3">
                      {f.url ? (
                        <a href={f.url} className="text-teal-fg hover:underline">
                          {f.label}
                        </a>
                      ) : (
                        f.label
                      )}
                    </td>
                    <td className="px-3 py-2.5 tabular-nums">{f.size}</td>
                    <td className="break-all py-2.5 pl-3 font-mono text-[13px] text-fg-2">
                      {f.checksum}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </main>
  );
}

/** Snout and fork (a Lite fish's head and tail) in the design's overlay
 *  colors, each ringed so it stands out on any photo, joined by a dashed
 *  line; and a Lite fish's laser dot, in red. */
function MeasurementOverlay({ overlay }: { overlay: Overlay }) {
  const r = overlay.width / 110;
  return (
    <svg
      viewBox={`0 0 ${overlay.width} ${overlay.height}`}
      preserveAspectRatio="none"
      className="absolute inset-0 h-full w-full"
      role="img"
      aria-label={`The measurement: ${overlay.names.snout.toLowerCase()} and ${overlay.names.fork.toLowerCase()} joined by a line${overlay.laser ? ", and the laser dot" : ""}`}
    >
      <line
        x1={overlay.snout.x}
        y1={overlay.snout.y}
        x2={overlay.fork.x}
        y2={overlay.fork.y}
        stroke="#FFFFFF"
        strokeWidth={overlay.width / 400}
        strokeDasharray={`${overlay.width / 120} ${overlay.width / 150}`}
      />
      <circle
        cx={overlay.snout.x}
        cy={overlay.snout.y}
        r={r}
        className="fill-snout stroke-ground"
        strokeWidth={r / 2.5}
      />
      <circle
        cx={overlay.fork.x}
        cy={overlay.fork.y}
        r={r}
        className="fill-fork stroke-ground"
        strokeWidth={r / 2.5}
      />
      {overlay.laser && (
        <circle
          cx={overlay.laser.x}
          cy={overlay.laser.y}
          r={r * 0.7}
          className="fill-laser stroke-ground"
          strokeWidth={r / 2.5}
        />
      )}
    </svg>
  );
}

function Length({ meters }: { meters: number | null | undefined }) {
  if (meters == null) return <>—</>;
  return (
    <>
      {millimeters(meters)}
      <span className="text-2xl font-medium text-fg-2"> mm</span>
    </>
  );
}

function Chip({ children }: { children: React.ReactNode }) {
  return (
    <span className="inline-flex h-7 items-center rounded-chip bg-raised px-2.5 text-sm font-medium">
      {children}
    </span>
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

function meters(value: number | null | undefined): string {
  return value == null ? "—" : `${value.toFixed(2)} m`;
}

function pixels(point: Point): string {
  return point ? `(${Math.round(point.x)}, ${Math.round(point.y)}) px` : "—";
}
