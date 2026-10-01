#!/usr/bin/env python3
"""Stage the PRODUCTION compose for a local rehearsal (docs/cutover.md).

    python3 deploy/rehearsal/stage.py --out ~/rehearsal --version v0.3.0
    python3 deploy/rehearsal/stage.py --out ~/rehearsal --local-images
    ~/rehearsal/dc up -d postgres            # then restore v1's dump, then:
    ~/rehearsal/dc up -d                     # bootstrap, migrate, the services
    ~/rehearsal/dc run --rm migrate fishsense-services-api migrate-v1
    ~/rehearsal/dc run --rm smoke --dive 490

The slot's compose reads its secrets from /run/tenant (vault-agent) and its
config from /var/lib/krg/fishsense (workdir.nix). This reproduces both under
``--out``: it renders deploy/incus/secrets.nix's templates with the rehearsal's
own values (a small consul-template: `with secret`, `.Data.data.<field>`,
`urlquery`), rewrites /run/tenant to that directory, copies deploy/incus as the
project directory, and layers compose.rehearsal.yml (local Temporal, no NRP, no
edge). Everything runs as project `fishsense-rehearsal`, so nothing it starts
can be the slot's or another local stack's.

Values: ``--values values.toml`` holds ``[<openbao path>] field = "..."``, e.g.
read-only Garage keys under ``[object_store]`` (PLAN.md §6.5: rehearsals read
v1's buckets with read-only keys). A missing database password is generated; a
missing external credential becomes a placeholder, so the service starts and
only its calls out fail.
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import shutil
import stat
import sys
import tomllib
from pathlib import Path
from urllib.parse import quote_plus

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
INCUS = REPO / "deploy" / "incus"
TENANT_RUN = "/run/tenant"
PROJECT = "fishsense-rehearsal"

_RENDER = re.compile(
    r'destination\s*=\s*"(?P<dest>[^"]+)";(?P<attrs>.*?)contents\s*=\s*\'\'(?P<body>.*?)\'\';',
    re.S,
)
_WITH = re.compile(r'\{\{\s*with secret "secret/data/tenants/fishsense/([^"]+)"\s*\}\}')
_FIELD = re.compile(r"\{\{\s*\.Data\.data\.([a-z_]+)(\s*\|\s*urlquery)?\s*\}\}")

#: Generated when not given: they are the rehearsal's own, never production's.
GENERATED = {"postgres", "services_db", "superset", "web"}
#: Placeholders shaped like the real thing, where a service validates a shape.
PLACEHOLDERS = {
    ("oidc/web", "issuer_url"): "https://auth.example.invalid/application/o/fishsense/",
    ("oidc/web", "client_id"): "fishsense-web-rehearsal",
    (
        "oidc/analytics",
        "issuer_url",
    ): "https://auth.example.invalid/application/o/fishsense-analytics/",
    ("oidc/analytics", "client_id"): "fishsense-analytics-rehearsal",
}


def render_secrets(nix: str, values: dict[str, dict[str, str]]) -> dict[str, str]:
    """Every render's destination and contents, as vault-agent would write
    them. A soft render (errorOnMissingKey = false) whose path has no values
    renders empty, as vault-agent's does."""
    rendered = {}
    for match in _RENDER.finditer(nix):
        soft = "errorOnMissingKey = false" in match["attrs"]
        lines = []
        path = None
        for raw in match["body"].splitlines():
            line = raw.strip()
            if not line:
                continue
            for opened in _WITH.finditer(line):
                path = opened.group(1)
            line = _WITH.sub("", line).replace("{{ end }}", "")
            if soft and path not in values:
                continue

            def _value(field: re.Match, path=path) -> str:
                value = _lookup(values, path, field.group(1))
                return quote_plus(value) if field.group(2) else value

            lines.append(_FIELD.sub(_value, line))
        rendered[match["dest"]] = "".join(f"{line}\n" for line in lines)
    return rendered


def _lookup(values: dict[str, dict[str, str]], path: str, field: str) -> str:
    given = values.get(path, {})
    if field in given:
        return str(given[field])
    if (path, field) in PLACEHOLDERS:
        return PLACEHOLDERS[(path, field)]
    if path in GENERATED:
        # Remembered, so two renders of one field (the app role's DSN in api.env
        # and orchestrator.env) agree -- as one OpenBao field does.
        return values.setdefault(path, {}).setdefault(field, secrets.token_hex(16))
    return f"unset-in-rehearsal-{path.replace('/', '-')}-{field}"


#: Each production host a rehearsal may reach, the OpenBao path whose
#: credential opens it, and where the rehearsal points instead without one.
#: Placeholder keys against the real host still make a (failing) call to it --
#: the first rehearsal did, to Label Studio and Garage -- so a service with no
#: credential given reaches an `.invalid` host, which fails without leaving the
#: box. PLAN.md §6.5: what a rehearsal may be given is read-only.
PRODUCTION_HOSTS = (
    ("https://app.heartex.com", "label_studio", "https://label-studio.example.invalid"),
    ("https://s3.e4e.ucsd.edu", "object_store", "https://s3.example.invalid"),
    ("https://e4e-nas.ucsd.edu:6021", "nas", "https://nas.example.invalid:6021"),
)


def _offline(text: str, values: dict[str, dict[str, str]]) -> str:
    for host, path, instead in PRODUCTION_HOSTS:
        if path not in values:
            text = text.replace(host, instead)
    return text


