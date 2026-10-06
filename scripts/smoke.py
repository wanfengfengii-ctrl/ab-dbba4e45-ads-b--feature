"""接口冒烟：一对有效 DF17 报文 + 一对坏 CRC 报文，批量提交验证。

成功退出码 0，失败退出码 1。仅依赖标准库，报文由 tests.encoding 构造。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.encoding import make_pair  # noqa: E402

T0 = 1_700_000_000_000


def post(base_url: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{base_url}/api/adsb/positions/decode",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.status == 200, f"HTTP {resp.status}"
        return json.loads(resp.read())


def fail(msg: str) -> None:
    print(f"SMOKE_FAIL: {msg}", file=sys.stderr)
    raise SystemExit(1)


def main(base_url: str) -> int:
    # 1) 有效报文对（北京上空）
    (t1, h1), (t2, h2) = make_pair(39.9042, 116.4074, 0x780A1B,
                                   t_even_ms=T0, t_odd_ms=T0 + 500)
    good = {
        "id": "smoke-valid",
        "frame1": {"received_at_ms": t1, "raw_hex": h1},
        "frame2": {"received_at_ms": t2, "raw_hex": h2},
    }

    # 2) 坏 CRC 报文对：翻转数据位且不重算校验
    corrupt = bytearray.fromhex(h2)
    corrupt[7] ^= 0x01
    bad = {
        "id": "smoke-bad-crc",
        "frame1": {"received_at_ms": t1, "raw_hex": h1},
        "frame2": {"received_at_ms": t2, "raw_hex": corrupt.hex().upper()},
    }

    body = post(base_url, {"pairs": [bad, good]})  # 坏组在前，验证不遮蔽

    if body.get("ok_count") != 1 or body.get("error_count") != 1:
        fail(f"计数不符：{body.get('ok_count')=} {body.get('error_count')=}")

    by_id = {r["id"]: r for r in body["results"]}
    if list(by_id) != ["smoke-bad-crc", "smoke-valid"]:
        fail("结果顺序或编号异常")

    bad_item = by_id["smoke-bad-crc"]
    if bad_item["status"] != "error" or bad_item["error"]["code"] != "BAD_CRC":
        fail(f"坏 CRC 组裁决异常：{bad_item}")
    if bad_item["error"]["frame"] != 2:
        fail("坏 CRC 组未标明帧编号 frame=2")

    good_item = by_id["smoke-valid"]
    if good_item["status"] != "ok":
        fail(f"有效报文组未通过：{good_item}")
    res = good_item["result"]
    if res["icao"] != "780A1B" or res["newer_frame"] != 2:
        fail(f"结果字段异常：{res}")
    if abs(res["latitude"] - 39.9042) > 1e-3 or abs(res["longitude"] - 116.4074) > 1e-3:
        fail(f"解算位置偏离：{res['latitude']},{res['longitude']}")
    if not (-180.0 <= res["longitude"] < 180.0):
        fail("经度未归一化到 [-180,180)")

    print("SMOKE_OK: 有效报文解算正确，坏 CRC 返回 BAD_CRC(frame=2)，坏组未遮蔽好组")
    return 0


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else "http://api:8000"
    try:
        raise SystemExit(main(base.rstrip("/")))
    except (urllib.error.URLError, OSError, AssertionError) as exc:
        fail(f"请求失败：{exc}")
