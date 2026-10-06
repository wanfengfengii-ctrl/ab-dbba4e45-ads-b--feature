"""运动合理性守卫（motion_guard）测试。

覆盖：
- 最短地表距离（haversine）与跨日期变更线短弧；
- 正常运动的位移/地速计算与阈值边界（相等通过、不用舍入值裁决）；
- MOTION_TIME_UNRESOLVED / MOTION_LIMIT_EXCEEDED 的单元与接口语义；
- 坏组隔离、批次顺序与计数、入参校验（1..2000）。
"""
from __future__ import annotations

import math
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app  # noqa: E402
from app.modes import (  # noqa: E402
    DecodedPosition,
    FramePosition,
    ModesError,
    adjudicate,
    cpr_nl,
    guard_motion,
    haversine_nm,
    motion_estimate,
)
from tests.encoding import (  # noqa: E402
    encode_cpr,
    make_airborne_msg,
    make_motion_pair,
    make_pair,
)

client = TestClient(app)
ICAO = 0x780A1B
T0 = 1_700_000_000_000


# --- 最短地表距离 ---------------------------------------------------------

def test_haversine_one_arcmin_is_one_nm():
    # 1 海里定义为球面上 1 角分大圆弧长
    assert haversine_nm(0.0, 0.0, 0.0, 1.0 / 60.0) == pytest.approx(1.0, abs=1e-9)
    assert haversine_nm(0.0, 0.0, 0.0, 1.0) == pytest.approx(60.0, abs=1e-9)
    # 同一地点距离为零，且结果对称
    assert haversine_nm(39.9, 116.4, 39.9, 116.4) == 0.0


def test_haversine_antimeridian_takes_short_arc():
    # 179.99°E 与 179.99°W 地表相距仅 0.02°（赤道约 1.2 nm），
    # 绝不能按 359.98° 的长弧（约 21599 nm）计算
    short = haversine_nm(0.0, 179.99, 0.0, -179.99)
    assert short == pytest.approx(1.2, abs=0.01)
    long_way = 180.0 * 60.0 / math.pi * math.radians(359.98)
    assert short < 2.0 < long_way
    # 与跨本初子午线的等距点一致
    assert short == pytest.approx(haversine_nm(0.0, -0.01, 0.0, 0.01), abs=1e-9)


def test_haversine_poles_and_meridian():
    # 经线方向 30° 恰为 1800 nm
    assert haversine_nm(0.0, 10.0, 30.0, 10.0) == pytest.approx(1800.0, abs=1e-9)
    assert haversine_nm(-45.0, 10.0, -44.0, 10.0) == pytest.approx(60.0, abs=1e-6)


# --- 正常运动与跨日期变更线（单元层） --------------------------------------

def test_normal_motion_distance_and_speed():
    # 5 秒内沿北纬 40° 东移约 0.01°：约 0.461 nm / 331.6 kt
    (t1, h1), (t2, h2) = make_motion_pair(
        40.0, 116.30, 40.0, 116.31, ICAO, t_even_ms=T0, t_odd_ms=T0 + 5000
    )
    pos = adjudicate(h1, t1, h2, t2)
    distance_nm, ground_speed_kt = motion_estimate(pos)
    assert distance_nm == pytest.approx(0.460588, abs=1e-5)
    assert ground_speed_kt == pytest.approx(331.623475, abs=1e-3)


def test_normal_motion_odd_frame_newer_uses_same_motion():
    (t1, h1), (t2, h2) = make_motion_pair(
        40.0, 116.30, 40.0, 116.31,
        ICAO, t_even_ms=T0 + 5000, t_odd_ms=T0,
    )
    pos = adjudicate(h1, t1, h2, t2)
    assert pos.newer_frame == 1
    distance_nm, ground_speed_kt = motion_estimate(pos)
    assert distance_nm == pytest.approx(0.460588, abs=1e-5)
    assert ground_speed_kt == pytest.approx(331.623475, abs=1e-3)


