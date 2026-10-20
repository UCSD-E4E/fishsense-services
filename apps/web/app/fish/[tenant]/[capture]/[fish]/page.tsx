import Link from "next/link";
import { notFound } from "next/navigation";
import { describeOrigin, formatLength } from "@/lib/captures";
import { isSlug, isUuid } from "@/lib/ids";
import {
  fishName,
  formatError,
  getObservation,
  knownLengthError,
  methodLabel,
  where,
} from "@/lib/observations";
import { requireSignedIn } from "../../../../captures/guard";

export const dynamic = "force-dynamic";

type Props = {
  params: Promise<{ tenant: string; capture: string; fish: string }>;
};

type Point = { x: number; y: number } | null | undefined;

/**
 * One measured fish (PLAN.md §4.9, Phase 1 of §9.26): its length, what it
 * is, and the evidence its method rests on. A Lite fish's photo is its
 * frame's render, once rendered (Phase 4). The labelled points are listed,
 * not drawn: they were placed on v1's JPEG, not on this render.
 */
export default async function FishDetailPage({ params }: Props) {
  const { tenant, capture, fish } = await params;
  // A malformed address names no fish: 404, like an unknown one.
  if (!isSlug(tenant) || !isUuid(capture) || !isUuid(fish)) notFound();
  const session = await requireSignedIn(`/fish/${tenant}/${capture}/${fish}`);
  const detail = await getObservation(session.accessToken ?? "", tenant, capture, fish);
  if (!detail) notFound();

  const o = detail.observation;
  const name = fishName(o);
  const error = knownLengthError(o);
  const place = where(o);

  return (
    <main className="mx-auto max-w-4xl px-6 py-12">
      <header className="mb-8 flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">
            {formatLength(o.length_m)}
          </h1>
          <p className="mt-1 text-sm text-slate-600 dark:text-slate-400">
            {name.name}
            {name.detail ? ` · ${name.detail}` : ""} ·{" "}
            {new Date(o.captured_at).toLocaleString()}
            {place ? ` · ${place}` : ""}
          </p>
        </div>
        <Link
          href={`/fish?tenant=${encodeURIComponent(tenant)}`}
          className="rounded-md border border-slate-300 px-3 py-1.5 text-sm dark:border-slate-700"
        >
          All fish
        </Link>
      </header>

      {o.photo_url && (
        // Presigned, short-lived Garage URLs: not for next/image's optimizer.
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={o.photo_url}
          alt={name.name}
          className="mb-8 w-full rounded-lg border border-slate-200 dark:border-slate-800"
        />
      )}

      {error !== null && (
        <p className="mb-8 rounded-md border border-slate-200 bg-slate-50 p-3 text-sm dark:border-slate-800 dark:bg-slate-900">
          {o.fish_model?.name} is a fish model of known length{" "}
          {formatLength(o.fish_model?.known_length_m)}: this measurement is{" "}
          <strong>{formatError(error)}</strong>{" "}
          against it.
        </p>
      )}

      <section className="mb-8">
        <h2 className="mb-3 text-lg font-medium">
          How it was measured: {methodLabel(o.method)}
        </h2>
        <dl className="grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1 text-sm">
          {detail.laser && (
            <>
              <Row label="Laser depth" value={metres(detail.laser.depth_m)} />
              <Row label="Laser range" value={metres(detail.laser.range_m)} />
              <Row
                label="Calibration"
                value={detail.laser.calibration_producer ?? "—"}
              />
              <Row label="Laser dot" value={pixels(detail.laser.dot)} />
              <Row label="Head" value={pixels(detail.laser.head)} />
              <Row label="Tail" value={pixels(detail.laser.tail)} />
            </>
          )}
          {detail.device_points && (
            <>
              <Row label="Snout" value={pixels(detail.device_points.snout)} />
              <Row label="Fork" value={pixels(detail.device_points.fork)} />
              <Row
                label="Points"
                value={describeOrigin(detail.device_points.origin)}
              />
            </>
          )}
          <Row
            label="Algorithm"
            value={[detail.algorithm, detail.algorithm_version]
              .filter(Boolean)
              .join(" ") || "—"}
          />
          <Row label="fishsense-core" value={detail.core_version ?? "—"} />
          <Row
            label="Measured"
            value={new Date(detail.measured_at).toLocaleString()}
          />
        </dl>
      </section>

      <section className="mb-8">
        <h2 className="mb-3 text-lg font-medium">Recorded with</h2>
        <dl className="grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1 text-sm">
          <Row
            label="Device"
            value={[o.device.name, o.device.kind && `(${o.device.kind})`]
              .filter(Boolean)
              .join(" ") || "—"}
          />
          {o.mission && (
            <Row
              label="Mission"
              value={`${o.mission.name ?? "—"}${o.mission.number != null ? ` #${o.mission.number}` : ""}`}
            />
          )}
          {o.client_capture_uuid && (
            <Row
              label="Capture"
              value={
                <Link
                  className="underline"
                  href={`/captures/${tenant}/${o.client_capture_uuid}`}
                >
                  photo, depth and files
                </Link>
              }
            />
          )}
        </dl>
      </section>

      {!o.photo_url && (
        <p className="text-sm text-slate-500">
          {o.method === "laser"
            ? "This frame hasn't been rendered yet."
            : "No photo is available for this fish."}
        </p>
      )}
    </main>
  );
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <>
      <dt className="text-slate-500">{label}</dt>
      <dd>{value}</dd>
    </>
  );
}

function metres(value: number | null | undefined): string {
  return value == null ? "—" : `${value.toFixed(2)} m`;
}

function pixels(point: Point): string {
  return point ? `(${Math.round(point.x)}, ${Math.round(point.y)}) px` : "—";
}
