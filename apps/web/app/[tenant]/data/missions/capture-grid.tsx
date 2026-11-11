import Link from "next/link";
import { formatLength } from "@/lib/captures";
import { captureCardLink, type CaptureCard } from "@/lib/mission-captures";
import { phoneCardLabel } from "@/lib/phone-measurement";

/** A mission's captures as cards, measured or not: the photo, then its
 *  length (linking to the fish), the server's unvalidated phone length or why
 *  it has none (2026-11-10), or "Not measured" (each linking to the frame). */
export function CaptureGrid({
  slug,
  mission,
  captures,
}: {
  slug: string;
  mission: number;
  captures: CaptureCard[];
}) {
  return (
    <ul className="grid gap-4 [grid-template-columns:repeat(auto-fill,minmax(200px,1fr))]">
      {captures.map((c) => {
        const href = captureCardLink(slug, c, mission);
        const body = (
          <>
            {c.photo_url ? (
              // Presigned, short-lived Garage URLs: not for next/image's optimizer.
              // eslint-disable-next-line @next/next/no-img-element
              <img src={c.photo_url} alt="" loading="lazy" className="aspect-4/3 w-full object-cover" />
            ) : (
              <div className="flex aspect-4/3 items-center justify-center border-b border-dashed border-line bg-raised text-sm text-fg-2">
                No photo yet
              </div>
            )}
            <div className="flex items-baseline justify-between gap-2 px-3.5 py-3">
              <span className="text-xs text-fg-2">{new Date(c.captured_at).toLocaleTimeString()}</span>
              {c.fish.length > 0 ? (
                <span className="whitespace-nowrap font-semibold tabular-nums">
                  {c.fish.map((f) => (f.length_m == null ? "—" : formatLength(f.length_m))).join(", ")}
                </span>
              ) : phoneCardLabel(c.phone) ? (
                <PhoneLabel label={phoneCardLabel(c.phone)!} />
              ) : (
                <span className="text-sm text-fg-2">Not measured</span>
              )}
            </div>
          </>
        );
        const card = "flex h-full flex-col overflow-hidden rounded-card border border-line bg-surface";
        return (
          <li key={c.capture_id}>
            {href ? (
              <Link href={href} className={`${card} hover:border-fg-3`}>
                {body}
              </Link>
            ) : (
              <div className={card}>{body}</div>
            )}
          </li>
        );
      })}
    </ul>
  );
}

/** The server's phone result on a card: a length, marked unvalidated, or why
 *  there is none. Never a measured fish's length. */
function PhoneLabel({ label }: { label: { text: string; unvalidated: boolean } }) {
  return label.unvalidated ? (
    <span className="flex items-baseline gap-1.5 whitespace-nowrap">
      <span className="font-semibold tabular-nums">{label.text}</span>
      <span className="text-xs font-semibold text-warn">unvalidated</span>
    </span>
  ) : (
    <span className="text-sm text-fg-2">{label.text}</span>
  );
}
