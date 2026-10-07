"""Unit tests for the playlist audit core and the HTTP API."""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from app.audit import MAX_SEGMENTS, AuditError, audit_playlist
from app.main import Handler

VOD_HEADER = "#EXTM3U\n#EXT-X-TARGETDURATION:2\n"


def make_playlist(body: str) -> str:
    return VOD_HEADER + body


class AuditTestCase(unittest.TestCase):
    def assert_audit_error(self, playlist, resources, code, line=None):
        with self.assertRaises(AuditError) as ctx:
            audit_playlist(playlist, resources)
        self.assertEqual(ctx.exception.code, code)
        if line is not None:
            self.assertEqual(ctx.exception.line, line)
        return ctx.exception


class ValidPlaylistTests(AuditTestCase):
    def test_single_file_cmaf_with_implicit_offsets(self):
        playlist = make_playlist(
            '#EXT-X-MAP:URI="video.mp4",BYTERANGE="824@0"\n'
            "#EXTINF:2.000000,\n"
            "#EXT-X-BYTERANGE:150000@824\n"
            "video.mp4\n"
            "#EXTINF:1.500000,\n"
            "#EXT-X-BYTERANGE:140000\n"
            "video.mp4\n"
            "#EXT-X-ENDLIST\n"
        )
        result = audit_playlist(playlist, {"video.mp4": 1_000_000})
        self.assertEqual(result["segment_count"], 2)
        self.assertEqual(result["total_duration_us"], 3_500_000)
        first, second = result["segments"]
        self.assertEqual(first["media_sequence"], 0)
        self.assertEqual(first["epoch"], 0)
        self.assertEqual(first["uri"], "video.mp4")
        self.assertEqual(first["init"], {"uri": "video.mp4", "start": 0, "end": 824})
        self.assertEqual(first["media"], {"start": 824, "end": 150824})
        self.assertEqual(first["start_us"], 0)
        self.assertEqual(first["end_us"], 2_000_000)
        self.assertEqual(second["media_sequence"], 1)
        self.assertEqual(second["media"], {"start": 150824, "end": 290824})
        self.assertEqual(second["start_us"], 2_000_000)
        self.assertEqual(second["end_us"], 3_500_000)

    def test_declared_sequence_numbers_are_honored(self):
        playlist = make_playlist(
            "#EXT-X-MEDIA-SEQUENCE:42\n"
            "#EXT-X-DISCONTINUITY-SEQUENCE:7\n"
            '#EXT-X-MAP:URI="v.mp4",BYTERANGE="100@0"\n'
            "#EXTINF:1.0,\n"
            "#EXT-X-BYTERANGE:100@100\n"
            "v.mp4\n"
            "#EXT-X-DISCONTINUITY\n"
            '#EXT-X-MAP:URI="v.mp4",BYTERANGE="100@0"\n'
            "#EXTINF:1.0,\n"
            "#EXT-X-BYTERANGE:100@200\n"
            "v.mp4\n"
            "#EXT-X-ENDLIST\n"
        )
        result = audit_playlist(playlist, {"v.mp4": 1000})
        first, second = result["segments"]
        self.assertEqual((first["media_sequence"], first["epoch"]), (42, 7))
        self.assertEqual((second["media_sequence"], second["epoch"]), (43, 8))

    def test_discontinuity_with_redeclared_identical_map_is_allowed(self):
        playlist = make_playlist(
            '#EXT-X-MAP:URI="v.mp4",BYTERANGE="100@0"\n'
            "#EXTINF:1.0,\n"
            "#EXT-X-BYTERANGE:100@100\n"
            "v.mp4\n"
            "#EXT-X-DISCONTINUITY\n"
            '#EXT-X-MAP:URI="v.mp4",BYTERANGE="100@0"\n'
            "#EXTINF:1.0,\n"
            "#EXT-X-BYTERANGE:100@200\n"
            "v.mp4\n"
            "#EXT-X-ENDLIST\n"
        )
        result = audit_playlist(playlist, {"v.mp4": 1000})
        self.assertEqual([s["epoch"] for s in result["segments"]], [0, 1])

    def test_map_without_byterange_covers_whole_resource(self):
        playlist = make_playlist(
            '#EXT-X-MAP:URI="init.mp4"\n'
            "#EXTINF:2.0,\n"
            "#EXT-X-BYTERANGE:100@0\n"
            "seg.mp4\n"
            "#EXT-X-ENDLIST\n"
        )
        result = audit_playlist(playlist, {"init.mp4": 700, "seg.mp4": 5000})
        self.assertEqual(
            result["segments"][0]["init"], {"uri": "init.mp4", "start": 0, "end": 700}
        )

    def test_map_byterange_without_offset_defaults_to_zero(self):
        playlist = make_playlist(
            '#EXT-X-MAP:URI="v.mp4",BYTERANGE="100"\n'
            "#EXTINF:2.0,\n"
            "#EXT-X-BYTERANGE:100@100\n"
            "v.mp4\n"
            "#EXT-X-ENDLIST\n"
        )
        result = audit_playlist(playlist, {"v.mp4": 1000})
        self.assertEqual(result["segments"][0]["init"], {"uri": "v.mp4", "start": 0, "end": 100})

    def test_extinf_microsecond_precision_boundaries(self):
        for duration, micros in (("2", 2_000_000), ("2.5", 2_500_000), ("0.000001", 1)):
            playlist = make_playlist(
                '#EXT-X-MAP:URI="v.mp4",BYTERANGE="10@0"\n'
                f"#EXTINF:{duration},\n"
                "#EXT-X-BYTERANGE:10@10\n"
                "v.mp4\n"
                "#EXT-X-ENDLIST\n"
            )
            result = audit_playlist(playlist, {"v.mp4": 100})
            self.assertEqual(result["segments"][0]["end_us"], micros)

    def test_crlf_line_endings_and_bom_are_accepted(self):
        playlist = (
            "\ufeff#EXTM3U\r\n"
            '#EXT-X-MAP:URI="v.mp4",BYTERANGE="10@0"\r\n'
            "#EXTINF:1.0,\r\n"
            "#EXT-X-BYTERANGE:10@10\r\n"
            "v.mp4\r\n"
            "#EXT-X-ENDLIST\r\n"
        )
        result = audit_playlist(playlist, {"v.mp4": 100})
        self.assertEqual(result["segment_count"], 1)

    def test_max_segments_boundary(self):
        parts = ["#EXTM3U", '#EXT-X-MAP:URI="v.mp4",BYTERANGE="1@0"']
        for i in range(MAX_SEGMENTS):
            parts += ["#EXTINF:1.0,", f"#EXT-X-BYTERANGE:1@{i + 1}", "v.mp4"]
        parts.append("#EXT-X-ENDLIST")
        result = audit_playlist("\n".join(parts), {"v.mp4": MAX_SEGMENTS + 10})
        self.assertEqual(result["segment_count"], MAX_SEGMENTS)