def test_per_frame_positions_follow_frame_order_when_frame1_is_odd():
    # frame1 为奇帧、frame2 为偶帧时，pos1/pos2 仍须按帧序绑定，
    # 距离与时间间隔与帧序无关
    lat1, lon1, lat2, lon2 = 40.0, 116.30, 40.0, 116.31
    nl = cpr_nl(lat1)
    yz_odd, xz_odd = encode_cpr(lat1, lon1, 1, nl)
    yz_even, xz_even = encode_cpr(lat2, lon2, 0, nl)
    h1 = bytes(make_airborne_msg(ICAO, yz_odd, xz_odd, 1)).hex().upper()
    h2 = bytes(make_airborne_msg(ICAO, yz_even, xz_even, 0)).hex().upper()

    pos = adjudicate(h1, T0, h2, T0 + 5000)
    assert pos.newer_frame == 2
    assert pos.pos1.frame == 1 and pos.pos2.frame == 2
    assert pos.pos1.latitude == pytest.approx(lat1, abs=1e-4)
    assert pos.pos2.latitude == pytest.approx(lat2, abs=1e-4)
    # 偶/奇角色互换后 CPR 子网格残差略不同，但量级一致且距离对称
    distance_nm, ground_speed_kt = motion_estimate(pos)
    assert distance_nm == pytest.approx(0.458548, abs=1e-5)
    assert ground_speed_kt == pytest.approx(330.154506, abs=1e-3)


def test_stationary_pair_zero_speed_passes():
    # (0,0) 在偶/奇 CPR 网格上恰好同格，残差严格为零
    (t1, h1), (t2, h2) = make_pair(0.0, 0.0, ICAO, t_even_ms=T0, t_odd_ms=T0 + 500)
    pos = adjudicate(h1, t1, h2, t2, max_ground_speed_kt=1)
    distance_nm, ground_speed_kt = motion_estimate(pos)
    assert distance_nm == 0.0
    assert ground_speed_kt == 0.0


def test_stationary_pair_subgrid_residual_small():
    # 其他静止点偶/奇网格量化残差不为零，但 10 秒间隔下远低于 1 kt
    (t1, h1), (t2, h2) = make_pair(40.0, 116.4, ICAO,
                                   t_even_ms=T0, t_odd_ms=T0 + 10_000)
    pos = adjudicate(h1, t1, h2, t2, max_ground_speed_kt=1)
    distance_nm, ground_speed_kt = motion_estimate(pos)
    assert distance_nm < 0.01
    assert ground_speed_kt < 1.0


def test_antimeridian_motion_is_short_arc_not_giant_jump():
    # 两帧分别位于 179.99°E 与 179.99°W（北纬 49°），相隔 5 秒。
    # 全球解算后按短弧只有约 0.79 nm / 566 kt，而不是绕地球的巨量跃迁。
    (t1, h1), (t2, h2) = make_motion_pair(
        49.0, 179.99, 49.0, -179.99, ICAO, t_even_ms=T0, t_odd_ms=T0 + 5000
    )
    pos = adjudicate(h1, t1, h2, t2)
    assert -180.0 <= pos.pos1.longitude < 180.0
    assert -180.0 <= pos.pos2.longitude < 180.0
    distance_nm, ground_speed_kt = motion_estimate(pos)
    assert distance_nm < 2.0
    assert ground_speed_kt < 1000.0
    # 与直接按解算位置计算的短弧距离一致
    assert distance_nm == pytest.approx(
        haversine_nm(
            pos.pos1.latitude, pos.pos1.longitude,
            pos.pos2.latitude, pos.pos2.longitude,
        ),
        abs=1e-12,
    )
    # 1000 kt 阈值应放行；该运动完全合理
    adjudicate(h1, t1, h2, t2, max_ground_speed_kt=1000)


# --- 阈值裁决：相等通过、不用舍入值 ---------------------------------------

def _synthetic_position(speed_kt: float, dt_ms: int = 3_600_000) -> DecodedPosition:
    """沿经线构造精确指定平均地速的合成解算结果。"""
    distance_nm = speed_kt * dt_ms / 3_600_000
    deg = distance_nm / 60.0
    p1 = FramePosition(1, 0.0, 0.0, 0)
    p2 = FramePosition(2, deg, 0.0, dt_ms)
    return DecodedPosition(1, deg, 0.0, 2, dt_ms, p1, p2)


def test_threshold_equality_passes():
    pos = _synthetic_position(1800.0)  # 30° / 1h 恰为 1800 kt
    guard_motion(pos, 1800)  # 相等不抛异常
    with pytest.raises(ModesError) as ei:
        guard_motion(pos, 1799)
    assert ei.value.code == "MOTION_LIMIT_EXCEEDED"