def _processor_env(values: dict[str, dict[str, str]]) -> str:
    """The processor's k8s Secret (docs/cutover.md), as an env file."""
    store = values.get("object_store", {})
    weights = values.get("model_weights", {})
    s3 = "https://s3.e4e.ucsd.edu"
    env = {
        "FISHSENSE_OBJECT_STORE_ENDPOINT_URL": (
            s3 if store else "https://s3.example.invalid"
        ),
        "FISHSENSE_OBJECT_STORE_REGION": "garage",
        "FISHSENSE_OBJECT_STORE_BUCKET": "labels-fishsense-lite",
        "FISHSENSE_OBJECT_STORE_LEGACY_LABELS_PREFIX": "fishsense-lite",
        "FISHSENSE_OBJECT_STORE_ACCESS_KEY_ID": store.get("access_key", "unset"),
        "FISHSENSE_OBJECT_STORE_SECRET_ACCESS_KEY": store.get("secret_key", "unset"),
        "FISHSENSE_MODEL_WEIGHTS_ENDPOINT_URL": (
            s3 if weights else "https://s3.example.invalid"
        ),
        "FISHSENSE_MODEL_WEIGHTS_ACCESS_KEY_ID": weights.get("access_key", "unset"),
        "FISHSENSE_MODEL_WEIGHTS_SECRET_ACCESS_KEY": weights.get("secret_key", "unset"),
    }
    return "".join(f"{k}={v}\n" for k, v in env.items())


def _images(compose: str, version: str | None, local: bool) -> str:
    if local:
        return re.sub(
            r"ghcr\.io/ucsd-e4e/(fishsense-services-[a-z-]+):v\d+\.\d+\.\d+",
            r"\1:rehearsal",
            compose,
        )
    if version:
        sys.path.insert(0, str(REPO / "deploy"))
        from bump_pins import bump  # pylint: disable=import-outside-toplevel

        return bump(compose, version)
    return compose


def stage(
    out: Path,
    *,
    values: dict[str, dict[str, str]] | None = None,
    version: str | None = None,
    local_images: bool = False,
    profiles: str = "",
) -> Path:
    """Stage under ``out``; returns the `dc` wrapper's path."""
    values = {k: dict(v) for k, v in (values or {}).items()}
    given = set(values)  # before rendering generates the rehearsal's own
    out = out.resolve()
    run = out / "run-tenant"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    for destination, contents in render_secrets((INCUS / "secrets.nix").read_text(), values).items():  # fmt: skip
        path = run / destination.removeprefix(TENANT_RUN + "/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
        path.chmod(0o600)
    (run / "secrets" / "processor.env").write_text(_processor_env(values))
    for sub in ("tls", "temporal", "nrp"):
        (run / sub).mkdir(parents=True, exist_ok=True)

    project = out / "project"
    shutil.copytree(INCUS, project, ignore=shutil.ignore_patterns("compose.yml"))
    (project / ".env").write_text(f"COMPOSE_PROFILES={profiles}\n")

    compose = (INCUS / "compose.yml").read_text().replace(TENANT_RUN, str(run))
    # Fixed container names are global to the docker host: the rehearsal's own.
    compose = re.sub(r"(container_name:\s*)(\S+)", rf"\g<1>{PROJECT}-\g<2>", compose)
    compose = _offline(compose, {k: {} for k in given})
    (out / "compose.yml").write_text(_images(compose, version, local_images))

    pins = re.search(r"fishsense-services-orchestrator:(v\d+\.\d+\.\d+)", compose)
    processor_tag = "rehearsal" if local_images else (version or pins.group(1))
    override = (HERE / "compose.rehearsal.yml").read_text()
    override = override.replace("REHEARSAL_RUN", str(run))
    override = override.replace(
        "ghcr.io/ucsd-e4e/fishsense-services-processor:REHEARSAL_VERSION",
        (
            "fishsense-services-processor:rehearsal"
            if local_images
            else f"ghcr.io/ucsd-e4e/fishsense-services-processor:{processor_tag}"
        ),
    )
    (out / "compose.rehearsal.yml").write_text(override)

    dc = out / "dc"
    dc.write_text(
        "#!/bin/sh\n"
        "# docker compose on the staged rehearsal (stage.py): the production file,\n"
        "# the rehearsal override, the project dir, and ITS OWN project name.\n"
        f'exec docker compose -p {PROJECT} --project-directory "{project}" '
        f'-f "{out / "compose.yml"}" -f "{out / "compose.rehearsal.yml"}" "$@"\n'
    )
    dc.chmod(dc.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    return dc


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, required=True)
    images = parser.add_mutually_exclusive_group()
    images.add_argument("--version", help="rehearse a release's GHCR images (vX.Y.Z)")
    images.add_argument(
        "--local-images",
        action="store_true",
        help="use fishsense-services-*:rehearsal built locally (docs/cutover.md)",
    )
    parser.add_argument(
        "--values", type=Path, help="TOML: [<openbao path>] field = ..."
    )
    parser.add_argument(
        "--superset", action="store_true", help="COMPOSE_PROFILES=superset"
    )
    args = parser.parse_args(argv)

    values = tomllib.loads(args.values.read_text()) if args.values else {}
    dc = stage(
        args.out,
        values=values,
        version=args.version,
        local_images=args.local_images,
        profiles="superset" if args.superset else "",
    )
    print(
        f"staged: {args.out}\n  {dc} config --quiet   # validate\n  {dc} up -d postgres"
    )
    return 0


if __name__ == "__main__":
    os.umask(0o077)
    sys.exit(main(sys.argv[1:]))
