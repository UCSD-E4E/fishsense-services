"""The e2e stack: the built image under compose, reached over real HTTP.

``compose.yml`` (Postgres → migrate → API) plus ``compose.e2e.yml``, which adds
a stand-in for Authentik's key endpoint and publishes ports on random local
ports. It runs as its own compose project, so it never touches a dev stack,
and it is torn down -- volumes included -- afterwards.

The same tests, pointed at a URL instead of compose, are meant to become the
cutover smoke test (PLAN.md §6.6).
"""

import json
import os
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from sqlalchemy import Engine, create_engine, text

REPO = Path(__file__).resolve().parents[4]
PROJECT = "fishsense-e2e"
ISSUER = "http://fake-idp:8080/application/o/fishsense/"  # as the API sees it
AUDIENCE = "fishsense-web"
KID = "e2e-key"
OWNER = "fishsense_owner:owner-dev-only"  # compose.yml's dev-only owner


@dataclass
class Stack:
    api_url: str
    owner_engine: Engine
    # Kept out of repr: a failing test must never print the environment.
    signing_key: rsa.RSAPrivateKey = field(repr=False)
    env: dict = field(repr=False)

    def compose(self, *args: str, check: bool = True) -> str:
        return compose(*args, env=self.env, check=check)

    def bearer(self, sub: str, key: rsa.RSAPrivateKey | None = None) -> dict:
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": sub,
            "iat": now,
            "exp": now + 300,
        }
        token = jwt.encode(
            claims, key or self.signing_key, algorithm="RS256", headers={"kid": KID}
        )
        return {"Authorization": f"Bearer {token}"}

    def grant(self, sub: str, role: str = "member") -> str:
        """As an admin would: a fresh tenant, and ``sub`` as its member."""
        slug = f"t-{uuid.uuid4().hex[:8]}"
        with self.owner_engine.begin() as conn:
            tenant = conn.execute(
                text("INSERT INTO tenants (slug, name) VALUES (:s, :s) RETURNING id"),
                {"s": slug},
            ).scalar_one()
            user = conn.execute(
                text(
                    "INSERT INTO users (sub) VALUES (:sub) ON CONFLICT (sub) "
                    "DO UPDATE SET sub = excluded.sub RETURNING id"
                ),
                {"sub": sub},
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO memberships (tenant_id, user_id, role) "
                    "VALUES (:t, :u, :r)"
                ),
                {"t": tenant, "u": user, "r": role},
            )
        return slug


def compose(*args: str, env: dict | None = None, check: bool = True) -> str:
    command = [
        "docker", "compose", "-p", PROJECT,
        "-f", str(REPO / "compose.yml"), "-f", str(REPO / "compose.e2e.yml"),
        *args,
    ]  # fmt: skip
    result = subprocess.run(
        command, cwd=REPO, env=env, capture_output=True, text=True, check=False
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed:\n{result.stderr}")
    return result.stdout


def _published(service: str, port: int, env: dict) -> str:
    return compose("port", service, str(port), env=env).strip()


@pytest.fixture(scope="session")
def stack(tmp_path_factory) -> Iterator[Stack]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwks_root = tmp_path_factory.mktemp("idp")
    jwks_dir = jwks_root / "application" / "o" / "fishsense" / "jwks"
    jwks_dir.mkdir(parents=True)
    public = RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    (jwks_dir / "index.html").write_text(
        json.dumps({"keys": [{**public, "kid": KID, "use": "sig", "alg": "RS256"}]})
    )
    jwks_root.chmod(0o755)
    env = {**os.environ, "E2E_JWKS_DIR": str(jwks_root)}

    compose("down", "-v", "--remove-orphans", env=env, check=False)
    compose("up", "--build", "-d", env=env)
    try:
        api_url = f"http://{_published('api', 8000, env)}"
        _wait_healthy(api_url)
        owner = create_engine(
            f"postgresql+psycopg://{OWNER}@{_published('postgres', 5432, env)}/fishsense"
        )
        yield Stack(api_url=api_url, owner_engine=owner, signing_key=key, env=env)
        owner.dispose()
    finally:
        compose("down", "-v", "--remove-orphans", env=env, check=False)


def _wait_healthy(api_url: str, timeout_s: float = 90) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{api_url}/healthz", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise RuntimeError(f"API at {api_url} never became healthy")
