"""HLS 点播清单（VOD / ENDLIST）解析器。

本模块只负责把文本清单解析成结构化的数据，并检查标签本身的语法合法性：
- 只接纳带 #EXT-X-ENDLIST 的点播清单，且必须包含媒体片段；
- 每个媒体片段必须带有正数、微秒精度的 #EXTINF；
- 每个媒体片段必须带有 #EXT-X-BYTERANGE；省略偏移时只能承接同一 URI
  的前一个媒体范围；
- #EXT-X-MAP 对采用字节范围复用的 CMAF 清单为强制项，且其 BYTERANGE 必须
  完整声明（含偏移）；EXT-X-DISCONTINUITY 之后必须重新声明 MAP。

资源长度（Content-Length）相关的未知资源、越界、重叠校验在 :mod:`app.audit`
中完成。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Optional

MAX_SEGMENTS = 2000


class PlaylistError(ValueError):
    """清单语法 / 语义不合法。

    ``line`` 为 1 起始的清单行号（按物理行序，指向最先出现的问题）。
    """

    def __init__(self, message: str, line: int):
        super().__init__(message)
        self.message = message
        self.line = line


@dataclass(frozen=True)
class ByteRange:
    """半开字节区间 [start, end)。"""

    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start

    def overlaps(self, other: "ByteRange") -> bool:
        return self.start < other.end and other.start < self.end


@dataclass
class Segment:
    line: int                 # URI 所在物理行号（1 起始）
    extinf_line: int
    byterange_line: int
    uri: str
    duration_us: int
    media_range: ByteRange
    map_uri: str
    map_range: ByteRange
    map_line: int
    discontinuity: bool = False
    # 序号 / epoch / 时间线在 audit 阶段填充
    sequence: int = 0
    epoch: int = 0
    start_us: int = 0
    end_us: int = 0


@dataclass
class Playlist:
    version: Optional[int] = None
    media_sequence: Optional[int] = None
    discontinuity_sequence: Optional[int] = None
    target_duration: Optional[int] = None
    independent_segments: bool = False
    segments: list[Segment] = field(default_factory=list)


def parse_playlist(text: str) -> Playlist:
    # HTTP 层已保证 UTF-8 解码；这里容错 BOM，并兼容 CRLF / LF / CR。
    if text.startswith("﻿"):
        text = text[1:]
    raw_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    plist = Playlist()
    saw_endlist = False
    endlist_line = 0
    last_content_line = 0

    pending_extinf: Optional[tuple[int, int]] = None          # (line, duration_us)
    pending_byterange: Optional[tuple[int, int, str]] = None  # (br_line, extinf?, value)
    current_map: Optional[tuple[str, ByteRange, int]] = None  # (uri, range, line)
    last_range_by_uri: dict[str, ByteRange] = {}
    discontinuity_pending = False
    saw_segment = False
    saw_extm3u = False
    header_done = False
    has_version = False

    for idx, raw in enumerate(raw_lines, start=1):
        line = raw.strip()
        if not line:
            continue
        last_content_line = idx

        if line.startswith("#"):
            if not line.startswith("#EXT"):
                continue  # 注释行

            if line == "#EXTM3U":
                if saw_extm3u:
                    raise PlaylistError("#EXTM3U 重复出现", idx)
                saw_extm3u = True
                continue
            if not saw_extm3u:
                raise PlaylistError("清单必须以 #EXTM3U 开头", idx)
            if line.startswith("#EXT-X-STREAM-INF") or line.startswith("#EXT-X-I-FRAME-STREAM-INF"):
                raise PlaylistError(
                    "仅接受媒体播放列表，不接受主播放列表（MASTER）", idx
                )
            if line.startswith("#EXTINF:"):
                if saw_endlist:
                    raise PlaylistError("EXTINF 出现在 ENDLIST 之后", idx)
                if pending_extinf is not None:
                    raise PlaylistError(
                        "连续的 EXTINF 之间缺少媒体 URI", idx
                    )
                pending_extinf = (idx, _parse_extinf(line[len("#EXTINF:"):], idx))
                continue
            if line.startswith("#EXT-X-BYTERANGE:"):
                if saw_endlist:
                    raise PlaylistError("EXT-X-BYTERANGE 出现在 ENDLIST 之后", idx)
                if pending_byterange is not None:
                    raise PlaylistError(
                        "同一媒体片段只能声明一个 EXT-X-BYTERANGE", idx
                    )
                pending_byterange = (idx, idx, line[len("#EXT-X-BYTERANGE:"):])
                continue
            if line == "#EXT-X-DISCONTINUITY":
                if saw_endlist:
                    raise PlaylistError(
                        "EXT-X-DISCONTINUITY 出现在 ENDLIST 之后", idx
                    )
                discontinuity_pending = True
                # 旧 MAP 立即失效；必须在后续片段之前用新的 EXT-X-MAP 重建。
                current_map = None
                continue
            if line == "#EXT-X-MAP" or line.startswith("#EXT-X-MAP:"):
                if saw_endlist:
                    raise PlaylistError("EXT-X-MAP 出现在 ENDLIST 之后", idx)
                attrs = _parse_attributes(line, idx)
                uri = attrs.get("URI")
                if uri is None:
                    raise PlaylistError("EXT-X-MAP 缺少 URI 属性", idx)
                br_value = attrs.get("BYTERANGE")
                if br_value is None:
                    raise PlaylistError("EXT-X-MAP 缺少 BYTERANGE 属性", idx)
                map_range = _parse_explicit_byterange(br_value, idx)
                current_map = (uri, map_range, idx)
                continue
            if line == "#EXT-X-ENDLIST":
                if pending_extinf is not None or pending_byterange is not None:
                    raise PlaylistError(
                        "EXTINF/EXT-X-BYTERANGE 之后缺少媒体 URI",
                        pending_extinf[0] if pending_extinf else idx,
                    )
                saw_endlist = True
                endlist_line = idx
                continue
            if line.startswith("#EXT-X-VERSION:"):
                if has_version:
                    raise PlaylistError("EXT-X-VERSION 重复声明", idx)
                value = line[len("#EXT-X-VERSION:"):].strip()
                try:
                    plist.version = int(value)
                except ValueError as e:
                    raise PlaylistError("EXT-X-VERSION 必须为整数", idx) from e
                if plist.version < 1:
                    raise PlaylistError("EXT-X-VERSION 必须为正整数", idx)
                has_version = True
                continue
            if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                if header_done:
                    raise PlaylistError(
                        "EXT-X-MEDIA-SEQUENCE 必须出现在第一个媒体片段之前", idx
                    )
                if plist.media_sequence is not None:
                    raise PlaylistError("EXT-X-MEDIA-SEQUENCE 重复声明", idx)
                try:
                    plist.media_sequence = int(
                        line[len("#EXT-X-MEDIA-SEQUENCE:"):].strip()
                    )
                except ValueError as e:
                    raise PlaylistError(
                        "EXT-X-MEDIA-SEQUENCE 必须为整数", idx
                    ) from e
                continue
            if line.startswith("#EXT-X-DISCONTINUITY-SEQUENCE:"):
                if header_done:
                    raise PlaylistError(
                        "EXT-X-DISCONTINUITY-SEQUENCE 必须出现在第一个媒体片段之前",
                        idx,
                    )
                if plist.discontinuity_sequence is not None:
                    raise PlaylistError(
                        "EXT-X-DISCONTINUITY-SEQUENCE 重复声明", idx
                    )
                try:
                    plist.discontinuity_sequence = int(
                        line[len("#EXT-X-DISCONTINUITY-SEQUENCE:"):].strip()
                    )
                except ValueError as e:
                    raise PlaylistError(
                        "EXT-X-DISCONTINUITY-SEQUENCE 必须为整数", idx
                    ) from e
                continue
            if line.startswith("#EXT-X-TARGETDURATION:"):
                try:
                    plist.target_duration = int(
                        line[len("#EXT-X-TARGETDURATION:"):].strip()
                    )
                except ValueError as e:
                    raise PlaylistError(
                        "EXT-X-TARGETDURATION 必须为整数", idx
                    ) from e
                continue
            if line == "#EXT-X-INDEPENDENT-SEGMENTS":
                plist.independent_segments = True
                continue
            # 其它标签（KEY、PLAYLIST-TYPE、ALLOW-CACHE 等）：忽略。
            continue

        # ---- 媒体 URI 行 ----
        if not saw_extm3u:
            raise PlaylistError("清单必须以 #EXTM3U 开头", idx)
        if saw_endlist:
            raise PlaylistError("媒体 URI 出现在 ENDLIST 之后", idx)
        if pending_extinf is None:
            raise PlaylistError("媒体 URI 之前缺少 EXTINF", idx)
        if pending_byterange is None:
            raise PlaylistError("媒体片段缺少 EXT-X-BYTERANGE", pending_extinf[0])

        extinf_line, duration_us = pending_extinf
        br_line, _, br_value = pending_byterange
        pending_extinf = None
        pending_byterange = None
        header_done = True
        saw_segment = True

        media_range = _resolve_media_range(
            br_value, br_line, line, last_range_by_uri
        )

        if current_map is None:
            if discontinuity_pending:
                raise PlaylistError(
                    "EXT-X-DISCONTINUITY 之后必须重新声明 EXT-X-MAP", idx
                )
            raise PlaylistError("媒体片段缺少当前生效的 EXT-X-MAP", extinf_line)

        if len(plist.segments) >= MAX_SEGMENTS:
            raise PlaylistError(
                f"清单最多包含 {MAX_SEGMENTS} 个媒体片段", idx
            )

        map_uri, map_range, map_line = current_map
        plist.segments.append(
            Segment(
                line=idx,
                extinf_line=extinf_line,
                byterange_line=br_line,
                uri=line,
                duration_us=duration_us,
                media_range=media_range,
                map_uri=map_uri,
                map_range=map_range,
                map_line=map_line,
                discontinuity=discontinuity_pending,
            )
        )
        last_range_by_uri[line] = media_range
        discontinuity_pending = False

    if not saw_segment:
        raise PlaylistError("清单不包含任何媒体片段", endlist_line or last_content_line or 1)
    if not saw_endlist:
        raise PlaylistError("仅接纳带 EXT-X-ENDLIST 的点播清单", last_content_line)
    return plist


def _parse_extinf(value: str, line: int) -> int:
    # EXTINF:<duration>,[<title>]
    head = value.split(",", 1)[0].strip()
    if not head:
        raise PlaylistError("EXTINF 缺少时长", line)
    try:
        duration = Decimal(head)
    except (InvalidOperation, ValueError) as e:
        raise PlaylistError("EXTINF 时长必须为数字", line) from e
    if duration <= 0:
        raise PlaylistError("EXTINF 时长必须为正数", line)
    # 微秒精度：小数点超过 6 位按四舍五入（HALF_UP）截断到微秒。
    duration_us = int(
        (duration * Decimal(1_000_000)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )
    if duration_us <= 0:
        raise PlaylistError("EXTINF 时长必须为正数（微秒精度）", line)
    return duration_us


def _parse_attributes(line: str, lineno: int) -> dict[str, str]:
    # 形如 #EXT-X-MAP:URI="init.mp4",BYTERANGE="120@0"
    if ":" not in line:
        raise PlaylistError(f"{line} 缺少属性列表", lineno)
    body = line.split(":", 1)[1]
    attrs: dict[str, str] = {}
    i, n = 0, len(body)
    while i < n:
        eq = body.find("=", i)
        if eq == -1:
            raise PlaylistError("属性格式非法", lineno)
        name = body[i:eq].strip()
        i = eq + 1
        if i < n and body[i] == '"':
            j = i + 1
            buf: list[str] = []
            while j < n and body[j] != '"':
                buf.append(body[j])
                j += 1
            if j >= n:
                raise PlaylistError(f"属性 {name} 的引号未闭合", lineno)
            attrs[name] = "".join(buf)
            i = j + 1
            if i < n and body[i] == ",":
                i += 1
        else:
            comma = body.find(",", i)
            if comma == -1:
                attrs[name] = body[i:].strip()
                i = n
            else:
                attrs[name] = body[i:comma].strip()
                i = comma + 1
    return attrs


def _parse_explicit_byterange(value: str, line: int) -> ByteRange:
    """解析 MAP 的 BYTERANGE，必须同时给出长度与偏移。"""
    v = value.strip()
    if "@" not in v:
        raise PlaylistError("EXT-X-MAP 的 BYTERANGE 必须显式声明偏移量", line)
    length_s, offset_s = v.split("@", 1)
    length = _parse_int(length_s, "BYTERANGE 长度", line)
    offset = _parse_int(offset_s, "BYTERANGE 偏移", line)
    if length <= 0:
        raise PlaylistError("BYTERANGE 长度必须为正数", line)
    if offset < 0:
        raise PlaylistError("BYTERANGE 偏移不能为负数", line)
    return ByteRange(offset, offset + length)


def _resolve_media_range(
    value: str,
    br_line: int,
    uri: str,
    last_range_by_uri: dict[str, ByteRange],
) -> ByteRange:
    v = value.strip()
    if "@" in v:
        length_s, offset_s = v.split("@", 1)
        length = _parse_int(length_s, "EXT-X-BYTERANGE 长度", br_line)
        offset = _parse_int(offset_s, "EXT-X-BYTERANGE 偏移", br_line)
        if length <= 0:
            raise PlaylistError("EXT-X-BYTERANGE 长度必须为正数", br_line)
        if offset < 0:
            raise PlaylistError("EXT-X-BYTERANGE 偏移不能为负数", br_line)
        return ByteRange(offset, offset + length)

    # 省略偏移：只能承接同一 URI 的前一媒体范围。
    length = _parse_int(v, "EXT-X-BYTERANGE 长度", br_line)
    if length <= 0:
        raise PlaylistError("EXT-X-BYTERANGE 长度必须为正数", br_line)
    prev = last_range_by_uri.get(uri)
    if prev is None:
        raise PlaylistError(
            "省略偏移的 BYTERANGE 缺少同一 URI 的前一媒体范围", br_line
        )
    return ByteRange(prev.end, prev.end + length)


def _parse_int(text: str, what: str, line: int) -> int:
    try:
        return int(text.strip())
    except ValueError as e:
        raise PlaylistError(f"{what} 必须为整数", line) from e
