"""A real Garage for the object-store tests, one per session.

Upload integrity rests on what Garage itself enforces -- a presigned PUT that
signs `x-amz-checksum-sha256` and `content-length` is refused for any other
bytes, and HEAD hands the checksum back -- so these tests use Garage, not an
S3 fake. Each tenant gets its own bucket and its own key, granted on that
bucket only, as in production (docs/handoff/krg-infra).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from testcontainers.core.container import DockerContainer

IMAGE = "dxflrs/garage:v2.1.0"
REGION = "garage"
CONFIG = """\
metadata_dir = "/var/lib/garage/meta"
data_dir = "/var/lib/garage/data"
db_engine = "sqlite"
replication_factor = 1
rpc_bind_addr = "[::]:3901"
rpc_public_addr = "127.0.0.1:3901"
rpc_secret = "{secret}"
[s3_api]
s3_region = "garage"
api_bind_addr = "[::]:3900"
root_domain = ".s3.garage.localhost"
[admin]
api_bind_addr = "[::]:3903"
admin_token = "{admin_token}"
"""
#: The admin API's token (dev_seed.bootstrap_garage drives that API).
ADMIN_TOKEN = "test-only-admin-token"


@dataclass(frozen=True)
class GarageKey:
    bucket: str
    access_key_id: str
    secret_access_key: str


class Garage:
    def __init__(self, config_dir: Path, *, layout: bool = True) -> None:
        #: False leaves the cluster as a fresh install is: no layout, so it
        #: stores nothing until one is applied (dev_seed.bootstrap_garage).
        self._layout = layout
        config = config_dir / "garage.toml"
        config.write_text(CONFIG.format(secret="0" * 63 + "1", admin_token=ADMIN_TOKEN))
        self._container = (
            DockerContainer(IMAGE)
            .with_exposed_ports(3900, 3903)
            .with_volume_mapping(str(config), "/etc/garage.toml", "ro")
        )

    def __enter__(self) -> Garage:
        self._container.start()
        node = self._wait_for_node()
        if self._layout:
            self._garage("layout", "assign", "-z", "dc1", "-c", "1G", node)
            self._garage("layout", "apply", "--version", "1")
        return self

    def __exit__(self, *exc) -> None:
        self._container.stop()

    @property
    def endpoint_url(self) -> str:
        host = self._container.get_container_host_ip()
        return f"http://{host}:{self._container.get_exposed_port(3900)}"

    @property
    def admin_url(self) -> str:
        host = self._container.get_container_host_ip()
        return f"http://{host}:{self._container.get_exposed_port(3903)}"

    def tenant_store(self, bucket: str) -> GarageKey:
        """A bucket and a key granted on it alone."""
        self._garage("bucket", "create", bucket)
        created = self._garage("key", "create", f"{bucket}-key")
        key_id = re.search(r"Key ID:\s*(\S+)", created).group(1)
        secret = re.search(r"Secret key:\s*(\S+)", created).group(1)
        self._garage(
            "bucket", "allow", "--read", "--write", "--owner", bucket, "--key", key_id
        )
        return GarageKey(bucket, key_id, secret)

    def _wait_for_node(self) -> str:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            status = self._garage("status", check=False)
            found = re.search(r"^([0-9a-f]{16})\s", status, re.MULTILINE)
            if found:
                return found.group(1)
            time.sleep(0.5)
        raise TimeoutError("Garage never reported its node")

    def _garage(self, *args: str, check: bool = True) -> str:
        code, output = self._container.exec(["/garage", *args])
        text = output.decode()
        if check and code != 0:
            raise RuntimeError(f"garage {' '.join(args)} failed: {text}")
        return text
