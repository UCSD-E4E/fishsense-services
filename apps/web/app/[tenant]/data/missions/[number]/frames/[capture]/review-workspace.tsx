"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState, useTransition } from "react";
import { ZoomFrame, type Marker } from "@/app/_review/zoom-frame";
import type { AutomaticFrame } from "@/lib/automatic";
import { colourLabel } from "@/lib/laser-colour";
import { maskFromRgba, snapToMask, type Pixel } from "@/lib/mask-snap";
import {
  KINDS,
  LEVEL_LABEL,
  acceptBody,
  agreeTarget,
  canConfirm,
  kindState,
  reasonLabel,
  reasonsFor,
  type Kind,
  type Recorded,
  type Review,
  type ReviewBody,
  type Skills,
} from "@/lib/reviews";
import { recordReviewAction } from "./actions";

/**
 * Reviewing a frame's automatic result (the owner, 2026-10-21; the API is
 * 0063). The frame page is review's home: the Labelling tab is to go away,
 * and its viewer is reused here (app/_review/zoom-frame.tsx).
 *
 * One kind at a time — the laser dot, the head and tail, or the species:
 * - accept sends the model's points as they are;
 * - fix places them. The dot goes exactly where it's put. The head and tail
 *   snap to the mask, as the app's do (lib/mask-snap.ts), and the reviewer
 *   sees where each went before saving; the drop points go with the review;
 * - reject gives a reason.
 * A Trusted or Expert reviewer can confirm a learner's waiting review. A
 * learner's own review backs the latest waiting one by someone else, and
 * counts with it if they agree. The API makes every rule and says why when it
 * refuses; that reason is shown as it comes.
 *
 * Keys: D, H, S choose the dot, the head and tail, or the species; A accepts,
 * F fixes, X rejects (then 1-4 for the reason); in a fix, Tab switches between
 * head and tail, Enter saves and Esc stops; C confirms the latest waiting
 * review; N goes to the next frame.
 */

const COLOUR = { laser: "#E51717", head: "#2563EB", tail: "#FFB547" };
const button =
  "inline-flex h-9 items-center rounded-control bg-teal px-3.5 text-sm font-semibold text-on-teal disabled:opacity-50";
const quiet =
  "inline-flex h-9 items-center rounded-control border border-line bg-surface px-3 text-sm font-medium disabled:opacity-50";

type Points = { point?: Pixel | null; head?: Pixel | null; tail?: Pixel | null };
type Draft = Points & {
  raw: { point?: Pixel; head?: Pixel; tail?: Pixel };
  active: "point" | "head" | "tail";
  species: string;
};
type LoadedMask = { mask: Uint8Array; width: number; height: number };
type Note = { tone: "ok" | "error"; text: string };

