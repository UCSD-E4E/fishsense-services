import { project } from "@/lib/public-page";
import { bubbleSize, mapFrame, type TenantRegion } from "@/lib/today";

/**
 * A tenant's regions on the world outline (public/world.svg), framed to
 * them: a bubble per region, sized by its fish, and its name and count
 * beside it. Made to look good in a screenshot rather than to survey (the
 * owner, 2026-10-21); never finer than a region.
 */
export function RegionMap({ regions }: { regions: TenantRegion[] }) {
  const frame = mapFrame(regions);
  const most = Math.max(...regions.map((r) => r.fish));
  return (
    <div
      className="relative w-full overflow-hidden rounded-sheet border border-line bg-teal-tint/40"
      style={{ aspectRatio: frame.aspect }}
    >
      {/* The whole world, scaled and shifted so the frame fills the box. */}
      <div
        className="absolute"
        style={{
          width: `${100 / frame.width}%`,
          height: `${100 / frame.height}%`,
          left: `${(-frame.x / frame.width) * 100}%`,
          top: `${(-frame.y / frame.height) * 100}%`,
        }}
      >
        <div
          aria-hidden
          className="absolute inset-0 bg-fg-3/25"
          style={{
            maskImage: "url(/world.svg)",
            WebkitMaskImage: "url(/world.svg)",
            maskSize: "100% 100%",
            WebkitMaskSize: "100% 100%",
          }}
        />
        {regions.map((r) => {
          const { left, top } = project(r.latitude, r.longitude);
          const size = bubbleSize(r.fish, most);
          return (
            <div key={r.name} className="absolute" style={{ left: `${left}%`, top: `${top}%` }}>
              <div
                aria-hidden
                className="absolute -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-teal bg-teal/20"
                style={{ width: size, height: size }}
              />
              <div aria-hidden className="absolute size-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full bg-teal" />
              <div
                className="absolute flex -translate-y-1/2 flex-col whitespace-nowrap rounded-control border border-line bg-surface px-3 py-1.5 shadow-sm"
                style={{ left: size / 2 + 6 }}
              >
                <span className="text-xs text-fg-2">{r.name}</span>
                <span className="font-semibold tabular-nums">{r.fish.toLocaleString()} fish</span>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
