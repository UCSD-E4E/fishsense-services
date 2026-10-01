"""A real local JWKS endpoint for tests, standing in for Authentik's jwks_uri.

It serves the key set only on paths ending in ``/jwks/`` -- anything else is
404 -- so wiring that fetches keys from the wrong URL fails its tests.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm


def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwk(key: rsa.RSAPrivateKey, kid: str) -> dict:
    public = RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    return {**public, "kid": kid, "use": "sig", "alg": "RS256"}


class JwksServer:
    """A JWKS endpoint whose published keys the test can rotate."""

    def __init__(self) -> None:
        self.keys: list[dict] = []
        self.raw_body: bytes | None = None  # served instead of the key set
        self.fetches = 0
        jwks = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if not self.path.endswith("/jwks/"):
                    self.send_error(404)
                    return
                jwks.fetches += 1
                body = jwks.raw_body or json.dumps({"keys": jwks.keys}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self._server.server_port}"
        self.url = f"{self.base_url}/jwks/"

    def __enter__(self) -> "JwksServer":
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()
