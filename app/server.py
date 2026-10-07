"""基于 Python 标准库的 HTTP 入口，提供健康检查与 POST /api/playlists/audit。"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from .audit import AuditError, audit_playlist
from .hls import PlaylistError

MAX_MANIFEST_BYTES = 1 * 1024 * 1024  # 1 MiB（媒体清单字段限额）
MAX_UNIQUE_URIS = 128
# 传输层防 DoS 硬上限：1 MiB 清单 + 资源长度表等请求开销。
MAX_BODY_BYTES = 8 * 1024 * 1024


class BadRequest(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class PayloadTooLarge(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _json_error(handler: BaseHTTPRequestHandler, status: int, code: str,
                message: str, line: Optional[int] = None) -> None:
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    if line is not None:
        body["error"]["line"] = line
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    if status == 422:
        # 明确禁止任何中间层把该错误当作可重试的临时故障。
        handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(raw)


class AuditHandler(BaseHTTPRequestHandler):
    server_version = "MediaQc/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        if os.environ.get("QC_QUIET"):
            return
        super().log_message(fmt, *args)

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] == "/healthz":
            payload = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        _json_error(self, 404, "not_found", "未知路由")

    def do_POST(self) -> None:
        if self.path.split("?", 1)[0] != "/api/playlists/audit":
            _json_error(self, 404, "not_found", "未知路由")
            return
        self._handle_audit()

    def _handle_audit(self) -> None:
        ctype = self.headers.get("Content-Type", "")
        if "application/json" not in ctype:
            _json_error(self, 415, "unsupported_media_type",
                        "必须使用 application/json")
            return

        raw = self._read_body()
        if raw is None:
            return
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            _json_error(self, 400, "invalid_utf8", "请求体必须为 UTF-8 文本")
            return
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as e:
            _json_error(self, 400, "invalid_json", f"JSON 解析失败: {e.msg}")
            return

        try:
            manifest, lengths = load_payload(payload)
        except BadRequest as e:
            _json_error(self, 400, "bad_request", e.message)
            return
        except PayloadTooLarge as e:
            _json_error(self, 413, "payload_too_large", e.message)
            return

        # PlaylistError / AuditError 均以 422 返回，按清单行序报告首个问题。
        try:
            results = audit_playlist(manifest, lengths)
        except PlaylistError as e:
            _json_error(self, 422, "playlist_error", e.message, line=e.line)
            return
        except AuditError as e:
            _json_error(self, 422, e.code, e.message, line=e.line)
            return

        out = {
            "status": "ok",
            "segments": [r.to_dict() for r in results],
            "segment_count": len(results),
        }
        raw_out = json.dumps(out, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw_out)))
        self.end_headers()
        self.wfile.write(raw_out)

    def _read_body(self) -> Optional[bytes]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            _json_error(self, 400, "bad_request", "Content-Length 非法")
            return None
        if length <= 0:
            _json_error(self, 400, "bad_request", "请求体为空")
            return None
        if length > MAX_BODY_BYTES:
            _json_error(self, 413, "payload_too_large",
                        f"请求体超过 {MAX_BODY_BYTES} 字节硬上限")
            return None
        return self.rfile.read(length)


def load_payload(payload: Any) -> tuple[str, dict[str, int]]:
    """校验并归一化请求 JSON。"""
    if not isinstance(payload, dict):
        raise BadRequest("请求体必须为 JSON 对象")

    manifest = payload.get("manifest")
    if not isinstance(manifest, str):
        raise BadRequest("manifest 必须为字符串（UTF-8 媒体清单文本）")
    if len(manifest) == 0:
        raise BadRequest("manifest 为空")
    # 限额按 UTF-8 字节计（len(str) 是码点数，不等于字节数）。
    if len(manifest.encode("utf-8")) > MAX_MANIFEST_BYTES:
        raise PayloadTooLarge(f"manifest 超过 {MAX_MANIFEST_BYTES} 字节限额")

    resources = payload.get("resource_lengths")
    if resources is None:
        raise BadRequest("缺少 resource_lengths")
    if not isinstance(resources, dict):
        raise BadRequest("resource_lengths 必须为 URI->长度 的 JSON 对象")

    lengths: dict[str, int] = {}
    for uri, value in resources.items():
        if not isinstance(uri, str) or not uri:
            raise BadRequest("resource_lengths 的键必须为非空 URI 字符串")
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise BadRequest(f"资源 {uri} 的长度必须为正整数")
        lengths[uri] = value
    if len(lengths) > MAX_UNIQUE_URIS:
        raise BadRequest(f"resource_lengths 最多包含 {MAX_UNIQUE_URIS} 个唯一 URI")

    return manifest, lengths


def build_server(host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), AuditHandler)
    server.daemon_threads = True
    return server


def main() -> None:
    host = os.environ.get("QC_HOST", "0.0.0.0")
    port = int(os.environ.get("QC_PORT", "8080"))
    server = build_server(host, port)
    print(f"media-qc listening on {host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
