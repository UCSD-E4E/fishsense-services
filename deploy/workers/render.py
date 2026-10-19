#!/usr/bin/env python3
"""Render a compose file of processor workers for a machine off NRP.

New in v2 (2026-10-19). A lab machine deployed by hand runs every processor
role -- one GPU worker per card, a per-image worker sized to its CPUs, a light
worker -- polling the same Temporal queues as the NRP pods, so it takes work
just by running. Each worker gets its NRP pod's own environment, read from
deploy/nrp/*.yaml (the one source: tests keep them identical), the released
images at one version, and the credentials `fetch_credentials.sh` leaves in the
operator's account: mounted read-only, never written here.

    uv run python deploy/workers/render.py --gpus 4 --cpus 128 \\
        --version v0.13.1 --uid 1000 --gid 1000 \\
        --home /home/op/.fishsense-worker --data /scratch/op > compose.yml

`home` holds the credentials (the operator's account); `data` the model
caches and raw scratch, on the machine's local disk -- the lab machines'
homes are NFS (2026-10-19), too slow for multi-GB caches.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

NRP = Path(__file__).resolve().parents[1] / "nrp"
GHCR = "ghcr.io/ucsd-e4e"
#: Workers per manifest: (service name, manifest, image).
GPU_IMAGE = f"{GHCR}/fishsense-services-processor-gpu"
CPU_IMAGE = f"{GHCR}/fishsense-services-processor"


def _env(manifest: str) -> dict[str, str]:
    body = yaml.safe_load((NRP / f"{manifest}.yaml").read_text())
    (container,) = body["spec"]["template"]["spec"]["containers"]
    return {e["name"]: str(e["value"]) for e in container["env"]}


def per_image_cap(cpus: int) -> int:
    """A raw decode peaks at 1-3 GB and a core (registry): a big machine runs
    many; a small one keeps the pod's two."""
    return max(2, min(24, cpus // 6))


def _worker(name: str, manifest: str, image: str, version: str, uid: int,
            gid: int, home: str, data: str,
            extra_env: dict | None = None) -> dict:  # fmt: skip
    env = {**_env(manifest), "HOME": "/cache", **(extra_env or {})}
    return {
        "image": f"{image}:{version}",
        "user": f"{uid}:{gid}",
        "restart": "unless-stopped",
        "stop_grace_period": "45s",
        "environment": env,
        # raw: compose must not interpolate a secret value's `$`.
        "env_file": [{"path": f"{home}/processor.env", "format": "raw"}],
        "volumes": [
            f"{home}/certs:/certs:ro",
            f"{data}/cache/{name}:/cache",
            f"{data}/scratch/{name}:/scratch",
        ],
    }


def render(*, gpus: int, cpus: int, version: str, uid: int, gid: int,
           home: str, data: str) -> dict:  # fmt: skip
    services = {}
    for index in range(gpus):
        name = f"gpu-{index}"
        service = _worker(name, "gpu", GPU_IMAGE, version, uid, gid, home, data)
        # GPUs through CDI (the lab machines' docker: nvidia.com/gpu=N).
        service["devices"] = [f"nvidia.com/gpu={index}"]
        services[name] = service
    services["per-image"] = _worker(
        "per-image", "per-image", CPU_IMAGE, version, uid, gid, home, data,
        {"FISHSENSE_PROCESSOR_MAX_CONCURRENT_ACTIVITIES": str(per_image_cap(cpus))},
    )  # fmt: skip
    services["light"] = _worker(
        "light", "light", CPU_IMAGE, version, uid, gid, home, data
    )
    return {"name": "fishsense-workers", "services": services}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--gpus", type=int, required=True)
    parser.add_argument("--cpus", type=int, required=True)
    parser.add_argument("--version", required=True, help="the release, vX.Y.Z")
    parser.add_argument("--uid", type=int, required=True)
    parser.add_argument("--gid", type=int, required=True)
    parser.add_argument("--home", required=True, help="the credentials' directory")
    parser.add_argument("--data", required=True, help="local disk: caches, scratch")
    args = parser.parse_args(argv)
    yaml.safe_dump(render(
        gpus=args.gpus,
        cpus=args.cpus,
        version=args.version,
        uid=args.uid,
        gid=args.gid, home=args.home, data=args.data),  # fmt: skip
        sys.stdout,
        sort_keys=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