export function ReviewWorkspace({
  tenant,
  captureId,
  framePath,
  frame,
  reviews,
  skills,
  readOnly,
  maskSrc,
  nextHref,
}: {
  tenant: string;
  captureId: string;
  framePath: string;
  frame: AutomaticFrame;
  reviews: Review[];
  skills: Skills;
  readOnly: boolean;
  /** This server's copy of the mask (app/api/masks), or null without one. */
  maskSrc: string | null;
  nextHref: string | null;
}) {
  const router = useRouter();
  const [kind, setKind] = useState<Kind>("head_tail");
  const [mode, setMode] = useState<"view" | "fix" | "reject">("view");
  const [draft, setDraft] = useState<Draft | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [note, setNote] = useState<Note | null>(null);
  const [pending, startTransition] = useTransition();
  const retainedScaleRef = useRef<number | null>(null);
  const maskRef = useRef<Promise<LoadedMask | null> | null>(null);

  const level = skills[kind];
  const state = kindState(reviews, kind);
  const previewed = reviews.find((r) => r.review_id === preview) ?? null;
  const hasRender = Boolean(frame.render_url && frame.render_width && frame.render_height && frame.render_id);

  const shown: Points =
    mode === "fix" && draft
      ? draft
      : previewed
        ? { point: previewed.point, head: previewed.head, tail: previewed.tail }
        : { point: frame.laser, head: frame.head, tail: frame.tail };
  const caption =
    mode === "fix"
      ? "Your fix: not saved yet"
      : previewed
        ? `${previewed.mine ? "Your" : `A ${LEVEL_LABEL[previewed.level]} reviewer's`} review`
        : "The automatic result";

  function loadMask(): Promise<LoadedMask | null> {
    if (!maskSrc) return Promise.resolve(null);
    maskRef.current ??= decodeMask(maskSrc);
    return maskRef.current;
  }

  function submit(body: ReviewBody) {
    setNote(null);
    startTransition(async () => {
      const result = await recordReviewAction(tenant, captureId, framePath, body);
      if (!result.ok) {
        setNote({ tone: "error", text: result.error });
        return;
      }
      setNote({ tone: "ok", text: outcome(result.recorded) });
      setMode("view");
      setDraft(null);
      setPreview(null);
      router.refresh();
    });
  }

  const backing = agreeTarget(level, state.waiting);
  const base = { kind, automatic_head_tail_prediction_id: frame.prediction_id };

  function accept() {
    const body = acceptBody(kind, frame);
    if (!body) {
      setNote({ tone: "error", text: "There's nothing of this kind to accept on this frame" });
      return;
    }
    submit({ ...body, confirms_review_id: backing });
  }

  async function startFix() {
    setPreview(null);
    if (kind !== "species" && !hasRender) {
      setNote({ tone: "error", text: "The render this result is on isn't available, so points can't be placed" });
      return;
    }
    if (kind === "head_tail" && !(await loadMask())) {
      setNote({
        tone: "error",
        text: "This frame has no mask yet, so the head and tail can't snap to the fish as the app's do. Accept or reject it instead.",
      });
      return;
    }
    setNote(null);
    setDraft({
      point: frame.laser,
      head: frame.head,
      tail: frame.tail,
      raw: {},
      active: kind === "laser" ? "point" : "head",
      species: frame.species ?? "",
    });
    setMode("fix");
  }

  function saveFix() {
    if (!draft) return;
    if (kind === "species") {
      if (!draft.species.trim()) return setNote({ tone: "error", text: "Name the species" });
      return submit({ ...base, action: "fix", scientific_name: draft.species.trim(), confirms_review_id: backing });
    }
    if (kind === "laser") {
      if (!draft.point) return setNote({ tone: "error", text: "Place the dot" });
      return submit({
        ...base,
        action: "fix",
        render_id: frame.render_id,
        point: draft.point,
        raw: Object.keys(draft.raw).length ? draft.raw : null,
        confirms_review_id: backing,
      });
    }
    if (!draft.head || !draft.tail) return setNote({ tone: "error", text: "Place both the head and the tail" });
    submit({
      ...base,
      action: "fix",
      render_id: frame.render_id,
      head: draft.head,
      tail: draft.tail,
      raw: Object.keys(draft.raw).length ? draft.raw : null,
      confirms_review_id: backing,
    });
  }

  function reject(reason: ReviewBody["reason"]) {
    submit({ ...base, action: "reject", reason, confirms_review_id: backing });
  }

  function confirm(target: Review) {
    submit({ kind, action: "confirm", confirms_review_id: target.review_id });
  }

  async function place(p: Pixel) {
    if (mode !== "fix" || !draft) return;
    if (kind === "laser") {
      setDraft({ ...draft, point: p, raw: { ...draft.raw, point: p } });
      return;
    }
    if (kind !== "head_tail") return;
    const mask = await loadMask();
    const snapped = mask && snapToMask({ ...mask, x: p.x, y: p.y });
    if (!snapped) {
      setNote({ tone: "error", text: "No fish within 256 pixels of that point: drop it closer to the fish" });
      return;
    }
    setNote(null);
    const which = draft.active === "tail" ? "tail" : "head";
    setDraft({
      ...draft,
      [which]: snapped,
      raw: { ...draft.raw, [which]: p },
      active: which === "head" ? "tail" : "head",
    });
  }

  function chooseKind(k: Kind) {
    setKind(k);
    setMode("view");
    setDraft(null);
    setPreview(null);
    setNote(null);
  }

  const reasons = reasonsFor(kind);
  const confirmable = state.waiting.filter((r) => canConfirm(level, r));

  // --- keys ------------------------------------------------------------------
  // Subscribed after every render, so the handler always sees this render's
  // state (the compiler memoizes the rest).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === "INPUT" || t.tagName === "SELECT" || t.tagName === "TEXTAREA")) {
        if (e.key === "Escape") t.blur();
        return;
      }
      if (e.metaKey || e.ctrlKey || e.altKey || pending) return;
      const key = e.key.toLowerCase();
      if (key === "n" && nextHref) return router.push(nextHref);
      if (key === "d") return chooseKind("laser");
      if (key === "h") return chooseKind("head_tail");
      if (key === "s") return chooseKind("species");
      if (key === "escape") return (setMode("view"), setDraft(null));
      if (readOnly) return;
      if (mode === "fix") {
        if (key === "enter") return saveFix();
        if (e.key === "Tab" && kind === "head_tail" && draft) {
          e.preventDefault();
          return setDraft({ ...draft, active: draft.active === "head" ? "tail" : "head" });
        }
        return;
      }
      if (mode === "reject" && /^[1-9]$/.test(key)) {
        const r = reasons[Number(key) - 1];
        if (r) reject(r.value);
        return;
      }
      if (key === "a") return accept();
      if (key === "f") return void startFix();
      if (key === "x") return setMode("reject");
      if (key === "c" && confirmable.length) return confirm(confirmable[confirmable.length - 1]);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  // --- drawing ---------------------------------------------------------------
  const markers: Marker[] = [];
  if (kind !== "species" || mode !== "fix") {
    if (shown.point) markers.push({ ...shown.point, colour: COLOUR.laser });
    if (shown.head) markers.push({ ...shown.head, colour: COLOUR.head });
    if (shown.tail) markers.push({ ...shown.tail, colour: COLOUR.tail });
  }
  if (mode === "fix" && draft) {
    for (const [which, p] of Object.entries(draft.raw)) {
      const at = draft[which as keyof Points];
      if (p && at && (p.x !== at.x || p.y !== at.y)) {
        markers.push({ ...p, colour: COLOUR[which === "point" ? "laser" : (which as "head" | "tail")], faint: true });
      }
    }
  }
  const width = frame.render_width ?? 0;
  const height = frame.render_height ?? 0;
  const layers = (
    <>
      <defs>
        {/* The mask is greyscale, 255 on the fish: its brightness becomes teal's opacity. */}
        <filter id="review-mask" colorInterpolationFilters="sRGB">
          <feColorMatrix type="matrix" values="0 0 0 0 0  0 0 0 0 0  0 0 0 0 0  1 0 0 0 0" result="alpha" />
          <feFlood floodColor="#00AAA5" />
          <feComposite in2="alpha" operator="in" />
        </filter>
      </defs>
      {maskSrc && <image href={maskSrc} width={width} height={height} filter="url(#review-mask)" opacity={0.35} />}
      {shown.head && shown.tail && (
        <line
          x1={shown.head.x}
          y1={shown.head.y}
          x2={shown.tail.x}
          y2={shown.tail.y}
          stroke="#FFFFFF"
          // Counter-scaled like the markers: the zoom is a CSS transform,
          // which `non-scaling-stroke` doesn't see.
          style={{
            strokeWidth: "calc(1.5px * var(--inv, 1))",
            strokeDasharray: "calc(6px * var(--inv, 1)) calc(5px * var(--inv, 1))",
          }}
        />
      )}
    </>
  );
  const focus = frame.head ?? frame.laser;

  return (
    <div className="min-w-0">
      {hasRender ? (
        <ZoomFrame
          src={frame.render_url!}
          width={width}
          height={height}
          alt="The frame the automatic run saw"
          focus={focus ? { xPercent: (focus.x / width) * 100, yPercent: (focus.y / height) * 100 } : null}
          markers={markers}
          layers={layers}
          retainedScaleRef={retainedScaleRef}
          onPlace={mode === "fix" && kind !== "species" ? (p) => void place(p) : undefined}
        />
      ) : (
        <div className="flex aspect-4/3 items-center justify-center rounded-card border border-dashed border-line bg-raised text-sm text-fg-2">
          The render this result was made on isn&apos;t available
        </div>
      )}
      <p className="mt-2 flex flex-wrap items-center gap-4 text-sm text-fg-2">
        <span className="font-medium text-fg">{caption}</span>
        <Key colour={COLOUR.head}>Head</Key>
        <Key colour={COLOUR.tail}>Tail</Key>
        <Key colour={COLOUR.laser}>Laser dot</Key>
        {maskSrc && <Key colour="#00AAA5">Fish mask</Key>}
        <span className="text-xs">Scroll or pinch to zoom; drag to pan</span>
      </p>

      <div className="mt-4 rounded-card border border-line bg-raised p-4">
        <div role="tablist" aria-label="What to review" className="mb-3 flex flex-wrap gap-2">
          {KINDS.map(({ kind: k, label }) => {
            const s = kindState(reviews, k);
            return (
              <button
                key={k}
                role="tab"
                aria-selected={k === kind}
                onClick={() => chooseKind(k)}
                className={`rounded-control border px-3 py-1.5 text-left text-sm ${k === kind ? "border-teal bg-surface font-semibold" : "border-line"}`}
              >
                {label}
                <span className="block text-xs font-normal text-fg-2">{standing(s)}</span>
              </button>
            );
          })}
        </div>

        {kind === "laser" && frame.laser && (
          <p className="mb-3 text-sm text-fg-2">
            The model read this dot&apos;s colour as{" "}
            <strong className="text-fg">{colourLabel(frame.laser_color, frame.laser_color_margin, frame.laser_color_source)}</strong>.
          </p>
        )}

        <p className="mb-3 text-sm text-fg-2">
          You&apos;re <strong className="text-fg">{LEVEL_LABEL[level]}</strong> at this.{" "}
          {level === "learning"
            ? "Your review waits until a Trusted reviewer confirms it, or another learner agrees."
            : "Your review counts at once, and becomes the frame's label."}
        </p>

        {readOnly ? (
          <p className="text-sm text-fg-2">You&apos;re viewing this tenant read-only: unlock it to review.</p>
        ) : mode === "fix" && draft ? (
          <div className="flex flex-wrap items-center gap-2">
            {kind === "species" ? (
              <>
                <input
                  autoFocus
                  list="species-guesses"
                  className="h-9 min-w-72 flex-1 rounded-control border border-line bg-surface px-2 text-sm"
                  value={draft.species}
                  onChange={(e) => setDraft({ ...draft, species: e.target.value })}
                  onKeyDown={(e) => e.key === "Enter" && saveFix()}
                  aria-label="Species"
                />
                <datalist id="species-guesses">
                  {frame.species_top5.map((g, i) => (
                    <option key={i} value={String(g.choice ?? "")} />
                  ))}
                </datalist>
              </>
            ) : kind === "head_tail" ? (
              <span className="text-sm">
                Click to place the{" "}
                <strong style={{ color: COLOUR[draft.active === "tail" ? "tail" : "head"] }}>
                  {draft.active === "tail" ? "tail" : "head"}
                </strong>
                ; it snaps to the fish. Tab switches.
              </span>
            ) : (
              <span className="text-sm">Click to place the dot; it stays exactly where you put it.</span>
            )}
            <button className={button} disabled={pending} onClick={saveFix}>
              Save fix
            </button>
            <button className={quiet} disabled={pending} onClick={() => (setMode("view"), setDraft(null))}>
              Cancel
            </button>
          </div>
        ) : mode === "reject" ? (
          <div className="flex flex-wrap items-center gap-2">
            {reasons.map((r, i) => (
              <button key={r.value} className={quiet} disabled={pending} onClick={() => reject(r.value)}>
                <span className="mr-1.5 font-mono text-xs text-fg-2">{i + 1}</span>
                {r.label}
              </button>
            ))}
            <button className={quiet} disabled={pending} onClick={() => setMode("view")}>
              Cancel
            </button>
          </div>
        ) : (
          <div className="flex flex-wrap items-center gap-2">
            <button className={button} disabled={pending || !acceptBody(kind, frame)} onClick={accept}>
              Accept <kbd className="ml-1.5 text-xs opacity-75">A</kbd>
            </button>
            <button className={quiet} disabled={pending} onClick={() => void startFix()}>
              Fix <kbd className="ml-1.5 text-xs text-fg-2">F</kbd>
            </button>
            <button className={quiet} disabled={pending} onClick={() => setMode("reject")}>
              Reject <kbd className="ml-1.5 text-xs text-fg-2">X</kbd>
            </button>
            {backing && (
              <span className="text-xs text-fg-2">Your review backs the waiting one if you agree with it.</span>
            )}
          </div>
        )}

        {note && (
          <p
            role="status"
            className={`mt-3 rounded-control border px-3 py-2 text-sm ${note.tone === "error" ? "border-warn text-warn" : "border-line text-fg"}`}
          >
            {note.text}
          </p>
        )}

        {(state.decided || state.waiting.length > 0) && (
          <ul className="mt-4 space-y-2 text-sm">
            {state.decided && (
              <ReviewRow review={state.decided} label="Counts" previewing={preview} onPreview={setPreview} />
            )}
            {state.waiting.map((r) => (
              <ReviewRow key={r.review_id} review={r} label="Waiting" previewing={preview} onPreview={setPreview}>
                {!readOnly && canConfirm(level, r) && (
                  <button className={quiet} disabled={pending} onClick={() => confirm(r)}>
                    Confirm <kbd className="ml-1.5 text-xs text-fg-2">C</kbd>
                  </button>
                )}
              </ReviewRow>
            ))}
          </ul>
        )}
      </div>

      <div className="mt-3 flex justify-end">
        {nextHref ? (
          <Link href={nextHref} className="text-sm font-medium text-teal-fg hover:underline">
            Next frame <kbd className="ml-1 text-xs text-fg-2">N</kbd> →
          </Link>
        ) : null}
      </div>
    </div>
  );
}

