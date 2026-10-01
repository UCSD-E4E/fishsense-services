"""Readers for the production deploy's files, shared by the invariant tests.

Each reads a committed file the way its consumer does, or as close as a test
can get without the consumer: compose's YAML, vault-agent's templates in
secrets.nix, the Nix attributes the flake hands mkTenant. Deliberately small
parsers over files this repo writes, not general ones.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
INCUS = REPO / "deploy" / "incus"
COMPOSE = INCUS / "compose.yml"
SECRETS_NIX = INCUS / "secrets.nix"
WORKDIR_NIX = INCUS / "workdir.nix"
FLAKE = REPO / "flake.nix"
FLAKE_LOCK = REPO / "flake.lock"
WORKFLOWS = REPO / ".github" / "workflows"

#: Where vault-agent renders on the slot (krg-infra nixosModules.tenant).
TENANT_RUN = "/run/tenant"

#: The six images one release versions (decision 6).
IMAGES = (
    "fishsense-services-api",
    "fishsense-services-orchestrator",
    "fishsense-services-processor",
    "fishsense-services-processor-gpu",
    "fishsense-services-backup",
    "fishsense-services-web",
)
GHCR = "ghcr.io/ucsd-e4e"
VERSION = re.compile(r"^v\d+\.\d+\.\d+$")


@cache
def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text())


def services() -> dict[str, dict]:
    return compose()["services"]


def converged_services() -> dict[str, dict]:
    """What the slot's `up -d` starts: every service not behind a profile
    (krg composeStack passes none, and compose.env names none by default)."""
    return {n: s for n, s in services().items() if not s.get("profiles")}


def environment(service: dict) -> dict[str, str]:
    env = service.get("environment") or {}
    if isinstance(env, list):
        env = dict(item.split("=", 1) for item in env)
    return {k: "" if v is None else str(v) for k, v in env.items()}


def env_files(service: dict) -> list[str]:
    files = service.get("env_file") or []
    if isinstance(files, str):
        files = [files]
    return [f if isinstance(f, str) else f["path"] for f in files]


def volumes(service: dict) -> list[tuple[str, str]]:
    """(source, target) for every short-syntax volume."""
    found = []
    for volume in service.get("volumes") or []:
        source, target = volume.split(":")[:2]
        found.append((source, target))
    return found


def mounts(service: dict, prefix: str) -> bool:
    return any(
        source == prefix or source.startswith(prefix + "/")
        for source, _ in volumes(service)
    )


def image_version(image: str) -> str | None:
    """The tag of a ``ghcr.io/ucsd-e4e/fishsense-services-*`` image."""
    match = re.fullmatch(
        rf"{re.escape(GHCR)}/(fishsense-services-[a-z-]+):(\S+)", image
    )
    return match.group(2) if match else None


# --- secrets.nix ------------------------------------------------------------------


@dataclass
class Render:
    destination: str
    soft: bool
    contents: str
    #: Variable -> the template that renders it (value with {{ }} left in).
    variables: dict[str, str] = field(default_factory=dict)
    #: Variable -> the (OpenBao path, field) pairs its value reads.
    sources: dict[str, list[tuple[str, str]]] = field(default_factory=dict)


_RENDER = re.compile(
    r'destination\s*=\s*"(?P<dest>[^"]+)";(?P<attrs>.*?)contents\s*=\s*\'\'(?P<body>.*?)\'\';',
    re.S,
)
_WITH = re.compile(r'\{\{\s*with secret "secret/data/tenants/fishsense/([^"]+)"\s*\}\}')
_FIELD = re.compile(r"\.Data\.data\.([a-z_]+)")
_ASSIGN = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")


@cache
def renders() -> dict[str, Render]:
    found = {}
    for match in _RENDER.finditer(SECRETS_NIX.read_text()):
        render = Render(
            destination=match["dest"],
            soft="errorOnMissingKey = false" in match["attrs"],
            contents=match["body"],
        )
        path = None
        for raw in match["body"].splitlines():
            line = raw.strip()
            for opened in _WITH.finditer(line):
                path = opened.group(1)
            line = _WITH.sub("", line)
            line = line.replace("{{ end }}", "")
            assigned = _ASSIGN.match(line)
            if not assigned:
                continue
            name, value = assigned.groups()
            render.variables[name] = value
            render.sources[name] = [(path, f) for f in _FIELD.findall(value)]
        found[render.destination] = render
    return found


def rendered_value(template: str) -> str:
    """A template's value with every ``{{ }}`` replaced by a stand-in, the
    way the render fills it on the slot (hex secrets, so URL-safe)."""
    return re.sub(r"\{\{[^}]*\}\}", "0f0f0f0f", template)


def service_env(service: dict) -> dict[str, str]:
    """What the container sees: its env_file renders, then `environment`
    (compose's precedence)."""
    env: dict[str, str] = {}
    for path in env_files(service):
        render = renders().get(path)
        if render is not None:
            env.update({k: rendered_value(v) for k, v in render.variables.items()})
    env.update(environment(service))
    return env


# --- flake.nix --------------------------------------------------------------------


def flake_attr(name: str) -> str:
    match = re.search(rf"^\s*{re.escape(name)}\s*=\s*([^;]+);", FLAKE.read_text(), re.M)
    assert match, f"flake.nix sets no {name}"
    return match.group(1).strip()


def reload_list() -> list[str]:
    match = re.search(r"reload\s*=\s*\[(.*?)\];", FLAKE.read_text(), re.S)
    assert match, "flake.nix has no temporal.reload"
    # Strip comments before collecting names: they quote service names too.
    body = "\n".join(line.split("#", 1)[0] for line in match.group(1).splitlines())
    return re.findall(r'"([^"]+)"', body)
