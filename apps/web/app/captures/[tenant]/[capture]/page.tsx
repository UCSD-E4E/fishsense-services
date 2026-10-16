import Link from "next/link";
import { notFound } from "next/navigation";
import {
  describeOrigin,
  formatLength,
  getCapture,
  measurementOverlay,
  osmLink,
  placeName,
} from "@/lib/captures";
import { requireSignedIn } from "../../guard";

export const dynamic = "force-dynamic";

type Props = { params: Promise<{ tenant: string; capture: string }> };

const FILE_LABELS: Record<string, string> = {
  rgb_jpeg: "Photo (JPEG)",
  rgb_lossless: "Frame the phone measured on (lossless)",
  depth_f32: "LiDAR depth (float32, metres)",
  confidence_u8: "Depth confidence (uint8)",
  mask_u8: "Fish mask (uint8)",
};

/**
 * One FishSense Mobile capture: the photo with the phone's snout and fork
 * points, every measurement version, everything recorded with it, and every
 * file it synced (with its SHA-256), for reproducing the measurement.
 */
export default async function CapturePage({ params }: Props) {
  const { tenant, capture: captureId } = await params;
  const session = await requireSignedIn(`/captures/${tenant}/${captureId}`);
  const capture = await getCapture(
    session.accessToken ?? "",
    tenant,
    captureId,
  ).catch((error: unknown) => {
    // An id that can't be one is "not found", not a crash.
    if (error instanceof Error && /^Invalid /.test(error.message)) return null;
    throw error;
  });
  if (!capture) notFound();

  const current = capture.current_measurement;
  const overlay = measurementOverlay(capture, current);
  const photo = capture.files.find((f) => f.role === "rgb_jpeg");
  const place = placeName({
    place_name: capture.location?.place_name,
    derived_place_name: capture.derived_place_name,
  });

  return (
    <main className="mx-auto max-w-5xl px-6 py-12">
      <header className="mb-6 flex items-start justify-between gap-4">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">
            {formatLength(current?.length_m)}
          </h1>
          <p className="mt-1 text-sm text-slate-600 dark:text-slate-400">
            {new Date(capture.captured_at).toLocaleString()}
            {place
              ? ` · ${place.name}${place.derived ? " (from coordinates)" : ""}`
              : ""}
          </p>
        </div>
        <Link
          href={`/captures?tenant=${encodeURIComponent(tenant)}`}
          className="shrink-0 rounded-md border border-slate-300 px-3 py-1 text-sm dark:border-slate-700"
        >
          All captures
        </Link>
      </header>

      {photo?.url && (
        <figure className="relative mb-8 overflow-hidden rounded-lg border border-slate-200 dark:border-slate-800">
          {/* Presigned Garage URL: not for next/image (it would outlive it). */}
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src={photo.url} alt="Fish capture" className="block w-full" />
          {overlay && (
            <svg
              viewBox={`0 0 ${overlay.width} ${overlay.height}`}
              preserveAspectRatio="none"
              className="absolute inset-0 h-full w-full"
              aria-label="Snout and fork points"
            >
              <line
                x1={overlay.snout.x}
                y1={overlay.snout.y}
                x2={overlay.fork.x}
                y2={overlay.fork.y}
                stroke="#38bdf8"
                strokeWidth={overlay.width / 300}
              />
              <circle
                cx={overlay.snout.x}
                cy={overlay.snout.y}
                r={overlay.width / 120}
                fill="#f97316"
              />
              <circle
                cx={overlay.fork.x}
                cy={overlay.fork.y}
                r={overlay.width / 120}
                fill="#22c55e"
              />
            </svg>
          )}
          {overlay && (
            <figcaption className="px-3 py-2 text-xs text-slate-500">
              Orange: snout. Green: fork. Points{" "}
              {current ? describeOrigin(current.origin) : ""}.
            </figcaption>
          )}
        </figure>
      )}

      <section className="mb-8">
        <h2 className="mb-2 text-lg font-medium">Measurements</h2>
        {capture.measurements.length === 0 ? (
          <p className="text-sm text-slate-500">
            The phone did not measure this fish.
          </p>
        ) : (
          <table className="w-full text-left text-sm">
            <thead className="text-slate-500">
              <tr>
                <th className="py-1">When</th>
                <th>Length</th>
                <th>Points</th>
                <th>Algorithm</th>
              </tr>
            </thead>
            <tbody>
              {capture.measurements.map((m, i) => (
                <tr
                  key={m.client_measurement_uuid}
                  className="border-t border-slate-200 dark:border-slate-800"
                >
                  <td className="py-1">
                    {new Date(m.measured_at).toLocaleString()}
                    {i === capture.measurements.length - 1 ? " (current)" : ""}
                  </td>
                  <td>{formatLength(m.length_m)}</td>
                  <td>{describeOrigin(m.origin)}</td>
                  <td className="text-slate-500">
                    {m.algorithm
                      ? `${m.algorithm} ${m.algorithm_version ?? ""}`
                      : "not recorded"}
                    {m.model_version ? ` · ${m.model_version}` : ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="mb-8 grid gap-x-8 gap-y-1 text-sm sm:grid-cols-2">
        <h2 className="mb-1 text-lg font-medium sm:col-span-2">
          Recorded with the capture
        </h2>
        <Field label="Device">
          {capture.device.model ?? capture.device.info ?? "—"}
        </Field>
        <Field label="App">{capture.app_version ?? "not recorded"}</Field>
        <Field label="fishsense-core">{capture.core_version ?? "—"}</Field>
        <Field label="Model">{capture.model_version ?? "—"}</Field>
        <Field label="Location">
          {capture.location ? (
            <a
              className="underline"
              href={osmLink(
                capture.location.latitude,
                capture.location.longitude,
              )}
            >
              {capture.location.latitude.toFixed(5)},{" "}
              {capture.location.longitude.toFixed(5)}
              {capture.location.horizontal_accuracy_m != null
                ? ` (±${capture.location.horizontal_accuracy_m.toFixed(0)} m)`
                : ""}
            </a>
          ) : (
            "—"
          )}
        </Field>
        <Field label="Orientation">{capture.orientation ?? "—"}</Field>
        <Field label="Depth">
          {capture.depth.width}×{capture.depth.height},{" "}
          {capture.depth.row_bytes} bytes/row
        </Field>
        <Field label="Intrinsics (K)">
          {capture.intrinsics.map((v) => v.toFixed(2)).join(", ")}
        </Field>
      </section>

      <section>
        <h2 className="mb-2 text-lg font-medium">Files</h2>
        <ul className="space-y-1 text-sm">
          {capture.files.map((f) => (
            <li key={f.role}>
              {f.url ? (
                <a className="underline" href={f.url}>
                  {FILE_LABELS[f.role] ?? f.role}
                </a>
              ) : (
                (FILE_LABELS[f.role] ?? f.role)
              )}{" "}
              <span className="text-slate-500">
                {(f.size / 1024).toFixed(0)} KiB · sha256{" "}
                {f.sha256.slice(0, 12)}…
              </span>
            </li>
          ))}
        </ul>
      </section>
    </main>
  );
}

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex gap-2">
      <span className="w-32 shrink-0 text-slate-500">{label}</span>
      <span>{children}</span>
    </div>
  );
}
