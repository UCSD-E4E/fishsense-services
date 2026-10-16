"""A local stand-in for a self-hosted Photon geocoder (`GET /reverse`).

It answers whatever feature the test sets, records each query, and can be
told to fail or to hang, so the client's handling of an outage is tested over
real HTTP.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


def feature(lat: float, lon: float, **properties) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": properties,
    }


class PhotonServer:
    def __init__(self) -> None:
        self.features: list[dict] = []
        self.status = 200
        self.delay_seconds = 0.0
        self.queries: list[dict[str, str]] = []
        photon = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                url = urlparse(self.path)
                if url.path != "/reverse":
                    self.send_error(404)
                    return
                photon.queries.append({k: v[0] for k, v in parse_qs(url.query).items()})
                time.sleep(photon.delay_seconds)
                body = json.dumps(
                    {"type": "FeatureCollection", "features": photon.features}
                ).encode()
                self.send_response(photon.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self._server.server_port}"

    def __enter__(self) -> "PhotonServer":
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()
