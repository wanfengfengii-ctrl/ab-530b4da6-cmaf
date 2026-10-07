"""HTTP 接口测试：进程内启动服务 + urllib 发请求。"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from app.server import MAX_MANIFEST_BYTES, MAX_UNIQUE_URIS, build_server

VALID_PLAYLIST = (
    "#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-TARGETDURATION:6\n"
    '#EXT-X-MAP:URI="v.mp4",BYTERANGE="90@0"\n'
    "#EXTINF:2.0,\n#EXT-X-BYTERANGE:10@90\nv.mp4\n"
    "#EXT-X-ENDLIST\n"
)


def post_json(url: str, body: bytes, headers=None):
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class HttpTests(unittest.TestCase):
    server: ThreadingHTTPServer
    thread: threading.Thread
    base: str

    @classmethod
    def setUpClass(cls):
        cls.server = build_server("127.0.0.1", 0)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_healthz(self):
        with urllib.request.urlopen(self.base + "/healthz", timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(json.loads(resp.read()), {"status": "ok"})

    def test_valid_audit(self):
        body = json.dumps({
            "manifest": VALID_PLAYLIST,
            "resource_lengths": {"v.mp4": 100},
        }).encode()
        status, raw = post_json(self.base + "/api/playlists/audit", body)
        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertEqual(data["segment_count"], 1)
        seg = data["segments"][0]
        self.assertEqual(seg["sequence"], 0)
        self.assertEqual(seg["epoch"], 0)
        self.assertEqual(seg["init_range"], {"start": 0, "end": 90})
        self.assertEqual(seg["media_range"], {"start": 90, "end": 100})
        self.assertEqual(seg["start_us"], 0)
        self.assertEqual(seg["end_us"], 2_000_000)

    def test_unknown_resource_422(self):
        body = json.dumps({
            "manifest": VALID_PLAYLIST,
            "resource_lengths": {"other.mp4": 100},
        }).encode()
        status, raw = post_json(self.base + "/api/playlists/audit", body)
        self.assertEqual(status, 422)
        err = json.loads(raw)["error"]
        self.assertEqual(err["code"], "unknown_resource")
        self.assertEqual(err["line"], 4)

    def test_out_of_bounds_422(self):
        body = json.dumps({
            "manifest": VALID_PLAYLIST,
            "resource_lengths": {"v.mp4": 99},
        }).encode()
        status, raw = post_json(self.base + "/api/playlists/audit", body)
        self.assertEqual(status, 422)
        err = json.loads(raw)["error"]
        self.assertEqual(err["code"], "out_of_bounds")
        self.assertEqual(err["line"], 6)

    def test_missing_endlist_422(self):
        body = json.dumps({
            "manifest": VALID_PLAYLIST.replace("#EXT-X-ENDLIST\n", ""),
            "resource_lengths": {"v.mp4": 100},
        }).encode()
        status, raw = post_json(self.base + "/api/playlists/audit", body)
        self.assertEqual(status, 422)
        self.assertEqual(json.loads(raw)["error"]["code"], "playlist_error")

    def test_overlap_422(self):
        playlist = (
            "#EXTM3U\n#EXT-X-VERSION:7\n"
            '#EXT-X-MAP:URI="v.mp4",BYTERANGE="10@0"\n'
            "#EXTINF:1.0,\n#EXT-X-BYTERANGE:100@10\nv.mp4\n"
            "#EXTINF:1.0,\n#EXT-X-BYTERANGE:50@100\nv.mp4\n"
            "#EXT-X-ENDLIST\n"
        )
        body = json.dumps({
            "manifest": playlist,
            "resource_lengths": {"v.mp4": 5000},
        }).encode()
        status, raw = post_json(self.base + "/api/playlists/audit", body)
        self.assertEqual(status, 422)
        self.assertEqual(json.loads(raw)["error"]["code"], "overlapping_range")

    def test_discontinuity_without_map_422(self):
        playlist = (
            "#EXTM3U\n#EXT-X-VERSION:7\n"
            '#EXT-X-MAP:URI="v.mp4",BYTERANGE="10@0"\n'
            "#EXTINF:1.0,\n#EXT-X-BYTERANGE:10@10\nv.mp4\n"
            "#EXT-X-DISCONTINUITY\n"
            "#EXTINF:1.0,\n#EXT-X-BYTERANGE:10@20\nv.mp4\n"
            "#EXT-X-ENDLIST\n"
        )
        body = json.dumps({
            "manifest": playlist,
            "resource_lengths": {"v.mp4": 5000},
        }).encode()
        status, raw = post_json(self.base + "/api/playlists/audit", body)
        self.assertEqual(status, 422)

    def test_bad_json_400(self):
        status, _ = post_json(self.base + "/api/playlists/audit", b"{not json")
        self.assertEqual(status, 400)

    def test_missing_field_400(self):
        body = json.dumps({"resource_lengths": {}}).encode()
        status, raw = post_json(self.base + "/api/playlists/audit", body)
        self.assertEqual(status, 400)

    def test_bad_resource_length_400(self):
        body = json.dumps({
            "manifest": VALID_PLAYLIST,
            "resource_lengths": {"v.mp4": -1},
        }).encode()
        status, _ = post_json(self.base + "/api/playlists/audit", body)
        self.assertEqual(status, 400)

    def test_too_many_uris_400(self):
        body = json.dumps({
            "manifest": VALID_PLAYLIST,
            "resource_lengths": {f"u{i}.mp4": 100 for i in range(MAX_UNIQUE_URIS + 1)},
        }).encode()
        status, _ = post_json(self.base + "/api/playlists/audit", body)
        self.assertEqual(status, 400)

    def test_manifest_too_large_413(self):
        # 让 manifest 字段本身超过 1 MiB。
        big = "# comment " + "x" * (MAX_MANIFEST_BYTES + 10)
        body = json.dumps({
            "manifest": big,
            "resource_lengths": {},
        }).encode()
        status, _ = post_json(self.base + "/api/playlists/audit", body)
        self.assertEqual(status, 413)

    def test_wrong_content_type_415(self):
        status, _ = post_json(
            self.base + "/api/playlists/audit", b"{}",
            headers={"Content-Type": "text/plain"},
        )
        self.assertEqual(status, 415)

    def test_unknown_route_404(self):
        req = urllib.request.Request(self.base + "/nope")
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("expected 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)


if __name__ == "__main__":
    unittest.main()