function ReviewRow({
  review,
  label,
  previewing,
  onPreview,
  children,
}: {
  review: Review;
  label: string;
  previewing: string | null;
  onPreview: (id: string | null) => void;
  children?: React.ReactNode;
}) {
  const showing = previewing === review.review_id;
  const hasPoints = Boolean(review.point || review.head);
  return (
    <li className="flex flex-wrap items-center gap-2">
      <span className="inline-flex h-6 items-center rounded-chip bg-surface px-2 text-xs font-semibold">{label}</span>
      <span>
        {describe(review)}
        <span className="text-fg-2">
          {" "}
          · {review.mine ? "you" : `a ${LEVEL_LABEL[review.level]} reviewer`},{" "}
          {new Date(review.created_at).toLocaleDateString()}
        </span>
      </span>
      {hasPoints && (
        <button
          className="text-sm text-teal-fg hover:underline"
          onClick={() => onPreview(showing ? null : review.review_id)}
        >
          {showing ? "Show the automatic result" : "Show its points"}
        </button>
      )}
      {children}
    </li>
  );
}

function Key({ colour, children }: { colour: string; children: React.ReactNode }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="size-2.5 rounded-full" style={{ background: colour }} aria-hidden />
      {children}
    </span>
  );
}

function describe(r: Review): string {
  if (r.action === "accept") return "Accepted";
  if (r.action === "reject") return `Rejected: ${reasonLabel(r.reason)}`;
  if (r.action === "confirm") return "Confirmed";
  return r.scientific_name ? `Fixed: ${r.scientific_name}` : "Fixed";
}

