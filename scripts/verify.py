#!/usr/bin/env python3
"""verify 一次性服务入口。

流程（任一步失败都继续执行后续步骤，最后以退出码统一汇总）：
1. 轮询 API 的 /healthz，等待服务就绪；
2. 代码测试：python -m unittest discover；
3. 构建检查：python -m compileall（全部源码/脚本可编译）；
4. API 冒烟：合法清单 + 越界 / 未知资源 / 重叠 / 无 ENDLIST 清单。

环境变量：
- API_BASE：服务根地址（默认 http://api:8080）
- APP_ROOT：仓库根目录（容器内默认 /app）
- READY_TIMEOUT：就绪等待秒数（默认 60）
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request

API_BASE = os.environ.get("API_BASE", "http://api:8080").rstrip("/")
APP_ROOT = os.environ.get("APP_ROOT", "/app")
READY_TIMEOUT = int(os.environ.get("READY_TIMEOUT", "60"))


def wait_ready() -> bool:
    deadline = time.time() + READY_TIMEOUT
    last_err: Exception | None = None
    print(f"[verify] waiting for API at {API_BASE}/healthz ...", flush=True)
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{API_BASE}/healthz", timeout=3) as resp:
                if resp.status == 200:
                    print("[verify] API is ready", flush=True)
                    return True
        except Exception as e:  # noqa: BLE001 - 就绪探测需要兜底所有网络异常
            last_err = e
            time.sleep(1)
    print(f"[verify] API not ready within {READY_TIMEOUT}s: {last_err}", flush=True)
    return False


def run_step(name: str, cmd: list[str]) -> bool:
    print(f"\n[verify] === {name}: {' '.join(cmd)} ===", flush=True)
    proc = subprocess.run(cmd, cwd=APP_ROOT)
    ok = proc.returncode == 0
    print(f"[verify] {name}: {'PASS' if ok else 'FAIL'} (exit {proc.returncode})",
          flush=True)
    return ok


def main() -> int:
    results: list[tuple[str, bool]] = []

    results.append(("等待服务就绪", wait_ready()))
    results.append((
        "代码测试 (unittest)",
        run_step("代码测试",
                 [sys.executable, "-m", "unittest", "discover", "-s", "tests"]),
    ))
    results.append((
        "构建检查 (compileall)",
        run_step("构建检查",
                 [sys.executable, "-m", "compileall", "-q", "app", "scripts", "tests"]),
    ))
    results.append((
        "API 冒烟（合法/越界清单等）",
        run_step("API 冒烟",
                 [sys.executable, os.path.join("scripts", "smoke.py")]),
    ))

    print("\n[verify] ============ 汇总 ============", flush=True)
    for name, ok in results:
        print(f"[verify] {'PASS' if ok else 'FAIL'}  {name}", flush=True)
    failed = [name for name, ok in results if not ok]
    if failed:
        print(f"\n[verify] VERIFY FAILED: {len(failed)} step(s): {failed}", flush=True)
        return 1
    print("\n[verify] VERIFY OK", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