def test_adjudication_uses_unrounded_speed():
    # 原始地速 239.000144… kt，三位小数舍入后恰为 239.000。
    # 阈值 239 必须判定超限，证明裁决没有使用舍入值。
    (t1, h1), (t2, h2) = make_motion_pair(
        40.0, 116.30, 40.0, 116.304, ICAO, t_even_ms=T0, t_odd_ms=T0 + 2789
    )
    pos = adjudicate(h1, t1, h2, t2)
    _, raw_speed = motion_estimate(pos)
    assert raw_speed > 239.0
    assert round(raw_speed, 3) == 239.0
    with pytest.raises(ModesError) as ei:
        adjudicate(h1, t1, h2, t2, max_ground_speed_kt=239)
    assert ei.value.code == "MOTION_LIMIT_EXCEEDED"
    # 阈值 240 放行
    adjudicate(h1, t1, h2, t2, max_ground_speed_kt=240)


def test_mismatched_pair_detected_as_limit_exceeded():
    # 两帧各自合法，但强行错配：偶帧编码 116.40°E、奇帧编码 116.50°E，
    # 全球解算把二者投到约 108.4°E 的另一经度带，0.5 秒内约 27000 kt。
    (t1, h1), (t2, h2) = make_motion_pair(
        40.0, 116.40, 40.0, 116.50, ICAO, t_even_ms=T0, t_odd_ms=T0 + 500
    )
    # 不启用守卫时：两帧各自合法，得到一条“可用但失真”的航迹
    pos = adjudicate(h1, t1, h2, t2)
    assert pos.time_delta_ms == 500
    with pytest.raises(ModesError) as ei:
        adjudicate(h1, t1, h2, t2, max_ground_speed_kt=2000)
    assert ei.value.code == "MOTION_LIMIT_EXCEEDED"
    assert ei.value.frame is None


def test_same_timestamp_motion_time_unresolved():
    (t1, h1), (t2, h2) = make_pair(40.0, 116.4, ICAO, t_even_ms=T0, t_odd_ms=T0)
    # 不启用守卫时同刻报文照常解算（frame1 为较新帧）
    assert adjudicate(h1, t1, h2, t2).newer_frame == 1
    with pytest.raises(ModesError) as ei:
        adjudicate(h1, t1, h2, t2, max_ground_speed_kt=2000)
    assert ei.value.code == "MOTION_TIME_UNRESOLVED"
    assert ei.value.frame is None


def test_omitting_guard_changes_nothing():
    (t1, h1), (t2, h2) = make_motion_pair(
        40.0, 116.30, 40.0, 116.31, ICAO, t_even_ms=T0, t_odd_ms=T0 + 5000
    )
    pos = adjudicate(h1, t1, h2, t2)  # 无 max_ground_speed_kt
    # 旧有裁决结果不含运动字段；运动量须显式调用 motion_estimate
    assert pos.latitude != 0.0
    with pytest.raises(AttributeError):
        pos.distance_nm


# --- 接口层 ---------------------------------------------------------------

def _payload(pid, pair, guard=None):
    body = {
        "id": pid,
        "frame1": {"received_at_ms": pair[0][0], "raw_hex": pair[0][1]},
        "frame2": {"received_at_ms": pair[1][0], "raw_hex": pair[1][1]},
    }
    req = {"pairs": [body]}
    if guard is not None:
        req["motion_guard"] = {"max_ground_speed_kt": guard}
    return req


def test_api_omitted_guard_response_unchanged():
    pair = make_motion_pair(
        40.0, 116.30, 40.0, 116.31, ICAO, t_even_ms=T0, t_odd_ms=T0 + 5000
    )
    r = client.post("/api/adsb/positions/decode", json=_payload("g", pair))
    assert r.status_code == 200
    res = r.json()["results"][0]["result"]
    assert "distance_nm" not in res
    assert "ground_speed_kt" not in res


