"""媒体清单质检：把解析后的清单与已登记资源长度进行核对。

校验严格按清单物理行序推进，保证返回的错误是“按清单行序最先出现的问题”。
事件流由两类事件合并而成：

- EXT-X-MAP 声明行：校验初始化段 URI 已登记、BYTERANGE 落在资源内且不与该
  URI 已登记的任何区间（初始化或媒体）重叠；重新声明完全相同的初始化区间
  视为幂等复用，允许通过；
- 媒体片段：未知资源指向 URI 行；区间越界 / 重叠指向 EXT-X-BYTERANGE 行
  （隐式偏移在解析期已解析为同 URI 前一媒体范围的末尾，相关失败同样在
  BYTERANGE 行上报）。

输出按清单顺序给出：媒体序号、epoch、资源 URI、初始化 / 媒体半开字节区间、
累计起止微秒。序号遵循 EXT-X-MEDIA-SEQUENCE（默认 0），epoch 遵循
EXT-X-DISCONTINUITY-SEQUENCE（默认 0），每遇到 EXT-X-DISCONTINUITY 递增。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .hls import ByteRange, Segment, parse_playlist

DEFAULT_MEDIA_SEQUENCE = 0
DEFAULT_DISCONTINUITY_SEQUENCE = 0

KIND_INIT = "init"
KIND_MEDIA = "media"


class AuditError(Exception):
    """质检失败；``line`` 指向清单中最先出现问题的物理行（1 起始）。"""

    def __init__(self, message: str, line: int, code: str):
        super().__init__(message)
        self.message = message
        self.line = line
        self.code = code


@dataclass(frozen=True)
class SegmentResult:
    sequence: int
    epoch: int
    uri: str
    init_start: int
    init_end: int
    media_start: int
    media_end: int
    start_us: int
    end_us: int

    def to_dict(self) -> dict:
        return {
            "sequence": self.sequence,
            "epoch": self.epoch,
            "uri": self.uri,
            "init_range": {"start": self.init_start, "end": self.init_end},
            "media_range": {"start": self.media_start, "end": self.media_end},
            "start_us": self.start_us,
            "end_us": self.end_us,
        }


def audit_playlist(text: str, resource_lengths: dict[str, int]) -> list[SegmentResult]:
    """解析并质检清单，返回每段的核对结果。

    :raises AuditError: 首个按行序出现的资源/区间问题。
    :raises PlaylistError: 清单语法/语义问题（同样映射为 422）。
    """
    plist = parse_playlist(text)
    lengths = dict(resource_lengths)
    # 每个 URI 已占用的（半开区间, 类别）；初始化段与媒体段统一参与重叠检测。
    occupied: dict[str, list[tuple[ByteRange, str]]] = {}

    # 合并事件流：EXT-X-MAP 行与媒体 URI 行在清单中不可能重合，按物理行
    # 排序即得到严格的校验先后顺序。
    events: list[tuple[int, object]] = []
    declared_map_lines: set[int] = set()
    for seg in plist.segments:
        if seg.map_line not in declared_map_lines:
            declared_map_lines.add(seg.map_line)
            events.append((seg.map_line, ("map", seg.map_uri, seg.map_range)))
        events.append((seg.line, ("media", seg)))
    events.sort(key=lambda e: e[0])

    sequence = (
        plist.media_sequence
        if plist.media_sequence is not None
        else DEFAULT_MEDIA_SEQUENCE
    )
    epoch = (
        plist.discontinuity_sequence
        if plist.discontinuity_sequence is not None
        else DEFAULT_DISCONTINUITY_SEQUENCE
    )
    timeline_us = 0
    results: list[SegmentResult] = []

    for event_line, event in events:
        if event[0] == "map":
            _, map_uri, map_range = event
            # MAP 的 URI 与 BYTERANGE 在同一行声明，两类错误都指向该行。
            prior_entries = occupied.get(map_uri, [])
            same_init = any(
                kind == KIND_INIT and rng == map_range
                for rng, kind in prior_entries
            )
            if not same_init:
                _check_registered(
                    map_uri, map_range, lengths, occupied,
                    uri_line=event_line, kind=KIND_INIT,
                )
            continue

        seg: Segment = event[1]
        if seg.discontinuity:
            epoch += 1

        _check_registered(
            seg.uri, seg.media_range, lengths, occupied,
            uri_line=seg.line, range_line=seg.byterange_line,
            kind=KIND_MEDIA, what="媒体片段",
        )

        start_us = timeline_us
        end_us = start_us + seg.duration_us
        timeline_us = end_us

        results.append(
            SegmentResult(
                sequence=sequence,
                epoch=epoch,
                uri=seg.uri,
                init_start=seg.map_range.start,
                init_end=seg.map_range.end,
                media_start=seg.media_range.start,
                media_end=seg.media_range.end,
                start_us=start_us,
                end_us=end_us,
            )
        )
        sequence += 1

    return results


def _check_registered(
    uri: str,
    brange: ByteRange,
    lengths: dict[str, int],
    occupied: dict[str, list[tuple[ByteRange, str]]],
    uri_line: int,
    kind: str,
    range_line: Optional[int] = None,
    what: Optional[str] = None,
) -> None:
    """校验单个区间。

    - 未知资源指向 URI 所在行（``uri_line``）；
    - 越界 / 重叠指向区间声明行（``range_line``），MAP 缺省与 URI 同行。
    """
    if range_line is None:
        range_line = uri_line
    if what is None:
        what = "初始化段" if kind == KIND_INIT else "媒体片段"

    if uri not in lengths:
        raise AuditError(f"未知资源 URI: {uri}", uri_line, "unknown_resource")
    total = lengths[uri]
    if brange.start < 0 or brange.end > total or brange.start >= brange.end:
        raise AuditError(
            f"{what}字节区间 [{brange.start}, {brange.end}) 超出资源 {uri} "
            f"的登记长度 {total}",
            range_line,
            "out_of_bounds",
        )
    for prior, _kind in occupied.get(uri, []):
        if prior.overlaps(brange):
            raise AuditError(
                f"资源 {uri} 的字节区间 [{brange.start}, {brange.end}) 与已登记区间 "
                f"[{prior.start}, {prior.end}) 重叠",
                range_line,
                "overlapping_range",
            )
    occupied.setdefault(uri, []).append((brange, kind))
