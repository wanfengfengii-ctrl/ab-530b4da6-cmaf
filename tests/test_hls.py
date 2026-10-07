"""解析器单元测试。"""

import unittest

from app.hls import MAX_SEGMENTS, PlaylistError, parse_playlist

HEADER = (
    "#EXTM3U\n"
    "#EXT-X-VERSION:7\n"
    "#EXT-X-TARGETDURATION:6\n"
    '#EXT-X-MAP:URI="v.mp4",BYTERANGE="100@0"\n'
)


def seg(uri: str, duration: str = "4.0", brange: str = "1000@100",
        map_line: str | None = None, discontinuity: bool = False) -> str:
    parts = []
    if discontinuity:
        parts.append("#EXT-X-DISCONTINUITY\n")
    if map_line is not None:
        parts.append(map_line + "\n")
    parts.append(f"#EXTINF:{duration},\n")
    parts.append(f"#EXT-X-BYTERANGE:{brange}\n")
    parts.append(f"{uri}\n")
    return "".join(parts)


class ParserHappyPathTests(unittest.TestCase):
    def test_minimal_valid(self):
        text = HEADER + seg("v.mp4") + "#EXT-X-ENDLIST\n"
        plist = parse_playlist(text)
        self.assertEqual(len(plist.segments), 1)
        s = plist.segments[0]
        self.assertEqual((s.media_range.start, s.media_range.end), (100, 1100))
        self.assertEqual((s.map_range.start, s.map_range.end), (0, 100))
        self.assertEqual(s.duration_us, 4_000_000)
        self.assertIsNone(plist.media_sequence)
        self.assertIsNone(plist.discontinuity_sequence)

    def test_implicit_offset_same_uri_chain(self):
        text = (
            HEADER
            + seg("v.mp4", "1.0", "1000@100")
            + seg("v.mp4", "1.0", "500")       # 承接 -> [1100,1600)
            + seg("v.mp4", "1.0", "300")       # 承接 -> [1600,1900)
            + "#EXT-X-ENDLIST\n"
        )
        segs = parse_playlist(text).segments
        self.assertEqual([(s.media_range.start, s.media_range.end) for s in segs],
                         [(100, 1100), (1100, 1600), (1600, 1900)])

    def test_sequences_declared(self):
        text = (
            "#EXTM3U\n#EXT-X-VERSION:7\n"
            "#EXT-X-MEDIA-SEQUENCE:42\n"
            "#EXT-X-DISCONTINUITY-SEQUENCE:7\n"
            + HEADER.split("\n", 1)[1]  # 去掉重复的 EXTM3U/VERSION 头
        )
        # 上面的拼接容易出错，直接显式构造。
        text = (
            "#EXTM3U\n#EXT-X-VERSION:7\n"
            "#EXT-X-MEDIA-SEQUENCE:42\n"
            "#EXT-X-DISCONTINUITY-SEQUENCE:7\n"
            '#EXT-X-MAP:URI="v.mp4",BYTERANGE="100@0"\n'
            + seg("v.mp4")
            + "#EXT-X-ENDLIST\n"
        )
        plist = parse_playlist(text)
        self.assertEqual(plist.media_sequence, 42)
        self.assertEqual(plist.discontinuity_sequence, 7)

    def test_discontinuity_with_new_map(self):
        text = (
            HEADER
            + seg("a.mp4", "1.0", "10@100")
            + seg("b.mp4", "1.0", "10@100",
                  map_line='#EXT-X-MAP:URI="b.mp4",BYTERANGE="80@0"',
                  discontinuity=True)
            + "#EXT-X-ENDLIST\n"
        )
        segs = parse_playlist(text).segments
        self.assertTrue(segs[1].discontinuity)
        self.assertEqual(segs[1].map_uri, "b.mp4")

    def test_microsecond_precision(self):
        text = HEADER + seg("v.mp4", "6.000001") + "#EXT-X-ENDLIST\n"
        self.assertEqual(parse_playlist(text).segments[0].duration_us, 6_000_001)

    def test_microsecond_rounding_half_up(self):
        text = HEADER + seg("v.mp4", "0.0000015") + "#EXT-X-ENDLIST\n"
        self.assertEqual(parse_playlist(text).segments[0].duration_us, 2)

    def test_crlf_and_bom(self):
        text = "﻿" + (HEADER + seg("v.mp4") + "#EXT-X-ENDLIST\r\n").replace("\n", "\r\n")
        self.assertEqual(len(parse_playlist(text).segments), 1)

    def test_max_segments_boundary_allowed(self):
        body = "".join(
            seg("v.mp4", "1.0", f"1@{100 + i}") for i in range(MAX_SEGMENTS)
        )
        text = HEADER + body + "#EXT-X-ENDLIST\n"
        self.assertEqual(len(parse_playlist(text).segments), MAX_SEGMENTS)


