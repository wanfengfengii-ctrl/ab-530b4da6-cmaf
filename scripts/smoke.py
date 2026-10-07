#!/usr/bin/env python3
"""API 冒烟测试：针对运行中的服务验证合法 / 非法清单的响应。

由 verify 一次性服务调用。环境变量：
- API_BASE：服务根地址（默认 http://api:8080）
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

API_BASE = os.environ.get("API_BASE", "http://api:8080").rstrip("/")

VALID_PLAYLIST = (
    "#EXTM3U\r\n"
    "#EXT-X-VERSION:7\r\n"
    "#EXT-X-TARGETDURATION:6\r\n"
    '#EXT-X-MAP:URI="v.mp4",BYTERANGE="90@0"\r\n'
    "#EXTINF:2.5,\r\n"
    "#EXT-X-BYTERANGE:10@90\r\n"
    "v.mp4\r\n"
    "#EXTINF:3.000001,\r\n"
    "#EXT-X-BYTERANGE:20\r\n"
    "v.mp4\r\n"
    "#EXT-X-DISCONTINUITY\r\n"
    '#EXT-X-MAP:URI="a.mp4",BYTERANGE="40@0"\r\n'
    "#EXTINF:1.0,\r\n"
    "#EXT-X-BYTERANGE:100@40\r\n"
    "a.mp4\r\n"
    "#EXT-X-ENDLIST\r\n"
)
LENGTHS = {"v.mp4": 200, "a.mp4": 300}

failures: list[str] = []


def post_audit(manifest: str, lengths: dict) -> tuple[int, dict]:
    body = json.dumps(
        {"manifest": manifest, "resource_lengths": lengths}
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{API_BASE}/api/playlists/audit",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def check(name: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"[smoke:{mark}] {name}{(' -- ' + detail) if detail else ''}", flush=True)
    if not ok:
        failures.append(name)


def smoke_valid() -> None:
    status, data = post_audit(VALID_PLAYLIST, LENGTHS)
    if status != 200:
        check("合法清单返回 200", False, f"got {status}: {data}")
        return
    segs = data.get("segments", [])
    check("合法清单返回 200", True)
    check("片段数量为 3", len(segs) == 3, f"got {len(segs)}")

    s0, s1, s2 = segs
    check("默认序号从 0 递增",
          [s["sequence"] for s in segs] == [0, 1, 2],
          str([s["sequence"] for s in segs]))
    check("epoch 在 discontinuity 后递增",
          [s["epoch"] for s in segs] == [0, 0, 1],
          str([s["epoch"] for s in segs]))
    check("初始化段半开区间",
          s0["init_range"] == {"start": 0, "end": 90}
          and s2["init_range"] == {"start": 0, "end": 40})
    check("隐式偏移承接同 URI 前一范围",
          s1["media_range"] == {"start": 100, "end": 120},
          str(s1["media_range"]))
    check("累计微秒时间线",
          [(s["start_us"], s["end_us"]) for s in segs]
          == [(0, 2_500_000), (2_500_000, 5_500_001), (5_500_001, 6_500_001)],
          str([(s["start_us"], s["end_us"]) for s in segs]))


def smoke_out_of_bounds() -> None:
    # v.mp4 登记长度 119，而第二段隐式区间 [100,120) 越界。
    status, data = post_audit(VALID_PLAYLIST, {"v.mp4": 119, "a.mp4": 300})
    check("越界清单返回 422", status == 422, f"got {status}")
    err = data.get("error", {})
    check("越界错误代码", err.get("code") == "out_of_bounds", str(err))
    # 第二段 BYTERANGE（隐式偏移 [100,120)）位于清单第 9 行。
    check("越界报告首个问题行", err.get("line") == 9, str(err))


def smoke_unknown_resource() -> None:
    status, data = post_audit(VALID_PLAYLIST, {"v.mp4": 200})  # 缺少 a.mp4
    check("未知资源返回 422", status == 422, f"got {status}")
    err = data.get("error", {})
    check("未知资源错误代码", err.get("code") == "unknown_resource", str(err))
    # a.mp4 的 MAP 声明行（第 12 行）先于其媒体片段出现。
    check("未知资源报告 MAP 行", err.get("line") == 12, str(err))


def smoke_overlap() -> None:
    playlist = (
        "#EXTM3U\n#EXT-X-VERSION:7\n"
        '#EXT-X-MAP:URI="v.mp4",BYTERANGE="10@0"\n'
        "#EXTINF:1.0,\n#EXT-X-BYTERANGE:100@10\nv.mp4\n"
        "#EXTINF:1.0,\n#EXT-X-BYTERANGE:50@100\nv.mp4\n"
        "#EXT-X-ENDLIST\n"
    )
    status, data = post_audit(playlist, {"v.mp4": 5000})
    check("重叠范围返回 422", status == 422, f"got {status}")
    check("重叠错误代码",
          data.get("error", {}).get("code") == "overlapping_range", str(data))


def smoke_live_event_rejected() -> None:
    playlist = VALID_PLAYLIST.replace("#EXT-X-ENDLIST\r\n", "")
    status, data = post_audit(playlist, LENGTHS)
    check("无 ENDLIST 的清单返回 422", status == 422, f"got {status}")


def main() -> int:
    print(f"== API smoke against {API_BASE} ==", flush=True)
    smoke_valid()
    smoke_out_of_bounds()
    smoke_unknown_resource()
    smoke_overlap()
    smoke_live_event_rejected()
    if failures:
        print(f"\nSMOKE FAILED: {len(failures)} case(s): {failures}", flush=True)
        return 1
    print("\nSMOKE OK", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
