import Link from "next/link";
import { formatLength } from "@/lib/captures";
import { fishName, observationHref, type Observation } from "@/lib/observations";

/** Fish as cards: photo, name and length, each linking to its page. Today's
 *  recent fish, and a mission's (phase 7b). */
export function FishGrid({ slug, fish }: { slug: string; fish: Observation[] }) {
  return (
    <ul className="grid gap-4 [grid-template-columns:repeat(auto-fill,minmax(200px,1fr))]">
      {fish.map((o) => (
        <li key={`${o.capture_id}/${o.fish_id}`}>
          <Link
            href={observationHref(slug, o)}
            className="flex h-full flex-col overflow-hidden rounded-card border border-line bg-surface hover:border-fg-3"
          >
            {o.photo_url ? (
              // Presigned, short-lived Garage URLs: not for next/image's optimizer.
              // eslint-disable-next-line @next/next/no-img-element
              <img src={o.photo_url} alt={fishName(o).name} loading="lazy" className="aspect-4/3 w-full object-cover" />
            ) : (
              <div className="flex aspect-4/3 items-center justify-center border-b border-dashed border-line bg-raised text-sm text-fg-2">
                No photo yet
              </div>
            )}
            <div className="flex items-baseline justify-between gap-2 px-3.5 py-3">
              <span className="text-sm font-semibold">{fishName(o).name}</span>
              <span className="whitespace-nowrap font-semibold tabular-nums">
                {o.length_m == null ? "—" : formatLength(o.length_m)}
              </span>
            </div>
          </Link>
        </li>
      ))}
    </ul>
  );
}