class ParserErrorTests(unittest.TestCase):
    def assertErr(self, text: str, line: int, contains: str | None = None):
        with self.assertRaises(PlaylistError) as cm:
            parse_playlist(text)
        self.assertEqual(cm.exception.line, line,
                         f"want line {line}, got {cm.exception.line}: {cm.exception}")
        if contains:
            self.assertIn(contains, cm.exception.message)

    def test_missing_extm3u(self):
        self.assertErr(seg("v.mp4") + "#EXT-X-ENDLIST\n", 1, "EXTM3U")

    def test_master_playlist_rejected(self):
        text = (
            "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1280000\n"
            "child.m3u8\n#EXT-X-ENDLIST\n"
        )
        self.assertErr(text, 2, "主播放列表")

    def test_no_endlist(self):
        self.assertErr(HEADER + seg("v.mp4"), 7, "ENDLIST")

    def test_no_segments(self):
        self.assertErr("#EXTM3U\n#EXT-X-ENDLIST\n", 2, "媒体片段")

    def test_non_positive_extinf(self):
        self.assertErr(HEADER + seg("v.mp4", "0"), 5, "正数")

    def test_negative_extinf(self):
        self.assertErr(HEADER + seg("v.mp4", "-1"), 5, "正数")

    def test_extinf_not_number(self):
        self.assertErr(HEADER + seg("v.mp4", "abc"), 5, "数字")

    def test_missing_byterange(self):
        text = HEADER + "#EXTINF:4.0,\nv.mp4\n#EXT-X-ENDLIST\n"
        self.assertErr(text, 5, "BYTERANGE")

    def test_uri_without_extinf(self):
        text = HEADER + "v.mp4\n#EXT-X-ENDLIST\n"
        self.assertErr(text, 5, "EXTINF")

    def test_implicit_offset_without_prior_same_uri(self):
        # 首个片段在另一个 URI 上使用隐式偏移
        text = HEADER + seg("other.mp4", "1.0", "500") + "#EXT-X-ENDLIST\n"
        self.assertErr(text, 6, "同一 URI")

    def test_implicit_offset_different_uri(self):
        text = (
            HEADER
            + seg("a.mp4", "1.0", "10@100")
            + seg("b.mp4", "1.0", "10")  # b.mp4 无前序范围
            + "#EXT-X-ENDLIST\n"
        )
        self.assertErr(text, 9, "同一 URI")

    def test_map_required(self):
        text = (
            "#EXTM3U\n#EXT-X-VERSION:7\n"
            + seg("v.mp4")
            + "#EXT-X-ENDLIST\n"
        )
        self.assertErr(text, 3, "EXT-X-MAP")

    def test_map_without_byterange(self):
        text = (
            '#EXTM3U\n#EXT-X-MAP:URI="v.mp4"\n'
            + seg("v.mp4").split("\n", 0)[0]
        )
        text = (
            '#EXTM3U\n#EXT-X-MAP:URI="v.mp4"\n'
            "#EXTINF:4.0,\n#EXT-X-BYTERANGE:1000@0\nv.mp4\n#EXT-X-ENDLIST\n"
        )
        self.assertErr(text, 2, "BYTERANGE")

    def test_map_byterange_requires_offset(self):
        text = (
            '#EXTM3U\n#EXT-X-MAP:URI="v.mp4",BYTERANGE="100"\n'
            "#EXTINF:4.0,\n#EXT-X-BYTERANGE:1000@100\nv.mp4\n#EXT-X-ENDLIST\n"
        )
        self.assertErr(text, 2, "偏移")

    def test_discontinuity_without_new_map(self):
        text = (
            HEADER
            + seg("a.mp4", "1.0", "10@100")
            + "#EXT-X-DISCONTINUITY\n"
            + seg("a.mp4", "1.0", "10@200")
            + "#EXT-X-ENDLIST\n"
        )
        self.assertErr(text, 11, "重新声明 EXT-X-MAP")

    def test_too_many_segments(self):
        body = "".join(
            seg("v.mp4", "1.0", f"1@{100 + i}") for i in range(MAX_SEGMENTS + 1)
        )
        text = HEADER + body + "#EXT-X-ENDLIST\n"
        with self.assertRaises(PlaylistError) as cm:
            parse_playlist(text)
        self.assertIn(str(MAX_SEGMENTS), cm.exception.message)

    def test_byterange_non_positive_length(self):
        self.assertErr(HEADER + seg("v.mp4", "1.0", "0@100"), 6, "长度")

    def test_byterange_negative_offset(self):
        self.assertErr(HEADER + seg("v.mp4", "1.0", "10@-1"), 6, "偏移")

    def test_dangling_extinf_before_endlist(self):
        text = HEADER + "#EXTINF:4.0,\n#EXT-X-ENDLIST\n"
        self.assertErr(text, 5, "URI")

    def test_first_error_reported_by_line_order(self):
        # 第一个片段缺少 BYTERANGE；后面还有一个越界构造，必须报前者。
        text = (
            HEADER
            + "#EXTINF:4.0,\nv.mp4\n"
            + seg("v.mp4", "1.0", "10@100")
            + "#EXT-X-ENDLIST\n"
        )
        self.assertErr(text, 5)


if __name__ == "__main__":
    unittest.main()
