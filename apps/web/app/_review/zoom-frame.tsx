"use client";

// Moved out of app/portal/triage/triage-client.tsx (ported there from
// fishsense-lite@77e8f8e5 apps/fishsense-lite-web/app/portal/triage/
// triage-client.tsx) on 2026-10-22, so a frame's review uses the Labeling
// tab's viewer: the owner, "its code can and should be reused". Kept: the deep
// zoom, the markers that stay one size on screen, the pinch and double-tap,
// and the zoom carried between frames. Added for review: a tap or click that
// doesn't drag places a point (`onPlace`), wheel zoom for a desktop, and
// layers drawn in the image's own pixels (the mask, the head-tail line).

import { useCallback, useEffect, useRef, useState } from "react";
import { MAX_SCALE, clampScale, initialView, type ViewPoint } from "@/lib/triage-view";

export type Marker = {
  /** Image pixels. */
  x: number;
  y: number;
  color: string;
  /** A smaller, hollow mark: where a point was dropped before its snap. */
  faint?: boolean;
};

/** A tap that moves less than this (CSS px) is a tap, not a pan. */
const TAP_SLOP = 5;

/**
 * Zoomable frame with constant-size markers.
 *
 * The markers are carried by the image — they name a specific pixel, so they
 * must travel with it — while staying a fixed size on screen. That is what
 * makes deep zoom useful: at high magnification the ring ends up smaller than
 * a single image pixel, so the pixel being claimed is unambiguous.
 */
