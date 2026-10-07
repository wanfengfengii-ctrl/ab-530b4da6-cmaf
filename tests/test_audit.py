"""audit_playlist 质检逻辑单元测试。"""

import unittest

from app.audit import AuditError, audit_playlist
from app.hls import PlaylistError


def build(segments: list[str], maps: str | None = None,
          media_seq: int | None = None, disc_seq: int | None = None) -> str:
    head = "#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-TARGETDURATION:6\n"
    if media_seq is not None:
        head += f"#EXT-X-MEDIA-SEQUENCE:{media_seq}\n"
    if disc_seq is not None:
        head += f"#EXT-X-DISCONTINUITY-SEQUENCE:{disc_seq}\n"
    head += maps if maps is not None else '#EXT-X-MAP:URI="v.mp4",BYTERANGE="100@0"\n'
    return head + "".join(segments) + "#EXT-X-ENDLIST\n"


def s(uri: str, duration: str = "4.0", brange: str = "1000@100") -> str:
    return f"#EXTINF:{duration},\n#EXT-X-BYTERANGE:{brange}\n{uri}\n"


class AuditHappyPathTests(unittest.TestCase):
    def test_single_segment_output(self):
        [r] = audit_playlist(build([s("v.mp4", "2.5", "1000@100")]),
                             {"v.mp4": 2000})
        d = r.to_dict()
        self.assertEqual(d["sequence"], 0)          # 默认 MEDIA-SEQUENCE
        self.assertEqual(d["epoch"], 0)             # 默认 DISCONTINUITY-SEQUENCE
        self.assertEqual(d["uri"], "v.mp4")
        self.assertEqual(d["init_range"], {"start": 0, "end": 100})
        self.assertEqual(d["media_range"], {"start": 100, "end": 1100})
        self.assertEqual(d["start_us"], 0)
        self.assertEqual(d["end_us"], 2_500_000)

    def test_cumulative_timeline_and_sequences(self):
        text = build([s("v.mp4", "2.0", "10@100"), s("v.mp4", "3.5", "10@110")],
                     media_seq=100)
        rs = audit_playlist(text, {"v.mp4": 500})
        self.assertEqual([r.sequence for r in rs], [100, 101])
        self.assertEqual([(r.start_us, r.end_us) for r in rs],
                         [(0, 2_000_000), (2_000_000, 5_500_000)])

    def test_implicit_offset_contiguous_is_not_overlap(self):
        text = build([s("v.mp4", "1.0", "10@100"), s("v.mp4", "1.0", "10")])
        rs = audit_playlist(text, {"v.mp4": 500})
        self.assertEqual([(r.media_start, r.media_end) for r in rs],
                         [(100, 110), (110, 120)])

    def test_epoch_increments_on_discontinuity(self):
        text = build(
            [
                s("a.mp4", "1.0", "10@100"),
                "#EXT-X-DISCONTINUITY\n"
                '#EXT-X-MAP:URI="b.mp4",BYTERANGE="80@0"\n'
                + s("b.mp4", "1.0", "10@80"),
            ],
            maps='#EXT-X-MAP:URI="a.mp4",BYTERANGE="80@0"\n',
            disc_seq=5,
        )
        rs = audit_playlist(text, {"a.mp4": 500, "b.mp4": 500})
        self.assertEqual([r.epoch for r in rs], [5, 6])

    def test_timeline_continuous_across_discontinuity(self):
        text = build(
            [
                s("a.mp4", "2.0", "10@100"),
                "#EXT-X-DISCONTINUITY\n"
                '#EXT-X-MAP:URI="b.mp4",BYTERANGE="80@0"\n'
                + s("b.mp4", "3.0", "10@80"),
            ],
            maps='#EXT-X-MAP:URI="a.mp4",BYTERANGE="80@0"\n',
        )
        rs = audit_playlist(text, {"a.mp4": 500, "b.mp4": 500})
        self.assertEqual([(r.start_us, r.end_us) for r in rs],
                         [(0, 2_000_000), (2_000_000, 5_000_000)])

    def test_separate_uris_are_independent_ranges(self):
        text = build(
            [s("a.mp4", "1.0", "100@100"), s("b.mp4", "1.0", "100@100")],
            maps='#EXT-X-MAP:URI="init.mp4",BYTERANGE="50@0"\n',
        )
        rs = audit_playlist(text, {"init.mp4": 60, "a.mp4": 300, "b.mp4": 300})
        self.assertEqual(len(rs), 2)
        self.assertTrue(all(r.init_start == 0 and r.init_end == 50 for r in rs))

    def test_identical_map_redeclaration_is_idempotent(self):
        # 跨 discontinuity 重新声明完全相同的初始化区间应被允许。
        text = build(
            [
                s("v.mp4", "1.0", "10@100"),
                "#EXT-X-DISCONTINUITY\n"
                '#EXT-X-MAP:URI="v.mp4",BYTERANGE="100@0"\n'
                + s("v.mp4", "1.0", "10@200"),
            ]
        )
        rs = audit_playlist(text, {"v.mp4": 500})
        self.assertEqual(len(rs), 2)

    def test_end_byte_equal_resource_length_allowed(self):
        # 半开区间：相邻区间不重叠，end == 资源长度合法。
        text = build([s("v.mp4", "1.0", "10@90")],
                     maps='#EXT-X-MAP:URI="v.mp4",BYTERANGE="90@0"\n')
        [r] = audit_playlist(text, {"v.mp4": 100})
        self.assertEqual((r.media_start, r.media_end), (90, 100))