function standing(s: ReturnType<typeof kindState>): string {
  if (s.decided) return `${describe(s.decided)}${s.waiting.length ? ` · ${s.waiting.length} waiting` : ""}`;
  return s.waiting.length ? `${s.waiting.length} waiting` : "Not reviewed";
}

function outcome(r: Recorded): string {
  if (r.label_id) return "Saved. It counts, and is the frame's label now.";
  if (r.counts) return "Saved. It counts.";
  return "Saved. It waits for a Trusted reviewer's confirm, or another learner who agrees.";
}

/** The mask's pixels, decoded in the browser as they were stored: one grey
 *  byte per pixel, nonzero is fish. */
async function decodeMask(src: string): Promise<LoadedMask | null> {
  try {
    const response = await fetch(src);
    if (!response.ok) return null;
    const bitmap = await createImageBitmap(await response.blob(), {
      colorSpaceConversion: "none",
      premultiplyAlpha: "none",
    });
    const canvas = document.createElement("canvas");
    canvas.width = bitmap.width;
    canvas.height = bitmap.height;
    const context = canvas.getContext("2d", { willReadFrequently: true });
    if (!context) return null;
    context.drawImage(bitmap, 0, 0);
    const { data } = context.getImageData(0, 0, bitmap.width, bitmap.height);
    return { mask: maskFromRgba(data), width: bitmap.width, height: bitmap.height };
  } catch {
    return null;
  }
}
