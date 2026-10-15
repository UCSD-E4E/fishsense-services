"""deploy/nrp/processor-secret.sh: the NRP processor's Secret, and the model
weights it names.

What the processor relies on, pinned here because nothing else runs the
script before production does:

* **every weights pin a stage requires is written.** The automatic-results
  track runs BioCLIP species (decided 2026-10-15), whose settings require
  `FISHSENSE_BIOCLIP_*`; the 2026-10-15 audit found them missing, so the
  species step would fail on its first frame;
* **BioCLIP lands where the processor fetches it**, the paths derived from
  the processor's own constants, so the two can't drift apart;
* **the Secret's data never sits in an annotation.** A client-side
  `kubectl apply` keeps a full copy in
  `kubectl.kubernetes.io/last-applied-configuration`, readable by anyone who
  can `get` the Secret; a dump of that annotation on another Secret exposed
  part of a private key on 2026-10-14. The script applies server-side and
  removes any copy an earlier client-side apply left.
"""

from __future__ import annotations

import re
from pathlib import Path

from fishsense_services_processor.species_predict.weights import (
    BIOCLIP_FILENAME,
    BIOCLIP_MODEL,
    BIOCLIP_VERSIONS,
)

SCRIPT = (
    Path(__file__).resolve().parents[1] / "nrp" / "processor-secret.sh"
).read_text()


def _secret_keys() -> set[str]:
    """The keys of the env file the Secret is created from."""
    (body,) = re.findall(
        r'cat > "\$work/processor\.env" <<EOF\n(.*?)\nEOF', SCRIPT, re.S
    )
    return {line.split("=", 1)[0] for line in body.splitlines() if line.strip()}


def test_the_secret_pins_bioclip_for_the_species_step():
    assert {
        "FISHSENSE_BIOCLIP_SHA256",
        "FISHSENSE_BIOCLIP_SIZE",
        "FISHSENSE_BIOCLIP_FALLBACK_SHA256",
        "FISHSENSE_BIOCLIP_FALLBACK_SIZE",
    } <= _secret_keys()


def test_the_secret_still_pins_sam_and_the_slate_detector():
    assert {
        "FISHSENSE_SAM3_SHA256",
        "FISHSENSE_SAM3_SIZE",
        "FISHSENSE_SLATE_DETECTOR_SHA256",
        "FISHSENSE_SLATE_DETECTOR_SIZE",
    } <= _secret_keys()


def test_bioclip_is_uploaded_where_the_processor_fetches_it():
    for version in BIOCLIP_VERSIONS.values():
        assert f"{BIOCLIP_MODEL}/{version}/{BIOCLIP_FILENAME}" in SCRIPT, version


def test_the_secret_is_applied_server_side():
    applies = [
        line
        for line in SCRIPT.splitlines()
        if "kubectl" in line and " apply " in line and not line.lstrip().startswith("#")
    ]
    assert applies
    assert all("--server-side" in line for line in applies), applies


def test_a_copy_left_by_an_earlier_client_side_apply_is_removed():
    assert "kubectl.kubernetes.io/last-applied-configuration-" in SCRIPT
