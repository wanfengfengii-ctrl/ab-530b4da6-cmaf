"""One-shot verification for the playlist audit service.

Waits for the API to become ready, then runs, in order:

1. code tests   — the unit test suite in ``tests/``;
2. build check  — byte-compilation of ``app/`` and ``tests/``;
3. API smoke    — a valid playlist (200, exact timeline) and an
   out-of-bounds playlist (422 ``out_of_bounds``), plus unknown-resource
   and missing-init rejections, against the live API.

Every check is reported on stdout and the process exit code summarizes
the outcome: 0 when all checks pass, 1 otherwise.
"""

from __future__ import annotations

import compileall
import io
import json
import os
import sys
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API_BASE_URL = os.environ.get("API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
READY_TIMEOUT_SECONDS = float(os.environ.get("VERIFY_READY_TIMEOUT", "60"))

VALID_PLAYLIST = """\
#EXTM3U
#EXT-X-VERSION:7
#EXT-X-TARGETDURATION:2
#EXT-X-PLAYLIST-TYPE:VOD
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-MAP:URI="video.mp4",BYTERANGE="824@0"
#EXTINF:2.000000,
#EXT-X-BYTERANGE:150000@824
video.mp4
#EXTINF:2.000000,
#EXT-X-BYTERANGE:142000
video.mp4
#EXT-X-ENDLIST
"""

OUT_OF_BOUNDS_PLAYLIST = """\
#EXTM3U
#EXT-X-TARGETDURATION:2
#EXT-X-MAP:URI="video.mp4",BYTERANGE="824@0"
#EXTINF:2.000000,
#EXT-X-BYTERANGE:150000@900000
video.mp4
#EXT-X-ENDLIST
"""

UNKNOWN_RESOURCE_PLAYLIST = """\
#EXTM3U
#EXT-X-TARGETDURATION:2
#EXT-X-MAP:URI="video.mp4",BYTERANGE="824@0"
#EXTINF:2.000000,
#EXT-X-BYTERANGE:1000@824
other.mp4
#EXT-X-ENDLIST
"""

MISSING_INIT_PLAYLIST = """\
#EXTM3U
#EXT-X-TARGETDURATION:2
#EXT-X-MAP:URI="video.mp4",BYTERANGE="824@0"
#EXTINF:2.000000,
#EXT-X-BYTERANGE:1000@824
video.mp4
#EXT-X-DISCONTINUITY
#EXTINF:2.000000,
#EXT-X-BYTERANGE:1000@1824
video.mp4
#EXT-X-ENDLIST
"""

RESOURCES = {"video.mp4": 1_000_000}


def wait_ready() -> bool:
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(API_BASE_URL + "/healthz", timeout=2) as resp:
                if resp.status == 200:
                    return True
        except OSError:
            pass
        time.sleep(0.5)
    return False


def run_unit_tests() -> tuple[bool, str, str]:
    sys.path.insert(0, ROOT)
    suite = unittest.TestLoader().discover(os.path.join(ROOT, "tests"))
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=1).run(suite)
    summary = (
        f"{result.testsRun} tests, {len(result.failures)} failures, "
        f"{len(result.errors)} errors"
    )
    return result.wasSuccessful(), summary, stream.getvalue()


def run_build_check() -> tuple[bool, str]:
    ok = compileall.compile_dir(os.path.join(ROOT, "app"), quiet=1)
    ok = compileall.compile_dir(os.path.join(ROOT, "tests"), quiet=1) and ok
    return ok, "byte-compile of app/ and tests/"


def _post_audit(playlist: str, resources: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        API_BASE_URL + "/api/playlists/audit",
        data=json.dumps({"playlist": playlist, "resources": resources}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def run_smoke() -> list[tuple[str, bool, str]]:
    checks: list[tuple[str, bool, str]] = []

    status, body = _post_audit(VALID_PLAYLIST, RESOURCES)
    detail = json.dumps(body)
    ok = status == 200
    if ok:
        segments = body.get("segments", [])
        ok = (
            body.get("segment_count") == 2
            and body.get("total_duration_us") == 4_000_000
            and len(segments) == 2
            and segments[0]["media_sequence"] == 0
            and segments[0]["epoch"] == 0
            and segments[0]["init"] == {"uri": "video.mp4", "start": 0, "end": 824}
            and segments[0]["media"] == {"start": 824, "end": 150824}
            and segments[0]["start_us"] == 0
            and segments[0]["end_us"] == 2_000_000
            and segments[1]["media_sequence"] == 1
            and segments[1]["media"] == {"start": 150824, "end": 292824}
            and segments[1]["start_us"] == 2_000_000
            and segments[1]["end_us"] == 4_000_000
        )
    checks.append(("smoke: valid playlist -> 200 with exact timeline", ok, f"status={status} {detail}"))

    status, body = _post_audit(OUT_OF_BOUNDS_PLAYLIST, RESOURCES)
    error = body.get("error", {})
    ok = status == 422 and error.get("code") == "out_of_bounds" and error.get("line") == 6
    checks.append(
        ("smoke: out-of-bounds playlist -> 422 out_of_bounds", ok, f"status={status} {json.dumps(body)}")
    )

    status, body = _post_audit(UNKNOWN_RESOURCE_PLAYLIST, RESOURCES)
    error = body.get("error", {})
    ok = status == 422 and error.get("code") == "unknown_resource"
    checks.append(
        ("smoke: unknown resource -> 422 unknown_resource", ok, f"status={status} {json.dumps(body)}")
    )

    status, body = _post_audit(MISSING_INIT_PLAYLIST, RESOURCES)
    error = body.get("error", {})
    ok = status == 422 and error.get("code") == "missing_init"
    checks.append(
        (
            "smoke: discontinuity without new EXT-X-MAP -> 422 missing_init",
            ok,
            f"status={status} {json.dumps(body)}",
        )
    )

    return checks


def main() -> int:
    results: list[tuple[str, bool, str]] = []

    if wait_ready():
        results.append(("api readiness (/healthz)", True, API_BASE_URL))
    else:
        results.append(("api readiness (/healthz)", False, f"no 200 from {API_BASE_URL}"))

    ok, summary, detail = run_unit_tests()
    results.append(("code tests (unittest)", ok, summary))
    if not ok:
        print(detail, file=sys.stderr)

    ok, summary = run_build_check()
    results.append(("build check (byte-compile)", ok, summary))

    if results[0][1]:  # only smoke against a ready API
        results.extend(run_smoke())
    else:
        results.append(("smoke tests", False, "skipped: API not ready"))

    failed = 0
    print("\n=== verify summary ===")
    for name, ok, detail in results:
        mark = "PASS" if ok else "FAIL"
        print(f"{mark}  {name}  --  {detail}")
        if not ok:
            failed += 1
    print(f"{len(results) - failed}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