class AuditErrorTests(unittest.TestCase):
    def assertAudit(self, text, lengths, line, code=None):
        with self.assertRaises(AuditError) as cm:
            audit_playlist(text, lengths)
        self.assertEqual(cm.exception.line, line,
                         f"want {line} got {cm.exception.line}: {cm.exception}")
        if code:
            self.assertEqual(cm.exception.code, code)

    def test_unknown_map_uri(self):
        text = build([s("v.mp4")], maps='#EXT-X-MAP:URI="missing.mp4",BYTERANGE="10@0"\n')
        self.assertAudit(text, {"v.mp4": 5000}, 4, "unknown_resource")

    def test_unknown_media_uri_points_to_uri_line(self):
        self.assertAudit(build([s("ghost.mp4")]), {"v.mp4": 5000}, 7,
                         "unknown_resource")

    def test_init_out_of_bounds(self):
        text = build([s("v.mp4")], maps='#EXT-X-MAP:URI="v.mp4",BYTERANGE="100@0"\n')
        self.assertAudit(text, {"v.mp4": 50}, 4, "out_of_bounds")

    def test_media_out_of_bounds_points_to_byterange_line(self):
        text = build([s("v.mp4", "1.0", "1000@100")])
        self.assertAudit(text, {"v.mp4": 200}, 6, "out_of_bounds")

    def test_implicit_offset_overflow(self):
        # 隐式承接后区间越界，错误应在第二个 BYTERANGE 行。
        text = build([s("v.mp4", "1.0", "10@100"), s("v.mp4", "1.0", "1000")])
        self.assertAudit(text, {"v.mp4": 200}, 9, "out_of_bounds")

    def test_overlapping_media_ranges(self):
        text = build([s("v.mp4", "1.0", "100@100"), s("v.mp4", "1.0", "50@120")])
        self.assertAudit(text, {"v.mp4": 5000}, 9, "overlapping_range")

    def test_map_overlaps_media(self):
        # 第二个 MAP（新 URI 同资源）与已有媒体区间重叠。
        text = build(
            [
                s("v.mp4", "1.0", "100@100"),
                "#EXT-X-DISCONTINUITY\n"
                '#EXT-X-MAP:URI="v.mp4",BYTERANGE="80@120"\n'
                + s("v.mp4", "1.0", "100@300"),
            ]
        )
        self.assertAudit(text, {"v.mp4": 5000}, 9, "overlapping_range")

    def test_first_error_by_line_order_map_before_media(self):
        # MAP 行（第 4 行）未知资源；其后媒体片段也有问题，必须先报 MAP 行。
        text = build(
            [s("v.mp4", "1.0", "99999@0")],
            maps='#EXT-X-MAP:URI="nope.mp4",BYTERANGE="10@0"\n',
        )
        self.assertAudit(text, {"v.mp4": 5000}, 4, "unknown_resource")

    def test_first_error_by_line_order_earlier_segment(self):
        # 第一个片段未知资源；第二个片段越界；必须报第一个片段的 URI 行。
        text = build([s("ghost.mp4", "1.0", "10@100"),
                      s("v.mp4", "1.0", "99999@0")])
        self.assertAudit(text, {"v.mp4": 5000}, 7, "unknown_resource")

    def test_parser_error_propagates(self):
        with self.assertRaises(PlaylistError):
            audit_playlist("#EXTM3U\n#EXT-X-ENDLIST\n", {})


if __name__ == "__main__":
    unittest.main()
