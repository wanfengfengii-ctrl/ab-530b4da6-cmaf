"""Audit HLS VOD media playlists that reuse CMAF byte ranges.

The auditor proves, for every media segment in a playlist, that the
referenced byte ranges fall inside registered resources and that the
segments form an unambiguous playback timeline.

Contract enforced here:

* only VOD media playlists (``#EXT-X-ENDLIST`` present) are accepted;
* at most 2000 media segments per playlist;
* every segment needs a positive, microsecond-precision ``EXTINF``, a
  valid ``EXT-X-BYTERANGE`` and a current ``EXT-X-MAP``;
* a ``EXT-X-BYTERANGE`` without an explicit offset may only continue the
  previous media range of the *same* URI;
* ``EXT-X-MAP`` must be re-declared after every ``EXT-X-DISCONTINUITY``;
* every referenced range must be inside its registered resource and must
  not overlap another range of that resource (an init range re-declared
  with identical bounds is the normal CMAF reuse and is allowed);
* media sequence numbers and discontinuity epochs follow the playlist
  declarations (``EXT-X-MEDIA-SEQUENCE`` / ``EXT-X-DISCONTINUITY-SEQUENCE``)
  and default to 0.

The playlist is validated line by line and the first problem encountered
in playlist line order is reported via :class:`AuditError`.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from decimal import Decimal, InvalidOperation

MAX_PLAYLIST_BYTES = 1 << 20  # 1 MiB, UTF-8 encoded
MAX_RESOURCES = 128
MAX_SEGMENTS = 2000
MICROS_PER_SECOND = 1_000_000

_BYTERANGE_RE = re.compile(r"^([0-9]+)(?:@([0-9]+))?$")
_ATTR_RE = re.compile(r'([A-Za-z0-9-]+)=("([^"]*)"|[^,]*)')
_UINT_RE = re.compile(r"^[0-9]+$")

# Tags that only appear in master playlists; seeing one means the payload
# is not a media playlist at all.
_MASTER_ONLY_PREFIXES = (
    "#EXT-X-STREAM-INF",
    "#EXT-X-I-FRAME-STREAM-INF",
    "#EXT-X-MEDIA:",
    "#EXT-X-SESSION-DATA",
    "#EXT-X-SESSION-KEY",
)


class AuditError(Exception):
    """The first playlist problem, in playlist line order.

    ``line`` is the 1-based playlist line number where the problem was
    detected, or ``None`` for problems only detectable at end of input.
    """

    def __init__(self, code: str, message: str, line: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.line = line

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "line": self.line}


class _Intervals:
    """Disjoint half-open ``[start, end)`` intervals of one resource."""

    def __init__(self) -> None:
        self._intervals: list[tuple[int, int]] = []  # sorted by start
        self._init_seen: set[tuple[int, int]] = set()

    def _find_overlap(self, start: int, end: int) -> tuple[int, int] | None:
        idx = bisect_right(self._intervals, (start, end)) - 1
        if idx >= 0 and self._intervals[idx][1] > start:
            return self._intervals[idx]
        if idx + 1 < len(self._intervals) and self._intervals[idx + 1][0] < end:
            return self._intervals[idx + 1]
        return None

    def _insert(self, start: int, end: int) -> None:
        self._intervals.insert(bisect_right(self._intervals, (start, end)), (start, end))

    def add_init(self, start: int, end: int) -> tuple[int, int] | None:
        """Register an init range; identical re-declaration is allowed."""
        if (start, end) in self._init_seen:
            return None
        hit = self._find_overlap(start, end)
        if hit is None:
            self._init_seen.add((start, end))
            self._insert(start, end)
        return hit

    def add_media(self, start: int, end: int) -> tuple[int, int] | None:
        """Register a media range; any overlap with existing ranges fails."""
        hit = self._find_overlap(start, end)
        if hit is None:
            self._insert(start, end)
        return hit


def _parse_byterange(value: str, line: int, code: str = "invalid_byterange") -> tuple[int, int | None]:
    match = _BYTERANGE_RE.match(value)
    if not match:
        raise AuditError(code, f"malformed byte range {value!r}; expected <length>[@<offset>]", line)
    length = int(match.group(1))
    if length < 1:
        raise AuditError(code, f"byte range length must be positive: {value!r}", line)
    offset = int(match.group(2)) if match.group(2) is not None else None
    return length, offset


def _parse_extinf(payload: str, line: int) -> int:
    """Return the EXTINF duration in microseconds (positive, exact)."""
    duration_text = payload.split(",", 1)[0].strip()
    try:
        duration = Decimal(duration_text)
    except InvalidOperation:
        raise AuditError("invalid_extinf", f"malformed EXTINF duration {duration_text!r}", line) from None
    if not duration.is_finite():
        raise AuditError("invalid_extinf", f"EXTINF duration must be finite: {duration_text!r}", line)
    if duration <= 0:
        raise AuditError("invalid_extinf", f"EXTINF duration must be positive: {duration_text!r}", line)
    micros = duration * MICROS_PER_SECOND
    if micros != micros.to_integral_value():
        raise AuditError(
            "invalid_extinf",
            f"EXTINF duration {duration_text!r} is not expressible with microsecond precision",
            line,
        )
    return int(micros)


def _parse_attribute_list(payload: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for match in _ATTR_RE.finditer(payload):
        value = match.group(3) if match.group(3) is not None else match.group(2)
        attrs[match.group(1).upper()] = value
    return attrs


def _parse_uint(value: str, line: int, what: str) -> int:
    text = value.strip()
    if not _UINT_RE.match(text):
        raise AuditError("invalid_playlist", f"{what} must be a non-negative integer: {value!r}", line)
    return int(text)


def _parse_map(
    payload: str,
    line: int,
    resources: dict[str, int],
    intervals: dict[str, _Intervals],
) -> tuple[str, int, int]:
    """Validate an EXT-X-MAP declaration and return (uri, start, end)."""
    attrs = _parse_attribute_list(payload)
    uri = attrs.get("URI")
    if not uri:
        raise AuditError("invalid_map", "EXT-X-MAP requires a non-empty URI attribute", line)
    if uri not in resources:
        raise AuditError("unknown_resource", f'EXT-X-MAP resource "{uri}" is not registered', line)
    resource_length = resources[uri]
    byterange = attrs.get("BYTERANGE")
    if byterange is None:
        # Without a BYTERANGE attribute the init segment is the whole resource.
        start, end = 0, resource_length
    else:
        length, offset = _parse_byterange(byterange, line, code="invalid_map")
        start = offset if offset is not None else 0
        end = start + length
    if end > resource_length:
        raise AuditError(
            "out_of_bounds",
            f'init byte range [{start}, {end}) exceeds length {resource_length} of "{uri}"',
            line,
        )
    hit = intervals.setdefault(uri, _Intervals()).add_init(start, end)
    if hit is not None:
        raise AuditError(
            "overlap",
            f'init byte range [{start}, {end}) overlaps [{hit[0]}, {hit[1]}) of "{uri}"',
            line,
        )
    return uri, start, end


def audit_playlist(playlist: str, resources: dict[str, int]) -> dict:
    """Audit ``playlist`` against registered ``resources`` (URI -> length).

    Returns the ordered segment timeline on success and raises
    :class:`AuditError` with the first problem in playlist line order.
    """
    try:
        encoded_size = len(playlist.encode("utf-8"))
    except UnicodeEncodeError:
        raise AuditError("invalid_playlist", "playlist must be valid UTF-8 text") from None
    if encoded_size > MAX_PLAYLIST_BYTES:
        raise AuditError(
            "playlist_too_large",
            f"playlist is {encoded_size} bytes; limit is {MAX_PLAYLIST_BYTES} bytes (1 MiB)",
        )

    lines = playlist.split("\n")
    first = lines[0]
    if first.startswith("\ufeff"):
        first = first[1:]
    if first.endswith("\r"):
        first = first[:-1]
    if first != "#EXTM3U":
        raise AuditError("invalid_playlist", "first line must be #EXTM3U", 1)

    media_sequence = 0  # EXT-X-MEDIA-SEQUENCE, standard default 0
    epoch = 0  # EXT-X-DISCONTINUITY-SEQUENCE, standard default 0
    current_map: tuple[str, int, int] | None = None
    map_fresh = False  # False once a discontinuity requires a new EXT-X-MAP
    last_media: tuple[str, int] | None = None  # (uri, end) of previous segment
    cumulative_us = 0
    segments: list[dict] = []
    pending_extinf: tuple[int, int] | None = None  # (micros, line)
    pending_byterange: tuple[int, int | None, int] | None = None  # (len, offset, line)
    intervals: dict[str, _Intervals] = {}
    endlist_line: int | None = None
    seen_discontinuity = False

    for lineno, raw in enumerate(lines, start=1):
        if lineno == 1:
            continue  # #EXTM3U, already validated
        line = raw[:-1] if raw.endswith("\r") else raw
        if line == "":
            continue
        if endlist_line is not None:
            raise AuditError(
                "invalid_playlist",
                f"unexpected content after #EXT-X-ENDLIST (line {endlist_line})",
                lineno,
            )

        if line.startswith("#"):
            if line.startswith("#EXTINF:"):
                pending_extinf = (_parse_extinf(line[len("#EXTINF:"):], lineno), lineno)
            elif line.startswith("#EXT-X-BYTERANGE:"):
                length, offset = _parse_byterange(line[len("#EXT-X-BYTERANGE:"):].strip(), lineno)
                pending_byterange = (length, offset, lineno)
            elif line.startswith("#EXT-X-MAP:"):
                current_map = _parse_map(line[len("#EXT-X-MAP:"):], lineno, resources, intervals)
                map_fresh = True
            elif line == "#EXT-X-DISCONTINUITY":
                epoch += 1
                seen_discontinuity = True
                map_fresh = False  # MAP must be re-declared before the next segment
            elif line == "#EXT-X-ENDLIST":
                endlist_line = lineno
            elif line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                if segments:
                    raise AuditError(
                        "invalid_playlist",
                        "EXT-X-MEDIA-SEQUENCE must appear before the first media segment",
                        lineno,
                    )
                media_sequence = _parse_uint(
                    line[len("#EXT-X-MEDIA-SEQUENCE:"):], lineno, "EXT-X-MEDIA-SEQUENCE"
                )
            elif line.startswith("#EXT-X-DISCONTINUITY-SEQUENCE:"):
                if segments or seen_discontinuity:
                    raise AuditError(
                        "invalid_playlist",
                        "EXT-X-DISCONTINUITY-SEQUENCE must appear before the first "
                        "media segment or discontinuity",
                        lineno,
                    )
                epoch = _parse_uint(
                    line[len("#EXT-X-DISCONTINUITY-SEQUENCE:"):], lineno, "EXT-X-DISCONTINUITY-SEQUENCE"
                )
            elif any(line.startswith(prefix) for prefix in _MASTER_ONLY_PREFIXES):
                raise AuditError(
                    "not_media_playlist",
                    f"master playlist tag {line.split(':', 1)[0]} is not allowed; "
                    "only media playlists are audited",
                    lineno,
                )
            # Unrecognized tags and comments are ignored.
            continue

        # --- URI line: assemble and validate one media segment ---
        if len(segments) >= MAX_SEGMENTS:
            raise AuditError(
                "too_many_segments",
                f"playlist exceeds the limit of {MAX_SEGMENTS} media segments",
                lineno,
            )
        if pending_extinf is None:
            raise AuditError("missing_extinf", "media segment has no preceding EXTINF", lineno)
        if pending_byterange is None:
            raise AuditError(
                "missing_byterange", "media segment has no preceding EXT-X-BYTERANGE", lineno
            )
        if current_map is None or not map_fresh:
            raise AuditError(
                "missing_init",
                "media segment has no current EXT-X-MAP; a new EXT-X-MAP must be "
                "declared after each EXT-X-DISCONTINUITY",
                lineno,
            )
        uri = line.strip()
        if uri not in resources:
            raise AuditError("unknown_resource", f'media resource "{uri}" is not registered', lineno)

        length, offset, _ = pending_byterange
        if offset is None:
            if last_media is None or last_media[0] != uri:
                raise AuditError(
                    "invalid_implicit_offset",
                    f'EXT-X-BYTERANGE without offset for "{uri}" does not follow a media '
                    "range of the same resource",
                    lineno,
                )
            start = last_media[1]
        else:
            start = offset
        end = start + length

        resource_length = resources[uri]
        if end > resource_length:
            raise AuditError(
                "out_of_bounds",
                f'media byte range [{start}, {end}) exceeds length {resource_length} of "{uri}"',
                lineno,
            )
        hit = intervals.setdefault(uri, _Intervals()).add_media(start, end)
        if hit is not None:
            raise AuditError(
                "overlap",
                f'media byte range [{start}, {end}) overlaps [{hit[0]}, {hit[1]}) of "{uri}"',
                lineno,
            )

        micros, _ = pending_extinf
        segments.append(
            {
                "media_sequence": media_sequence,
                "epoch": epoch,
                "uri": uri,
                "init": {"uri": current_map[0], "start": current_map[1], "end": current_map[2]},
                "media": {"start": start, "end": end},
                "start_us": cumulative_us,
                "end_us": cumulative_us + micros,
            }
        )
        media_sequence += 1
        cumulative_us += micros
        last_media = (uri, end)
        pending_extinf = None
        pending_byterange = None

    if pending_extinf is not None or pending_byterange is not None:
        raise AuditError(
            "invalid_playlist", "dangling EXTINF/EXT-X-BYTERANGE without a media segment URI"
        )
    if endlist_line is None:
        raise AuditError(
            "not_vod", "playlist has no #EXT-X-ENDLIST; only VOD playlists are audited"
        )

    return {
        "segment_count": len(segments),
        "total_duration_us": cumulative_us,
        "segments": segments,
    }
