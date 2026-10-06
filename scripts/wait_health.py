"""轮询 API /health，直到健康或超时（默认 30 秒）。"""
from __future__ import annotations

import sys
import time
import urllib.error
import urllib.request


def main(base_url: str, timeout_s: float = 30.0) -> int:
    deadline = time.monotonic() + timeout_s
    last_err = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=2) as resp:
                if resp.status == 200:
                    print(f"API 健康检查通过：{base_url}/health")
                    return 0
                last_err = f"HTTP {resp.status}"
        except (urllib.error.URLError, OSError) as exc:
            last_err = str(exc)
        time.sleep(1.0)
    print(f"等待 API 健康超时：{last_err}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else "http://api:8000"
    raise SystemExit(main(base.rstrip("/")))
