"""HTTP API for the playlist audit service.

Endpoints:

* ``POST /api/playlists/audit`` — audit an HLS VOD media playlist.
  Request body (JSON)::

      {
        "playlist": "#EXTM3U\n...",          // UTF-8 media playlist, <= 1 MiB
        "resources": {"video.mp4": 1000000}  // <= 128 unique URIs -> byte length
      }

  Success: ``200`` with the ordered segment timeline.  Failure: ``422``
  with ``{"error": {"code", "message", "line"}}`` describing the first
  problem in playlist line order (``400`` for malformed requests).

* ``GET /healthz`` — health check, always ``200`` when the service is up.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .audit import MAX_PLAYLIST_BYTES, MAX_RESOURCES, AuditError, audit_playlist

# Playlist cap plus room for the JSON envelope and the resources map.
MAX_BODY_BYTES = MAX_PLAYLIST_BYTES + 512 * 1024


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _validate_payload(obj: object) -> tuple[str, dict[str, int]]:
    if not isinstance(obj, dict):
        raise ApiError(400, "invalid_request", "request body must be a JSON object")
    playlist = obj.get("playlist")
    if not isinstance(playlist, str):
        raise ApiError(400, "invalid_request", '"playlist" must be a string')
    resources = obj.get("resources")
    if not isinstance(resources, dict):
        raise ApiError(
            400, "invalid_request", '"resources" must be an object mapping URI to byte length'
        )
    if len(resources) > MAX_RESOURCES:
        raise ApiError(
            422,
            "too_many_resources",
            f"{len(resources)} resources registered; limit is {MAX_RESOURCES} unique URIs",
        )
    for uri, length in resources.items():
        if not isinstance(uri, str) or uri == "":
            raise ApiError(400, "invalid_request", "resource URIs must be non-empty strings")
        if isinstance(length, bool) or not isinstance(length, int):
            raise ApiError(
                400, "invalid_request", f'length of resource "{uri}" must be an integer'
            )
        if length < 0:
            raise ApiError(
                422, "invalid_resource_length", f'length of resource "{uri}" must be non-negative'
            )
    return playlist, resources


def _error_body(code: str, message: str, line: int | None = None) -> dict:
    return {"error": {"code": code, "message": message, "line": line}}


class Handler(BaseHTTPRequestHandler):
    server_version = "PlaylistAudit/1.0"
    protocol_version = "HTTP/1.1"

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _path(self) -> str:
        return self.path.split("?", 1)[0]

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        if self._path() == "/healthz":
            self._send_json(200, {"status": "ok"})
        else:
            self._send_json(404, _error_body("not_found", "unknown endpoint"))

    def do_POST(self) -> None:  # noqa: N802 (http.server naming)
        if self._path() != "/api/playlists/audit":
            self._send_json(404, _error_body("not_found", "unknown endpoint"))
            return
        try:
            content_length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            content_length = 0
        if content_length <= 0:
            self._send_json(400, _error_body("invalid_request", "missing request body"))
            return
        if content_length > MAX_BODY_BYTES:
            self._send_json(
                413, _error_body("request_too_large", "request body exceeds the size limit")
            )
            return
        raw = self.rfile.read(content_length)
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._send_json(
                400, _error_body("invalid_json", f"body must be a UTF-8 JSON document: {exc}")
            )
            return
        try:
            playlist, resources = _validate_payload(obj)
        except ApiError as exc:
            self._send_json(exc.status, _error_body(exc.code, exc.message))
            return
        try:
            result = audit_playlist(playlist, resources)
        except AuditError as exc:
            self._send_json(422, {"error": exc.to_dict()})
            return
        self._send_json(200, result)

    def log_message(self, fmt: str, *args: object) -> None:
        # Keep the default access log on stderr but through print's flushing.
        print(f"{self.address_string()} - {fmt % args}", flush=True)


def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"playlist-audit listening on 0.0.0.0:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