def test_api_normal_motion_fields_three_decimals():
    pair = make_motion_pair(
        40.0, 116.30, 40.0, 116.31, ICAO, t_even_ms=T0, t_odd_ms=T0 + 5000
    )
    r = client.post(
        "/api/adsb/positions/decode", json=_payload("g", pair, guard=1000)
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok_count"] == 1 and body["error_count"] == 0
    res = body["results"][0]["result"]
    assert res["distance_nm"] == 0.461
    assert res["ground_speed_kt"] == 331.623
    # 既有字段保持不变
    assert res["icao"] == "780A1B" and res["newer_frame"] == 2
    assert res["time_delta_ms"] == 5000


def test_api_antimeridian_motion_passes():
    pair = make_motion_pair(
        49.0, 179.99, 49.0, -179.99, ICAO, t_even_ms=T0, t_odd_ms=T0 + 5000
    )
    r = client.post(
        "/api/adsb/positions/decode", json=_payload("anti", pair, guard=1000)
    )
    body = r.json()
    assert body["ok_count"] == 1 and body["error_count"] == 0
    res = body["results"][0]["result"]
    assert res["distance_nm"] < 2.0
    assert res["ground_speed_kt"] < 1000.0


def test_api_over_limit_group_isolated_with_order_and_counts():
    good = make_motion_pair(
        40.0, 116.30, 40.0, 116.31, ICAO, t_even_ms=T0, t_odd_ms=T0 + 5000
    )
    jump = make_motion_pair(
        40.0, 116.40, 40.0, 116.50, ICAO, t_even_ms=T0, t_odd_ms=T0 + 500
    )
    same_ts = make_pair(40.0, 116.4, ICAO, t_even_ms=T0, t_odd_ms=T0)

    groups = [
        ("jump", jump),
        ("good", good),
        ("unresolved", same_ts),
    ]
    req = {"pairs": [_payload(pid, p)["pairs"][0] for pid, p in groups],
           "motion_guard": {"max_ground_speed_kt": 2000}}
    r = client.post("/api/adsb/positions/decode", json=req)
    assert r.status_code == 200
    body = r.json()
    assert body["ok_count"] == 1 and body["error_count"] == 2
    assert [x["id"] for x in body["results"]] == ["jump", "good", "unresolved"]

    jump_item, good_item, unresolved_item = body["results"]
    assert jump_item["status"] == "error"
    assert jump_item["error"]["code"] == "MOTION_LIMIT_EXCEEDED"
    assert jump_item["error"]["frame"] is None
    assert "result" not in jump_item

    assert unresolved_item["status"] == "error"
    assert unresolved_item["error"]["code"] == "MOTION_TIME_UNRESOLVED"
    assert unresolved_item["error"]["frame"] is None

    assert good_item["status"] == "ok"
    assert good_item["result"]["ground_speed_kt"] == 331.623


def test_api_adjudication_uses_unrounded_speed():
    pair = make_motion_pair(
        40.0, 116.30, 40.0, 116.304, ICAO, t_even_ms=T0, t_odd_ms=T0 + 2789
    )
    # 三位小数显示为 239.000，但阈值 239 仍须判超限
    r_fail = client.post(
        "/api/adsb/positions/decode", json=_payload("b", pair, guard=239)
    )
    item = r_fail.json()["results"][0]
    assert item["status"] == "error"
    assert item["error"]["code"] == "MOTION_LIMIT_EXCEEDED"

    r_ok = client.post(
        "/api/adsb/positions/decode", json=_payload("b", pair, guard=240)
    )
    res = r_ok.json()["results"][0]["result"]
    assert res["ground_speed_kt"] == 239.0
    assert res["distance_nm"] == 0.185


def test_api_threshold_endpoints_1_and_2000_accepted():
    # guard=1：(0,0) 同格静止对残差严格为零，可以通过最低阈值
    stationary = make_pair(0.0, 0.0, ICAO, t_even_ms=T0, t_odd_ms=T0 + 500)
    r = client.post(
        "/api/adsb/positions/decode", json=_payload("g0", stationary, guard=1)
    )
    assert r.status_code == 200
    assert r.json()["results"][0]["status"] == "ok"

    # guard=2000：正常运动对（约 332 kt）可以通过最高阈值
    moving = make_motion_pair(
        40.0, 116.30, 40.0, 116.31, ICAO, t_even_ms=T0, t_odd_ms=T0 + 5000
    )
    r = client.post(
        "/api/adsb/positions/decode", json=_payload("g2000", moving, guard=2000)
    )
    assert r.status_code == 200
    assert r.json()["results"][0]["status"] == "ok"


@pytest.mark.parametrize("value", [0, -1, 2001, 5000, 1.5, None])
def test_api_guard_validation(value):
    pair = make_pair(40.0, 116.4, ICAO, t_even_ms=T0, t_odd_ms=T0 + 500)
    req = _payload("g", pair)
    req["motion_guard"] = {"max_ground_speed_kt": value}
    r = client.post("/api/adsb/positions/decode", json=req)
    assert r.status_code == 422


def test_api_guard_unknown_field_rejected():
    pair = make_pair(40.0, 116.4, ICAO, t_even_ms=T0, t_odd_ms=T0 + 500)
    req = _payload("g", pair)
    req["motion_guard"] = {"other": 1}
    r = client.post("/api/adsb/positions/decode", json=req)
    assert r.status_code == 422
