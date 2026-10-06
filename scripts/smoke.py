"""接口冒烟：兼容回归 + 运动守卫正常运动 + 运动守卫超限（含坏组隔离）。

成功退出码 0，失败退出码 1。仅依赖标准库，报文由 tests.encoding 构造。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.encoding import make_moving_pair, make_pair  # noqa: E402

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
    # 1) 兼容回归：不带 motion_guard，有效报文对 + 坏 CRC 报文对
    (t1, h1), (t2, h2) = make_pair(39.9042, 116.4074, 0x780A1B,
                                   t_even_ms=T0, t_odd_ms=T0 + 500)
    good = {
        "id": "smoke-valid",
        "frame1": {"received_at_ms": t1, "raw_hex": h1},
        "frame2": {"received_at_ms": t2, "raw_hex": h2},
    }
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
    # 兼容回归：省略 motion_guard 时响应不得出现运动字段
    if "distance_nm" in res or "ground_speed_kt" in res:
        fail(f"省略 motion_guard 却返回运动字段：{res}")

    # 2) 运动守卫-正常运动：10 秒内向北约 1.2 海里 ≈ 432 节
    #    选在 NL 纬度分界（39.9226°）南侧，避免两帧落入不同纬度带
    lat1 = 39.8
    lat2 = lat1 + 1.2 / 60.0
    (tm1, hm1), (tm2, hm2) = make_moving_pair(
        lat1, 116.4074, lat2, 116.4074, 0x780A1B,
        t_even_ms=T0, t_odd_ms=T0 + 10_000,
    )
    moving = {
        "id": "smoke-motion-ok",
        "frame1": {"received_at_ms": tm1, "raw_hex": hm1},
        "frame2": {"received_at_ms": tm2, "raw_hex": hm2},
    }
    # 3) 运动守卫-超限：1 秒内向北 1 度（约 60 海里 ≈ 216000 节）
    (tf1, hf1), (tf2, hf2) = make_moving_pair(
        20.0, 100.0, 21.0, 100.0, 0x780A1B,
        t_even_ms=T0, t_odd_ms=T0 + 1000,
    )
    too_fast = {
        "id": "smoke-motion-limit",
        "frame1": {"received_at_ms": tf1, "raw_hex": hf1},
        "frame2": {"received_at_ms": tf2, "raw_hex": hf2},
    }
    # 4) 跨日期变更线相邻位置：短弧不足 1 海里，2000 节下必须放行
    (tc1, hc1), (tc2, hc2) = make_moving_pair(
        86.9, 179.9, 86.9, -179.95, 0x780A1B,
        t_even_ms=T0, t_odd_ms=T0 + 1000, nl=2,
    )
    cross_line = {
        "id": "smoke-antimeridian",
        "frame1": {"received_at_ms": tc1, "raw_hex": hc1},
        "frame2": {"received_at_ms": tc2, "raw_hex": hc2},
    }

    body = post(
        base_url,
        {
            "pairs": [too_fast, moving, cross_line],
            "motion_guard": {"max_ground_speed_kt": 2000},
        },
    )
    if body.get("ok_count") != 2 or body.get("error_count") != 1:
        fail(f"运动守卫计数不符：{body.get('ok_count')=} {body.get('error_count')=}")
    by_id = {r["id"]: r for r in body["results"]}
    if list(by_id) != ["smoke-motion-limit", "smoke-motion-ok", "smoke-antimeridian"]:
        fail("运动守卫结果顺序异常")

    fast_item = by_id["smoke-motion-limit"]
    if fast_item["status"] != "error":
        fail(f"超限组未判错：{fast_item}")
    if fast_item["error"]["code"] != "MOTION_LIMIT_EXCEEDED":
        fail(f"超限组错误码异常：{fast_item['error']}")
    if fast_item["error"]["frame"] is not None or "result" in fast_item:
        fail("超限组帧编号应为 null 且不得携带 result")

    mov = by_id["smoke-motion-ok"]["result"]
    if not (1.19 < mov["distance_nm"] < 1.21):
        fail(f"正常运动位移异常：{mov['distance_nm']}")
    if not (400.0 < mov["ground_speed_kt"] < 470.0):
        fail(f"正常运动地速异常：{mov['ground_speed_kt']}")
    if round(mov["distance_nm"], 3) != mov["distance_nm"]:
        fail("distance_nm 未保留三位小数")
    if round(mov["ground_speed_kt"], 3) != mov["ground_speed_kt"]:
        fail("ground_speed_kt 未保留三位小数")

    cross = by_id["smoke-antimeridian"]["result"]
    if cross["distance_nm"] >= 1.0 or cross["ground_speed_kt"] >= 2000.0:
        fail(
            "跨日期变更线被误判为大跃迁："
            f"{cross['distance_nm']} nm / {cross['ground_speed_kt']} kt"
        )

    print(
        "SMOKE_OK: 兼容回归无运动字段；正常运动返回三位小数位移/地速；"
        "超限返回 MOTION_LIMIT_EXCEEDED 且坏组不遮蔽；跨日期变更线按短弧放行"
    )
    return 0


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else "http://api:8000"
    try:
        raise SystemExit(main(base.rstrip("/")))
    except (urllib.error.URLError, OSError, AssertionError) as exc:
        fail(f"请求失败：{exc}")
