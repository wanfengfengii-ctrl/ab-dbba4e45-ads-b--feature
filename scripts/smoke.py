"""接口冒烟：报文对批量裁决与运动合理性守卫（motion_guard）。

两类请求：
1. 不带 motion_guard：兼容回归——一对有效报文 + 一对坏 CRC 报文，
   且成功响应不得出现 distance_nm / ground_speed_kt；
2. 带 motion_guard：正常运动（含跨日期变更线短弧）放行并返回三位小数
   运动量；错配大跃迁返回 MOTION_LIMIT_EXCEEDED；同刻报文返回
   MOTION_TIME_UNRESOLVED；坏组不遮蔽、顺序与计数不变。

成功退出码 0，失败退出码 1。仅依赖标准库，报文由 tests.encoding 构造。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.encoding import make_motion_pair, make_pair  # noqa: E402

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


def group(pid: str, pair: tuple) -> dict:
    (t1, h1), (t2, h2) = pair
    return {
        "id": pid,
        "frame1": {"received_at_ms": t1, "raw_hex": h1},
        "frame2": {"received_at_ms": t2, "raw_hex": h2},
    }


def fail(msg: str) -> None:
    print(f"SMOKE_FAIL: {msg}", file=sys.stderr)
    raise SystemExit(1)


def main(base_url: str) -> int:
    # ===== 请求 1（兼容回归）：不带 motion_guard =====
    # 有效报文对（北京上空）与坏 CRC 报文对，坏组在前验证不遮蔽
    (t1, h1), (t2, h2) = make_pair(39.9042, 116.4074, 0x780A1B,
                                   t_even_ms=T0, t_odd_ms=T0 + 500)
    corrupt = bytearray.fromhex(h2)
    corrupt[7] ^= 0x01
    compat_pairs = [
        group("smoke-bad-crc",
              ((t1, h1), (t2, corrupt.hex().upper()))),
        group("smoke-valid", ((t1, h1), (t2, h2))),
    ]
    body = post(base_url, {"pairs": compat_pairs})

    if body.get("ok_count") != 1 or body.get("error_count") != 1:
        fail(f"[兼容] 计数不符：{body.get('ok_count')=} {body.get('error_count')=}")
    if [r["id"] for r in body["results"]] != ["smoke-bad-crc", "smoke-valid"]:
        fail("[兼容] 结果顺序或编号异常")

    bad_item = body["results"][0]
    if bad_item["status"] != "error" or bad_item["error"]["code"] != "BAD_CRC":
        fail(f"[兼容] 坏 CRC 组裁决异常：{bad_item}")
    if bad_item["error"]["frame"] != 2:
        fail("[兼容] 坏 CRC 组未标明帧编号 frame=2")

    good_item = body["results"][1]
    if good_item["status"] != "ok":
        fail(f"[兼容] 有效报文组未通过：{good_item}")
    res = good_item["result"]
    if res["icao"] != "780A1B" or res["newer_frame"] != 2:
        fail(f"[兼容] 结果字段异常：{res}")
    if abs(res["latitude"] - 39.9042) > 1e-3 or abs(res["longitude"] - 116.4074) > 1e-3:
        fail(f"[兼容] 解算位置偏离：{res['latitude']},{res['longitude']}")
    if not (-180.0 <= res["longitude"] < 180.0):
        fail("[兼容] 经度未归一化到 [-180,180)")
    if "distance_nm" in res or "ground_speed_kt" in res:
        fail("[兼容] 省略 motion_guard 时响应不得包含运动量字段")

    # ===== 请求 2（运动守卫）：max_ground_speed_kt=2000 =====
    jump = make_motion_pair(40.0, 116.40, 40.0, 116.50, 0x780A1B,
                            t_even_ms=T0, t_odd_ms=T0 + 500)
    motion = make_motion_pair(40.0, 116.30, 40.0, 116.31, 0x780A1B,
                              t_even_ms=T0, t_odd_ms=T0 + 5000)
    antimeridian = make_motion_pair(49.0, 179.99, 49.0, -179.99, 0x780A1B,
                                    t_even_ms=T0, t_odd_ms=T0 + 5000)
    same_ts = make_pair(40.0, 116.4, 0x780A1B, t_even_ms=T0, t_odd_ms=T0)
    guard_req = {
        "pairs": [
            group("smoke-jump", jump),          # 错配大跃迁 -> 超限
            group("smoke-motion", motion),      # 正常运动 -> 放行
            group("smoke-antimeridian", antimeridian),  # 跨日期变更线短弧
            group("smoke-same-ts", same_ts),    # 同刻 -> 不可计算
        ],
        "motion_guard": {"max_ground_speed_kt": 2000},
    }
    body = post(base_url, guard_req)

    if body.get("ok_count") != 2 or body.get("error_count") != 2:
        fail(f"[守卫] 计数不符：{body.get('ok_count')=} {body.get('error_count')=}")
    by_id = {r["id"]: r for r in body["results"]}
    if list(by_id) != ["smoke-jump", "smoke-motion",
                       "smoke-antimeridian", "smoke-same-ts"]:
        fail("[守卫] 结果顺序或编号异常")

    jump_item = by_id["smoke-jump"]
    if jump_item["status"] != "error" or "result" in jump_item:
        fail(f"[守卫] 错配跃迁组应失败且无 result：{jump_item}")
    if jump_item["error"]["code"] != "MOTION_LIMIT_EXCEEDED":
        fail(f"[守卫] 错配跃迁应返回 MOTION_LIMIT_EXCEEDED：{jump_item['error']}")
    if jump_item["error"]["frame"] is not None:
        fail("[守卫] MOTION_LIMIT_EXCEEDED 的 frame 应为 null")

    same_item = by_id["smoke-same-ts"]
    if same_item["status"] != "error":
        fail(f"[守卫] 同刻报文组应失败：{same_item}")
    if same_item["error"]["code"] != "MOTION_TIME_UNRESOLVED":
        fail("[守卫] 同刻报文应返回 MOTION_TIME_UNRESOLVED")
    if same_item["error"]["frame"] is not None:
        fail("[守卫] MOTION_TIME_UNRESOLVED 的 frame 应为 null")

    motion_item = by_id["smoke-motion"]
    if motion_item["status"] != "ok":
        fail(f"[守卫] 正常运动组应放行：{motion_item}")
    mres = motion_item["result"]
    if mres["distance_nm"] != 0.461 or mres["ground_speed_kt"] != 331.623:
        fail(f"[守卫] 正常运动量三位小数不符：{mres['distance_nm']},"
             f"{mres['ground_speed_kt']}")

    anti_item = by_id["smoke-antimeridian"]
    if anti_item["status"] != "ok":
        fail(f"[守卫] 跨日期变更线相邻位置应放行：{anti_item}")
    ares = anti_item["result"]
    if not (ares["distance_nm"] < 2.0 and ares["ground_speed_kt"] < 1000.0):
        fail(f"[守卫] 跨日期变更线被误判为绕行地球的大跃迁：{ares}")
    if not (-180.0 <= ares["longitude"] < 180.0):
        fail("[守卫] 跨日期变更线经度未归一化")

    # 坏组（超限/不可计算）不得遮蔽合格组：两组 ok 已在上文计数中固定
    print(
        "SMOKE_OK: 兼容回归通过（省略守卫无运动量字段）；正常运动 0.461nm/"
        "331.623kt 放行；跨日期变更线按短弧放行；错配跃迁 MOTION_LIMIT_EXCEEDED、"
        "同刻 MOTION_TIME_UNRESOLVED，坏组未遮蔽好组，顺序与计数正确"
    )
    return 0


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else "http://api:8000"
    try:
        raise SystemExit(main(base.rstrip("/")))
    except (urllib.error.URLError, OSError, AssertionError) as exc:
        fail(f"请求失败：{exc}")