class RejectionTests(AuditTestCase):
    def test_missing_endlist_is_not_vod(self):
        playlist = make_playlist(
            '#EXT-X-MAP:URI="v.mp4",BYTERANGE="10@0"\n'
            "#EXTINF:1.0,\n"
            "#EXT-X-BYTERANGE:10@10\n"
            "v.mp4\n"
        )
        self.assert_audit_error(playlist, {"v.mp4": 100}, "not_vod")

    def test_missing_extm3u(self):
        self.assert_audit_error("#EXTINF:1.0,\n", {}, "invalid_playlist", line=1)

    def test_master_playlist_is_rejected(self):
        playlist = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nv.m3u8\n"
        self.assert_audit_error(playlist, {}, "not_media_playlist", line=2)

    def test_extinf_must_be_positive(self):
        for bad in ("0", "0.000000", "-1.5"):
            playlist = make_playlist(
                '#EXT-X-MAP:URI="v.mp4",BYTERANGE="10@0"\n'
                f"#EXTINF:{bad},\n"
                "#EXT-X-BYTERANGE:10@10\n"
                "v.mp4\n"
                "#EXT-X-ENDLIST\n"
            )
            self.assert_audit_error(playlist, {"v.mp4": 100}, "invalid_extinf", line=4)

    def test_extinf_must_have_microsecond_precision(self):
        playlist = make_playlist(
            '#EXT-X-MAP:URI="v.mp4",BYTERANGE="10@0"\n'
            "#EXTINF:2.0000001,\n"
            "#EXT-X-BYTERANGE:10@10\n"
            "v.mp4\n"
            "#EXT-X-ENDLIST\n"
        )
        self.assert_audit_error(playlist, {"v.mp4": 100}, "invalid_extinf", line=4)

    def test_extinf_must_be_a_number(self):
        playlist = make_playlist(
            '#EXT-X-MAP:URI="v.mp4",BYTERANGE="10@0"\n'
            "#EXTINF:abc,\n"
            "#EXT-X-BYTERANGE:10@10\n"
            "v.mp4\n"
            "#EXT-X-ENDLIST\n"
        )
        self.assert_audit_error(playlist, {"v.mp4": 100}, "invalid_extinf", line=4)

    def test_missing_extinf(self):
        playlist = make_playlist(
            '#EXT-X-MAP:URI="v.mp4",BYTERANGE="10@0"\n'
            "#EXT-X-BYTERANGE:10@10\n"
            "v.mp4\n"
            "#EXT-X-ENDLIST\n"
        )
        self.assert_audit_error(playlist, {"v.mp4": 100}, "missing_extinf", line=5)

    def test_missing_byterange(self):
        playlist = make_playlist(
            '#EXT-X-MAP:URI="v.mp4",BYTERANGE="10@0"\n' "#EXTINF:1.0,\n" "v.mp4\n" "#EXT-X-ENDLIST\n"
        )
        self.assert_audit_error(playlist, {"v.mp4": 100}, "missing_byterange", line=5)

    def test_malformed_byterange(self):
        playlist = make_playlist(
            '#EXT-X-MAP:URI="v.mp4",BYTERANGE="10@0"\n'
            "#EXTINF:1.0,\n"
            "#EXT-X-BYTERANGE:abc\n"
            "v.mp4\n"
            "#EXT-X-ENDLIST\n"
        )
        self.assert_audit_error(playlist, {"v.mp4": 100}, "invalid_byterange", line=5)

    def test_zero_length_byterange(self):
        playlist = make_playlist(
            '#EXT-X-MAP:URI="v.mp4",BYTERANGE="10@0"\n'
            "#EXTINF:1.0,\n"
            "#EXT-X-BYTERANGE:0@10\n"
            "v.mp4\n"
            "#EXT-X-ENDLIST\n"
        )
        self.assert_audit_error(playlist, {"v.mp4": 100}, "invalid_byterange", line=5)

    def test_missing_map_entirely(self):
        playlist = make_playlist("#EXTINF:1.0,\n#EXT-X-BYTERANGE:10@0\nv.mp4\n#EXT-X-ENDLIST\n")
        self.assert_audit_error(playlist, {"v.mp4": 100}, "missing_init", line=5)

    def test_discontinuity_requires_new_map(self):
        playlist = make_playlist(
            '#EXT-X-MAP:URI="v.mp4",BYTERANGE="10@0"\n'
            "#EXTINF:1.0,\n"
            "#EXT-X-BYTERANGE:10@10\n"
            "v.mp4\n"
            "#EXT-X-DISCONTINUITY\n"
            "#EXTINF:1.0,\n"
            "#EXT-X-BYTERANGE:10@20\n"
            "v.mp4\n"
            "#EXT-X-ENDLIST\n"
        )
        self.assert_audit_error(playlist, {"v.mp4": 100}, "missing_init", line=10)

    def test_unknown_media_resource(self):
        playlist = make_playlist(
            '#EXT-X-MAP:URI="v.mp4",BYTERANGE="10@0"\n'
            "#EXTINF:1.0,\n"
            "#EXT-X-BYTERANGE:10@10\n"
            "other.mp4\n"
            "#EXT-X-ENDLIST\n"
        )
        self.assert_audit_error(playlist, {"v.mp4": 100}, "unknown_resource", line=6)

    def test_unknown_map_resource(self):
        playlist = make_playlist(
            '#EXT-X-MAP:URI="missing.mp4",BYTERANGE="10@0"\n'
            "#EXTINF:1.0,\n"
            "#EXT-X-BYTERANGE:10@10\n"
            "v.mp4\n"
            "#EXT-X-ENDLIST\n"
        )
        self.assert_audit_error(playlist, {"v.mp4": 100}, "unknown_resource", line=3)

    def test_media_range_out_of_bounds(self):
        playlist = make_playlist(
            '#EXT-X-MAP:URI="v.mp4",BYTERANGE="10@0"\n'
            "#EXTINF:1.0,\n"
            "#EXT-X-BYTERANGE:10@95\n"
            "v.mp4\n"
            "#EXT-X-ENDLIST\n"
        )
        self.assert_audit_error(playlist, {"v.mp4": 100}, "out_of_bounds", line=6)

    def test_map_range_out_of_bounds(self):
        playlist = make_playlist(
            '#EXT-X-MAP:URI="v.mp4",BYTERANGE="10@95"\n'
            "#EXTINF:1.0,\n"
            "#EXT-X-BYTERANGE:10@10\n"
            "v.mp4\n"
            "#EXT-X-ENDLIST\n"
        )
        self.assert_audit_error(playlist, {"v.mp4": 100}, "out_of_bounds", line=3)

    def test_media_range_overlapping_init(self):
        playlist = make_playlist(
            '#EXT-X-MAP:URI="v.mp4",BYTERANGE="100@0"\n'
            "#EXTINF:1.0,\n"
            "#EXT-X-BYTERANGE:50@50\n"
            "v.mp4\n"
            "#EXT-X-ENDLIST\n"
        )
        self.assert_audit_error(playlist, {"v.mp4": 1000}, "overlap", line=6)

    def test_media_ranges_overlapping_each_other(self):
        playlist = make_playlist(
            '#EXT-X-MAP:URI="v.mp4",BYTERANGE="10@0"\n'
            "#EXTINF:1.0,\n"
            "#EXT-X-BYTERANGE:100@10\n"
            "v.mp4\n"
            "#EXTINF:1.0,\n"
            "#EXT-X-BYTERANGE:100@50\n"
            "v.mp4\n"
            "#EXT-X-ENDLIST\n"
        )
        self.assert_audit_error(playlist, {"v.mp4": 1000}, "overlap", line=9)

    def test_identical_media_range_is_overlap(self):
        playlist = make_playlist(
            '#EXT-X-MAP:URI="v.mp4",BYTERANGE="10@0"\n'
            "#EXTINF:1.0,\n"
            "#EXT-X-BYTERANGE:100@10\n"
            "v.mp4\n"
            "#EXTINF:1.0,\n"
            "#EXT-X-BYTERANGE:100@10\n"
            "v.mp4\n"
            "#EXT-X-ENDLIST\n"
        )
        self.assert_audit_error(playlist, {"v.mp4": 1000}, "overlap", line=9)

    def test_overlapping_map_declarations(self):
        playlist = make_playlist(
            '#EXT-X-MAP:URI="v.mp4",BYTERANGE="100@0"\n'
            '#EXT-X-MAP:URI="v.mp4",BYTERANGE="100@50"\n'
            "#EXTINF:1.0,\n"
            "#EXT-X-BYTERANGE:10@200\n"
            "v.mp4\n"
            "#EXT-X-ENDLIST\n"
        )
        self.assert_audit_error(playlist, {"v.mp4": 1000}, "overlap", line=4)

    def test_implicit_offset_without_previous_segment(self):
        playlist = make_playlist(
            '#EXT-X-MAP:URI="v.mp4",BYTERANGE="10@0"\n'
            "#EXTINF:1.0,\n"
            "#EXT-X-BYTERANGE:10\n"
            "v.mp4\n"
            "#EXT-X-ENDLIST\n"
        )
        self.assert_audit_error(playlist, {"v.mp4": 100}, "invalid_implicit_offset", line=6)

    def test_implicit_offset_with_different_previous_uri(self):
        playlist = make_playlist(
            '#EXT-X-MAP:URI="v.mp4",BYTERANGE="10@0"\n'
            "#EXTINF:1.0,\n"
            "#EXT-X-BYTERANGE:10@10\n"
            "v.mp4\n"
            "#EXTINF:1.0,\n"
            "#EXT-X-BYTERANGE:10\n"
            "w.mp4\n"
            "#EXT-X-ENDLIST\n"
        )
        self.assert_audit_error(
            playlist, {"v.mp4": 100, "w.mp4": 100}, "invalid_implicit_offset", line=9
        )

    def test_first_problem_in_line_order_is_reported(self):
        # The out-of-bounds range on line 5 must be reported, not the
        # unknown resource on line 8.
        playlist = make_playlist(
            '#EXT-X-MAP:URI="a.mp4",BYTERANGE="10@0"\n'
            "#EXTINF:1.0,\n"
            "#EXT-X-BYTERANGE:5@100\n"
            "a.mp4\n"
            "#EXTINF:1.0,\n"
            "#EXT-X-BYTERANGE:5@200\n"
            "b.mp4\n"
            "#EXT-X-ENDLIST\n"
        )
        self.assert_audit_error(playlist, {"a.mp4": 50}, "out_of_bounds", line=6)

    def test_too_many_segments(self):
        parts = ["#EXTM3U", '#EXT-X-MAP:URI="v.mp4",BYTERANGE="1@0"']
        for i in range(MAX_SEGMENTS + 1):
            parts += ["#EXTINF:1.0,", f"#EXT-X-BYTERANGE:1@{i + 1}", "v.mp4"]
        parts.append("#EXT-X-ENDLIST")
        self.assert_audit_error("\n".join(parts), {"v.mp4": MAX_SEGMENTS + 10}, "too_many_segments")

    def test_playlist_too_large(self):
        self.assert_audit_error(" " * ((1 << 20) + 1), {}, "playlist_too_large")

    def test_content_after_endlist(self):
        playlist = make_playlist(
            '#EXT-X-MAP:URI="v.mp4",BYTERANGE="10@0"\n'
            "#EXTINF:1.0,\n"
            "#EXT-X-BYTERANGE:10@10\n"
            "v.mp4\n"
            "#EXT-X-ENDLIST\n"
            "#EXTINF:1.0,\n"
        )
        self.assert_audit_error(playlist, {"v.mp4": 100}, "invalid_playlist", line=8)

    def test_dangling_extinf(self):
        playlist = make_playlist('#EXT-X-MAP:URI="v.mp4",BYTERANGE="10@0"\n#EXTINF:1.0,\n')
        self.assert_audit_error(playlist, {"v.mp4": 100}, "invalid_playlist")

    def test_media_sequence_after_first_segment(self):
        playlist = make_playlist(
            '#EXT-X-MAP:URI="v.mp4",BYTERANGE="10@0"\n'
            "#EXTINF:1.0,\n"
            "#EXT-X-BYTERANGE:10@10\n"
            "v.mp4\n"
            "#EXT-X-MEDIA-SEQUENCE:5\n"
            "#EXT-X-ENDLIST\n"
        )
        self.assert_audit_error(playlist, {"v.mp4": 100}, "invalid_playlist", line=7)


class HttpApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def post(self, payload, raw=None):
        data = raw if raw is not None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base + "/api/playlists/audit",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_healthz(self):
        with urllib.request.urlopen(self.base + "/healthz", timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(json.loads(resp.read()), {"status": "ok"})

    def test_unknown_endpoint(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.base + "/nope", timeout=5)
        self.assertEqual(ctx.exception.code, 404)

    def test_valid_playlist_returns_200(self):
        playlist = make_playlist(
            '#EXT-X-MAP:URI="v.mp4",BYTERANGE="10@0"\n'
            "#EXTINF:1.0,\n"
            "#EXT-X-BYTERANGE:10@10\n"
            "v.mp4\n"
            "#EXT-X-ENDLIST\n"
        )
        status, body = self.post({"playlist": playlist, "resources": {"v.mp4": 100}})
        self.assertEqual(status, 200)
        self.assertEqual(body["segment_count"], 1)

    def test_out_of_bounds_returns_422_with_line(self):
        playlist = make_playlist(
            '#EXT-X-MAP:URI="v.mp4",BYTERANGE="10@0"\n'
            "#EXTINF:1.0,\n"
            "#EXT-X-BYTERANGE:10@95\n"
            "v.mp4\n"
            "#EXT-X-ENDLIST\n"
        )
        status, body = self.post({"playlist": playlist, "resources": {"v.mp4": 100}})
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "out_of_bounds")
        self.assertEqual(body["error"]["line"], 6)

    def test_malformed_json_returns_400(self):
        status, body = self.post(None, raw=b"{not json")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_missing_fields_return_400(self):
        status, _ = self.post({})
        self.assertEqual(status, 400)

    def test_too_many_resources_returns_422(self):
        resources = {f"r{i}.mp4": 1 for i in range(129)}
        status, body = self.post({"playlist": "#EXTM3U\n", "resources": resources})
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "too_many_resources")

    def test_negative_resource_length_returns_422(self):
        status, body = self.post({"playlist": "#EXTM3U\n", "resources": {"v.mp4": -1}})
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_resource_length")

    def test_non_integer_resource_length_returns_400(self):
        status, _ = self.post({"playlist": "#EXTM3U\n", "resources": {"v.mp4": "100"}})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
