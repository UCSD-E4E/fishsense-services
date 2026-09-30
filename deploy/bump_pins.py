#!/usr/bin/env python3
"""Move every v2 image pin, and the processor's tag, to one release version.

    python3 deploy/bump_pins.py v1.2.3 deploy/incus/compose.yml

promote.yml runs this after it retags the release's images, then opens the
auto-deploy PR with the diff. The equivalent of fishsense-lite promote.yml's
`sed` over `image: ghcr.io/ucsd-e4e/<pkg>:vX.Y.Z`, with two differences:

* ONE version moves everything: `ghcr.io/ucsd-e4e/fishsense-services-*` pins
  AND `FISHSENSE_NRP_IMAGE_TAG`, the tag the orchestrator stands the NRP
  processor up at (the processor is not in the compose, and its image tag is
  deploy config, not baked into the orchestrator);
* it is a script with tests (deploy/tests/test_release_pipeline.py), not an
  inline regex: it refuses a non-version, refuses a file with nothing to bump,
  and checks that afterwards every pin agrees -- a regex that silently matched
  nothing would open no PR and the release would never deploy.

Stdlib only: it runs on a bare GitHub-hosted runner.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

VERSION = re.compile(r"v\d+\.\d+\.\d+")
PIN = re.compile(
    r"(image:\s*ghcr\.io/ucsd-e4e/fishsense-services-[a-z-]+:)v\d+\.\d+\.\d+"
)
TAG = re.compile(r"(FISHSENSE_NRP_IMAGE_TAG:\s*[\"']?)v\d+\.\d+\.\d+")


def bump(text: str, version: str) -> str:
    bumped, pins = PIN.subn(rf"\g<1>{version}", text)
    bumped, tags = TAG.subn(rf"\g<1>{version}", bumped)
    if not pins or not tags:
        raise SystemExit(
            f"nothing to bump: {pins} image pins, {tags} FISHSENSE_NRP_IMAGE_TAG"
        )
    found = {m.group(0).rsplit(":", 1)[-1].strip("\"' ") for m in PIN.finditer(bumped)}
    found |= {m.group(0).rsplit(":", 1)[-1].strip("\"' ") for m in TAG.finditer(bumped)}
    if found != {version}:
        raise SystemExit(f"pins disagree after the bump: {sorted(found)}")
    return bumped


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    version, files = argv[0], argv[1:]
    if not VERSION.fullmatch(version):
        print(f"not a release version (vX.Y.Z): {version!r}", file=sys.stderr)
        return 2
    for name in files:
        path = Path(name)
        path.write_text(bump(path.read_text(), version))
        print(f"updated: {path} -> {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
