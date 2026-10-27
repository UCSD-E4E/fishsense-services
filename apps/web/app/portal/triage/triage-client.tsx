"use client";

// Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/app/portal/triage/triage-client.tsx.

import { useCallback, useEffect, useRef, useState } from "react";
import type { ProjectOutcome, TriageItem } from "@/lib/triage-queue";
import { acceptAction, undoAcceptAction } from "./actions";
import { ZoomFrame } from "@/app/_review/zoom-frame";


type Props = {
  items: TriageItem[];
  kindLabel: string;
  /** What each walked project contributed — see the empty state. */
  scanned: number;
  projects: ProjectOutcome[];
  notWalked: number;
};

export function TriageClient({ items, kindLabel, scanned, projects, notWalked }: Props) {
  const [index, setIndex] = useState(0);
  const [accepted, setAccepted] = useState(0);
  const [skipped, setSkipped] = useState(0);
  const [message, setMessage] = useState<string | null>(null);
  const [undo, setUndo] = useState<{ taskId: number; annotationId: number } | null>(null);
  /**
   * Frames whose accept failed, re-queued at the end.
   *
   * Accept advances optimistically so the labeler is never waiting on the
   * network — but a failure then dropped the frame entirely, and the
   * judgment they had already made was lost with no way to retry it. Putting
   * it back is the difference between a retryable hiccup and silently
   * discarded work.
   */
  const [requeued, setRequeued] = useState<TriageItem[]>([]);

  /**
   * Magnification carried between frames.
   *
   * It lives here rather than in the viewer because the viewer is remounted
   * per task (`key`), which is what resets the loaded-image state — so
   * anything it holds is gone by the next frame. Deep zoom is most of the work
   * at 120x, and re-pinching it for every judgment was the difference the
   * labeler noticed against the Android app.
   */
  const retainedScaleRef = useRef<number | null>(null);

  const queue = [...items, ...requeued];
  const item = queue[index] ?? null;
  const startedAt = useRef(0);

  useEffect(() => {
    startedAt.current = Date.now();
  }, [index]);

  const advance = useCallback(() => {
    setIndex((i) => i + 1);
    setMessage(null);
  }, []);

  async function onAccept() {
    if (!item) return;
    const leadTime = Date.now() - startedAt.current;
    const current = item;
    setAccepted((n) => n + 1);
    advance();

    const res = await acceptAction(current.taskId, current.result, leadTime);
    if (res.ok) {
      // Undo stays available after the queue has moved on — a mis-tap is
      // usually noticed a beat later, once the next frame is up.
      setUndo({ taskId: current.taskId, annotationId: res.annotationId });
    } else {
      // Must roll the count back: without it a failed accept still read as
      // accepted, which is the number a labeler trusts.
      setAccepted((n) => n - 1);
      setRequeued((q) => (q.some((t) => t.taskId === current.taskId) ? q : [...q, current]));
      setMessage(
        `Accept failed for task ${current.taskId} — put back at the end of the queue. ${res.error}`,
      );
    }
  }

  function onSkip() {
    if (!item) return;
    // Client-side only. A Label Studio "skip" writes a canceled annotation
    // with an empty result, which flips `completed` with no coordinates and
    // makes the populate step exclude the image forever.
    setSkipped((n) => n + 1);
    advance();
  }

  async function onUndo() {
    if (!undo) return;
    const res = await undoAcceptAction(undo.annotationId);
    setUndo(null);
    if (res.ok) {
      setAccepted((n) => Math.max(0, n - 1));
      setMessage(`Took back task ${undo.taskId}`);
    } else {
      setMessage(res.error);
    }
  }

  if (!item) {
    return (
      <div className="flex w-full flex-col gap-3 rounded-md border border-slate-200 p-6 dark:border-slate-800">
        <p className="text-sm text-slate-700 dark:text-slate-300">
          Nothing left to triage in the {kindLabel} queue.
        </p>

        {/* An empty queue and a broken one looked identical, which cost real
            time to tell apart more than once. `loadQueue` already knew all of
            this and the page was throwing it away. */}
        <p className="text-xs text-slate-600 dark:text-slate-400">
          Walked {scanned} {scanned === 1 ? "project" : "projects"}.{" "}
          {scanned === 0
            ? "No projects were offered — either Label Studio is switched off, or the auto-accept gate has not finished sweeping any dive yet."
            : "Every task in them was already handled."}
        </p>

        {projects.length > 0 && (
          <details className="text-xs text-slate-600 dark:text-slate-400" open>
            <summary className="cursor-pointer">Per project</summary>
            <ul className="mt-2 space-y-1 font-mono text-[11px] text-slate-600 dark:text-slate-400">
              {projects.map((p) => (
                <li key={p.projectId}>
                  <span className="font-medium text-slate-900 dark:text-slate-200">{p.projectId}</span>
                  {p.title ? ` ${p.title}` : ""}
                  {" — "}
                  {p.error
                    ? p.error
                    : p.tasks === 0
                      ? "0 tasks on page 1"
                      : `${p.tasks} tasks, ${p.taken} taken` +
                        (Object.keys(p.reasons).length > 0
                          ? ` (${Object.entries(p.reasons)
                              .map(([reason, n]) => `${n} ${reason}`)
                              .join(", ")})`
                          : "")}
                </li>
              ))}
            </ul>
            {notWalked > 0 && (
              <p className="mt-2 text-[11px] text-slate-500 dark:text-slate-500">
                {notWalked} more offered but not reached this load.
              </p>
            )}
          </details>
        )}

        <p className="text-xs text-slate-500 dark:text-slate-400">
          {accepted} accepted · {skipped} skipped this session
        </p>
      </div>
    );
  }

  return (
    <>
      <Viewer key={item.taskId} item={item} retainedScaleRef={retainedScaleRef} />

      <footer className="flex flex-col gap-2">
        <div className="flex items-center justify-between text-xs text-slate-600 dark:text-slate-400">
          <span className="truncate" title={item.diveName}>
            {item.diveName} · task {item.taskId}
          </span>
          <span className="tabular-nums">
            {accepted} accepted · {skipped} skipped · {queue.length - index - 1} left
          </span>
        </div>

        {item.partial && (
          <p className="rounded-md border border-amber-400 bg-amber-50 px-2 py-1 text-xs text-amber-900 dark:border-amber-700/60 dark:bg-amber-950/40 dark:text-amber-200">
            Partial detection — the model found {item.keypoints.length} of the expected points.
          </p>
        )}

        {message && (
          <p className="rounded-md border border-slate-300 bg-white px-2 py-1 text-xs text-slate-700 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300">
            {message}
          </p>
        )}

        <div className="flex gap-2">
          <button
            type="button"
            onClick={onSkip}
            className="flex-1 rounded-md border border-slate-300 bg-white px-3 py-2 text-sm font-medium shadow-xs transition hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40 dark:border-slate-700 dark:bg-slate-900 dark:hover:bg-slate-800"
          >
            Skip
          </button>
          <button
            type="button"
            onClick={onAccept}
            className="flex-2 rounded-md border border-emerald-600 bg-emerald-600 py-2 text-sm font-semibold text-white shadow-xs transition hover:bg-emerald-500 dark:border-emerald-500 dark:bg-emerald-600 dark:hover:bg-emerald-500"
          >
            Accept
          </button>
          {undo && (
            <button
              type="button"
              onClick={onUndo}
              className="rounded-md border border-slate-300 bg-white px-3 py-2 text-sm font-medium shadow-xs transition hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40 dark:border-slate-700 dark:bg-slate-900 dark:hover:bg-slate-800 text-xs"
            >
              Undo
            </button>
          )}
        </div>
      </footer>
    </>
  );
}

/** The task's frame in the shared viewer (app/_review/zoom-frame.tsx, moved
 *  there from here on 2026-10-22), with its predictions as markers. */
function Viewer({
  item,
  retainedScaleRef,
}: {
  item: TriageItem;
  retainedScaleRef: React.MutableRefObject<number | null>;
}) {
  const first = item.keypoints[0];
  const W = first?.originalWidth ?? 4000;
  const H = first?.originalHeight ?? 3000;
  return (
    <ZoomFrame
      src={`/api/triage/image/${item.taskId}`}
      width={W}
      height={H}
      focus={first ?? null}
      retainedScaleRef={retainedScaleRef}
      markers={item.keypoints.map((kp) => ({
        x: (kp.xPercent / 100) * W,
        y: (kp.yPercent / 100) * H,
        color: colorFor(kp.label),
      }))}
    />
  );
}

function colorFor(label: string): string {
  const l = label.toLowerCase();
  if (l.includes("green")) return "#41e06a";
  if (l.includes("red")) return "#ff5252";
  if (l.includes("snout")) return "#ffa39e";
  if (l.includes("fork")) return "#26a269";
  return "#ffd400";
}
