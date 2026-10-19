"""Processor workers off NRP: every role, on a machine deployed by hand.

New in v2 (2026-10-19). Lab machines (one RTX A6000; four RTX 4500 Ada) join
NRP to push the automatic-results data through. They are first-class: the
released images, every processor role, and the NRP pods' own environment --
rendered from deploy/nrp/*.yaml, so the two never drift. Credentials stay in
the operator's account (0600), mounted read-only, never inline.
"""

from __future__ import annotations

import sys

import yaml

from _deploy import REPO

sys.path.insert(0, str(REPO / "deploy" / "workers"))
from render import render  # noqa: E402

NRP = REPO / "deploy" / "nrp"


def _manifest_env(name: str) -> dict:
    body = yaml.safe_load((NRP / f"{name}.yaml").read_text())
    (container,) = body["spec"]["template"]["spec"]["containers"]
    return {e["name"]: e["value"] for e in container["env"]}


def _render(**overrides):
    args = dict(gpus=4, cpus=128, version="v9.8.7", uid=1234, gid=5678,
                home="/home/op/.fishsense-worker", data="/scratch/op")  # fmt: skip
    args.update(overrides)
    return render(**args)["services"]


def test_one_gpu_worker_per_card_and_every_other_role():
    services = _render()
    gpus = [s for name, s in services.items() if name.startswith("gpu-")]
    assert len(gpus) == 4
    # The lab machines expose GPUs through CDI (docker info: nvidia.com/gpu=N).
    devices = sorted(d for s in gpus for d in s["devices"])
    assert devices == [f"nvidia.com/gpu={i}" for i in range(4)]
    assert all(len(s["devices"]) == 1 for s in gpus)
    assert {"per-image", "light"} <= set(services)


def test_each_worker_has_its_nrp_pods_environment():
    """The manifests are the one source: role, Temporal, certs, caches."""
    services = _render()
    for name, role, manifest in [("gpu-0", "gpu", "gpu"),
                                 ("per-image", "per_image", "per-image"),
                                 ("light", "light", "light")]:  # fmt: skip
        env = services[name]["environment"]
        for key, value in _manifest_env(manifest).items():
            assert env[key] == value, (name, key)
        assert env["FISHSENSE_PROCESSOR_ROLE"] == role


def test_the_released_images_at_one_version():
    services = _render(version="v1.2.3")
    assert (
        services["gpu-0"]["image"]
        == "ghcr.io/ucsd-e4e/fishsense-services-processor-gpu:v1.2.3"
    )
    assert (
        services["per-image"]["image"]
        == "ghcr.io/ucsd-e4e/fishsense-services-processor:v1.2.3"
    )
    assert (
        services["light"]["image"]
        == "ghcr.io/ucsd-e4e/fishsense-services-processor:v1.2.3"
    )


def test_credentials_stay_in_the_operators_account_read_only():
    """The Temporal leaf and the processor secret are files in the operator's
    home (0600, `fetch_credentials.sh`), mounted read-only; the workers run as
    that user so they can read them. No secret value is in the compose."""
    for name, service in _render().items():
        assert service["user"] == "1234:5678", name
        assert "/home/op/.fishsense-worker/certs:/certs:ro" in service["volumes"]
        # raw: a secret value with a `$` is not interpolated by compose.
        assert service["env_file"] == [
            {"path": "/home/op/.fishsense-worker/processor.env", "format": "raw"}
        ]
        # The image's home is the app user's; this user's is its own cache.
        assert service["environment"]["HOME"] == "/cache"


def test_the_decoders_cap_scales_with_the_machine():
    """per_image peaks at 1-3 GB a decode (registry): a 128-CPU, 377 GB
    machine runs many; a 12-CPU one keeps the pod's two."""
    big = _render(cpus=128)["per-image"]["environment"]
    small = _render(cpus=12, gpus=1)["per-image"]["environment"]
    assert int(big["FISHSENSE_PROCESSOR_MAX_CONCURRENT_ACTIVITIES"]) >= 16
    assert int(small["FISHSENSE_PROCESSOR_MAX_CONCURRENT_ACTIVITIES"]) == 2


def test_caches_and_scratch_are_on_the_machines_local_disk():
    """Home is NFS on the lab machines (2026-10-19): multi-GB model caches and
    raw scratch there would be slow. They go under `data` (local ZFS:
    /scratch/fishsense on kastner-ml, /scratch/krg/<user> on waiter); only the
    credentials stay in the home, read-only."""
    for name, service in _render().items():
        assert service["restart"] == "unless-stopped"
        assert f"/scratch/op/cache/{name}:/cache" in service["volumes"]
        assert f"/scratch/op/scratch/{name}:/scratch" in service["volumes"]
        in_home = [v for v in service["volumes"] if v.startswith("/home/op")]
        assert all(v.endswith(":ro") for v in in_home), in_home


def test_the_credentials_script_never_prints_a_secret():
    """It streams the Temporal leaf and the processor Secret as a tar into
    the operator's account (0700/0600), on tmpfs in between; nothing it
    prints is a secret (CLAUDE.md: never read secrets)."""
    script = (REPO / "deploy" / "workers" / "fetch_credentials.sh").read_text()
    assert "umask 077" in script and "/dev/shm/" in script
    assert "chmod 600" in script and "chmod 700" in script
    for leak in ("cat ", "set -x", "| tee", 'echo "$d', "echo $"):
        assert leak not in script, leak
