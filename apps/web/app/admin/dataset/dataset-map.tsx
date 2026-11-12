import { unstable_rethrow } from "next/navigation";
import { Failure, reason, RegionsOnMap } from "@/app/[tenant]/today-sections";
import { getDatasetRegions } from "@/lib/dataset";
import type { MapSource } from "@/lib/today";

/** Every tenant's fish by broad region, opted in to the public page or not
 *  (the owner, 2026-11-11): by the human labels, or by the automatic
 *  lengths, which nobody has reviewed and the map says so. */
export async function DatasetMap({ token, source }: { token: string; source: MapSource }) {
  let found;
  try {
    found = await getDatasetRegions(token, source);
  } catch (error) {
    unstable_rethrow(error);
    return <Failure>Could not load the map: {reason(error)}</Failure>;
  }
  if (found === null) {
    return <p className="text-fg-2">This map is only for system admins.</p>;
  }
  return <RegionsOnMap found={found} automatic={source === "automatic"} />;
}