export function ZoomFrame({
  src,
  width: W,
  height: H,
  focus,
  markers,
  retainedScaleRef,
  layers,
  onPlace,
  alt = "",
}: {
  src: string;
  width: number;
  height: number;
  /** Where a retained zoom re-centers: the thing being judged. */
  focus: ViewPoint | null;
  markers: Marker[];
  retainedScaleRef: React.MutableRefObject<number | null>;
  /** Drawn in the image's pixels, under the markers. */
  layers?: React.ReactNode;
  /** A tap or click that didn't drag, in image pixels. */
  onPlace?: (p: { x: number; y: number }) => void;
  alt?: string;
}) {
  const stageRef = useRef<HTMLDivElement>(null);
  const layerRef = useRef<HTMLDivElement>(null);
  const imageRef = useRef<HTMLImageElement>(null);
  const [ready, setReady] = useState(false);

  // A server-rendered frame can finish loading before the page hydrates, and
  // then `onLoad` never reaches React: the viewer would sit on "Loading
  // frame..." over a loaded image, swallowing every tap (seen in the harness,
  // 2026-10-22). So ask the image itself once mounted.
  useEffect(() => {
    const image = imageRef.current;
    if (image?.complete && image.naturalWidth > 0) setReady(true);
  }, [src]);
  const view = useRef({ k: 1, tx: 0, ty: 0 });

  /**
   * One imperative write per frame, and no React state.
   *
   * The markers live INSIDE the transformed layer, so they are carried by the
   * image for free. `--inv` counter-scales them, which is what keeps the ring
   * 11 CSS px across at every magnification: the browser composites it, so
   * panning never re-renders the tree or recomputes a screen position.
   */
  const paint = useCallback(() => {
    const layer = layerRef.current;
    if (!layer) return;
    const { k, tx, ty } = view.current;
    layer.style.transform = `translate(${tx}px,${ty}px) scale(${k})`;
    layer.style.setProperty("--inv", String(1 / k));
  }, []);

  const focusX = focus?.xPercent;
  const focusY = focus?.yPercent;
  const fit = useCallback(() => {
    const stage = stageRef.current;
    if (!stage) return;
    // Fractional box on purpose: `clientHeight` is integer-rounded, and that
    // rounding is visible once the frame is magnified a hundredfold.
    const r = stage.getBoundingClientRect();
    view.current = initialView({
      stage: { width: r.width, height: r.height },
      image: { width: W, height: H },
      keypoint: focusX != null && focusY != null ? { xPercent: focusX, yPercent: focusY } : null,
      retainedScale: retainedScaleRef.current,
    });
    paint();
  }, [W, H, focusX, focusY, paint, retainedScaleRef]);

  useEffect(() => {
    fit();
    const onResize = () => fit();
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [fit]);

  // --- gestures -------------------------------------------------------------

  const pointers = useRef(new Map<number, { x: number; y: number }>());
  const pinch = useRef<{ dist: number; cx: number; cy: number } | null>(null);
  const lastTap = useRef(0);
  const press = useRef<{ x: number; y: number; moved: boolean } | null>(null);

  function pinchState() {
    const p = [...pointers.current.values()];
    return {
      dist: Math.hypot(p[0].x - p[1].x, p[0].y - p[1].y) || 1,
      cx: (p[0].x + p[1].x) / 2,
      cy: (p[0].y + p[1].y) / 2,
    };
  }

  function zoomAbout(nextK: number, cx: number, cy: number) {
    const { k, tx, ty } = view.current;
    const ix = (cx - tx) / k;
    const iy = (cy - ty) / k;
    const clamped = clampScale(nextK);
    view.current = { k: clamped, tx: cx - ix * clamped, ty: cy - iy * clamped };
    retainedScaleRef.current = clamped;
    paint();
  }

  // A wheel listener must be non-passive to keep the page from scrolling.
  useEffect(() => {
    const stage = stageRef.current;
    if (!stage) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const r = stage.getBoundingClientRect();
      zoomAbout(view.current.k * Math.exp(-e.deltaY * 0.0015), e.clientX - r.left, e.clientY - r.top);
    };
    stage.addEventListener("wheel", onWheel, { passive: false });
    return () => stage.removeEventListener("wheel", onWheel);
    // zoomAbout reads refs only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div
      ref={stageRef}
      className={`relative aspect-4/3 max-h-[70vh] w-full touch-none select-none overflow-hidden rounded-md border border-slate-300 bg-black dark:border-slate-800 ${onPlace ? "cursor-crosshair" : ""}`}
      onPointerDown={(e) => {
        (e.target as Element).setPointerCapture?.(e.pointerId);
        pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
        if (pointers.current.size === 2) pinch.current = pinchState();
        press.current = pointers.current.size === 1 ? { x: e.clientX, y: e.clientY, moved: false } : null;

        // Double-tap zoom, where a tap doesn't place a point: placing twice
        // in a row is how a reviewer corrects a drop.
        const now = Date.now();
        if (!onPlace && pointers.current.size === 1 && now - lastTap.current < 320) {
          const r = stageRef.current!.getBoundingClientRect();
          zoomAbout(view.current.k > 60 ? 1 : MAX_SCALE, e.clientX - r.left, e.clientY - r.top);
        }
        lastTap.current = now;
      }}
      onPointerMove={(e) => {
        if (!pointers.current.has(e.pointerId)) return;
        const prev = pointers.current.get(e.pointerId)!;
        pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
        if (press.current && Math.hypot(e.clientX - press.current.x, e.clientY - press.current.y) > TAP_SLOP) {
          press.current.moved = true;
        }

        if (pointers.current.size === 1) {
          view.current.tx += e.clientX - prev.x;
          view.current.ty += e.clientY - prev.y;
          paint();
        } else if (pointers.current.size === 2 && pinch.current) {
          const now = pinchState();
          const r = stageRef.current!.getBoundingClientRect();
          const { k, tx, ty } = view.current;
          const ix = (pinch.current.cx - r.left - tx) / k;
          const iy = (pinch.current.cy - r.top - ty) / k;
          const nk = clampScale(k * (now.dist / pinch.current.dist));
          view.current = {
            k: nk,
            tx: now.cx - r.left - ix * nk,
            ty: now.cy - r.top - iy * nk,
          };
          pinch.current = now;
          retainedScaleRef.current = nk;
          paint();
        }
      }}
      onPointerUp={(e) => {
        const tap = press.current && !press.current.moved && pointers.current.size === 1;
        pointers.current.delete(e.pointerId);
        pinch.current = pointers.current.size === 2 ? pinchState() : null;
        press.current = null;
        if (tap && onPlace && ready) {
          const r = stageRef.current!.getBoundingClientRect();
          const { k, tx, ty } = view.current;
          const x = (e.clientX - r.left - tx) / k;
          const y = (e.clientY - r.top - ty) / k;
          if (x >= 0 && x < W && y >= 0 && y < H) onPlace({ x, y });
        }
      }}
      onPointerCancel={(e) => {
        pointers.current.delete(e.pointerId);
        pinch.current = null;
        press.current = null;
      }}
    >
      <div ref={layerRef} className="absolute left-0 top-0 origin-top-left">
        {/* Deliberately a plain <img>. `next/image` resamples and re-encodes,
            which is exactly what must not happen to a frame whose purpose is
            judging an individual pixel — and the bytes come from an
            authenticated proxy or a presigned link the optimizer cannot reach
            anyway. */}
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          ref={imageRef}
          src={src}
          alt={alt}
          draggable={false}
          width={W}
          height={H}
          // `max-w-none` is load-bearing: Tailwind's preflight sets
          // `img { max-width: 100% }`, which clamps a 4000px frame to the stage
          // width so `scale(k)` multiplies a base ten times too small.
          // Smoothing is off because it invents detail that is not in the
          // frame, the opposite of what a verification view should do.
          className="pointer-events-none block max-w-none [image-rendering:pixelated]"
          onLoad={() => setReady(true)}
        />

        {ready && layers && (
          <svg
            viewBox={`0 0 ${W} ${H}`}
            width={W}
            height={H}
            aria-hidden
            className="pointer-events-none absolute left-0 top-0"
          >
            {layers}
          </svg>
        )}

        {/* Held back until a frame has actually decoded: drawn over a blank
            view, a marker claims a point on an image nobody can see. */}
        {ready &&
          markers.map((m, i) => (
            <svg
              key={i}
              viewBox="-20 -20 40 40"
              aria-hidden
              className="pointer-events-none absolute h-10 w-10 overflow-visible"
              style={{
                left: `${m.x}px`,
                top: `${m.y}px`,
                margin: "-20px 0 0 -20px",
                transform: "scale(var(--inv, 1))",
              }}
            >
              {m.faint ? (
                <circle r="5" fill="none" stroke={m.color} strokeWidth="1.2" strokeDasharray="2 2" />
              ) : (
                <>
                  <circle r="11" fill="none" stroke="#000" strokeOpacity="0.55" strokeWidth="3.5" />
                  <circle r="11" fill="none" stroke={m.color} strokeWidth="1.5" />
                  <path d="M-9 0H-4M4 0H9M0 -9V-4M0 4V9" stroke={m.color} strokeWidth="1.2" />
                  <circle r="1" fill={m.color} />
                </>
              )}
            </svg>
          ))}
      </div>

      {/* Outside the transformed layer, so retained zoom cannot magnify it. */}
      {!ready && (
        <div className="absolute inset-0 grid place-items-center text-xs text-slate-400">
          Loading frame…
        </div>
      )}
    </div>
  );
}
