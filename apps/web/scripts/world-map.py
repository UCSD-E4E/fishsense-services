"""public/world.svg: the signed-out page's world outline (lib/public-page.ts).

From Natural Earth's 110m land polygons, which are in the public domain:
https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_110m_land.geojson

Equirectangular, 84°N to 58°S, Antarctica left out, 1200 wide; lib/public-page.ts
projects pins with the same frame, and its test pins the numbers. The page draws
the file as a CSS mask, so its one colour (black) is only an outline.

    uv run --no-project python -I scripts/world-map.py ne_110m_land.geojson public/world.svg
"""

import json
import sys

WIDTH, NORTH, SOUTH = 1200.0, 84.0, -58.0


def xy(lon: float, lat: float) -> tuple[float, float]:
    return (lon + 180.0) / 360.0 * WIDTH, (NORTH - lat) / 360.0 * WIDTH


def main(source: str, out: str) -> None:
    height = (NORTH - SOUTH) / 360.0 * WIDTH
    parts = []
    for feature in json.load(open(source))["features"]:
        geometry = feature["geometry"]
        polygons = (
            geometry["coordinates"]
            if geometry["type"] == "MultiPolygon"
            else [geometry["coordinates"]]
        )
        for polygon in polygons:
            ring = polygon[0]
            if max(lat for _, lat in ring) < -55:  # Antarctica
                continue
            points = [xy(lon, max(min(lat, NORTH), SOUTH)) for lon, lat in ring]
            parts.append("M" + "L".join(f"{x:.1f} {y:.1f}" for x, y in points) + "Z")
    with open(out, "w") as f:
        f.write(
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH:.0f} {height:.0f}">'
            f'<path fill="#000" d="{"".join(parts)}"/></svg>\n'
        )


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
