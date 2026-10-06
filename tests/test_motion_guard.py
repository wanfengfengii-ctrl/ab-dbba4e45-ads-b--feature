"""运动守卫单元测试：两帧逐位置解算、短弧位移/地速、阈值裁决。

覆盖：
- 两帧各自代表的位置（含奇偶帧序互换）
- 最短地表距离（大圆弧，跨日期变更线短弧）
- 阈值相等通过、超限拒绝、同刻 MOTION_TIME_UNRESOLVED
- 裁决使用未舍入值，展示值保留三位小数
"""
from __future__ import annotations

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.modes import (  # noqa: E402
    EARTH_RADIUS_NM,
    ModesError,
    adjudicate,
    cpr_nl,
    haversine_distance_nm,
)
from tests.encoding import make_moving_pair, make_pair  # noqa: E402

ICAO = 0x780A1B
T0 = 1_700_000_000_000


# --- 短弧地表距离 ---------------------------------------------------------

def test_haversine_zero_and_degree_length():
    assert haversine_distance_nm(30.0, 120.0, 30.0, 120.0) == 0.0
    # 赤道上 1 分经度 ≈ 1 海里（1 度 ≈ 60 海里，平均球面半径下 60.04）
    assert haversine_distance_nm(0.0, 10.0, 0.0, 11.0) == pytest.approx(
        60.0, abs=0.05
    )
    # 经线方向 1 度恒约 60 海里
    assert haversine_distance_nm(-40.0, -70.0, -39.0, -70.0) == pytest.approx(
        60.0, abs=0.05
    )


def test_haversine_symmetric():
    d1 = haversine_distance_nm(10.0, 20.0, 12.0, 23.0)
    d2 = haversine_distance_nm(12.0, 23.0, 10.0, 20.0)
    assert d1 == d2


@pytest.mark.parametrize(
    "lat,lon1,lon2",
    [
        (0.0, 179.999, -179.999),
        (20.0, 179.95, -179.95),
        (60.0, 178.0, -179.0),
        (86.9, 179.9, -179.95),
    ],
)
def test_haversine_antimeridian_short_arc(lat, lon1, lon2):
    """跨日期变更线必须取短弧，距离远小于绕行地球一周。"""
    short = haversine_distance_nm(lat, lon1, lat, lon2)
    naively_wrapped = haversine_distance_nm(lat, lon1, lat, lon2 + 360.0)
    # 两种写法等价：内部已取最短夹角（允许取模引入的极小浮点误差）
    assert short == pytest.approx(naively_wrapped, rel=1e-10, abs=1e-9)
    assert short < 120.0  # 短弧至多 2° 间距
    # 同方向沿纬线走完剩下的大圈远大于短弧（高纬例外，故用半周长比较）
    great_semicircle = math.pi * EARTH_RADIUS_NM * math.cos(math.radians(lat))
    assert short < great_semicircle


# --- 两帧各自位置 ---------------------------------------------------------

def test_frame_positions_match_encoded_locations():
    lat1, lon1, lat2, lon2 = 39.90, 116.40, 39.92, 116.43
    (t1, h1), (t2, h2) = make_moving_pair(
        lat1, lon1, lat2, lon2, ICAO, T0, T0 + 2000
    )
    pos = adjudicate(h1, t1, h2, t2)
    # 主位置取较新帧（frame2，奇帧）
    assert pos.newer_frame == 2
    assert pos.latitude == pytest.approx(pos.latitude_frame2, abs=1e-12)
    assert pos.longitude == pytest.approx(pos.longitude_frame2, abs=1e-12)
    assert pos.latitude_frame1 == pytest.approx(lat1, abs=1e-3)
    assert pos.longitude_frame1 == pytest.approx(lon1, abs=1e-3)
    assert pos.latitude_frame2 == pytest.approx(lat2, abs=1e-3)
    assert pos.longitude_frame2 == pytest.approx(lon2, abs=1e-3)


def test_frame_positions_when_even_frame_is_frame2():
    """frame1 为奇帧、frame2 为偶帧（较新）时，逐帧位置不得对错奇偶。"""
    lat1, lon1, lat2, lon2 = 48.0, 2.0, 48.01, 2.02
    (te, he), (to, ho) = make_moving_pair(
        lat2, lon2, lat1, lon1, ICAO, T0 + 3000, T0
    )
    # he 是偶帧且较新 → 作为 frame2；ho 是奇帧 → 作为 frame1
    pos = adjudicate(ho, T0, he, T0 + 3000)
    assert pos.newer_frame == 2
    assert pos.latitude_frame1 == pytest.approx(lat1, abs=1e-3)
    assert pos.longitude_frame1 == pytest.approx(lon1, abs=1e-3)
    assert pos.latitude_frame2 == pytest.approx(lat2, abs=1e-3)
    assert pos.longitude_frame2 == pytest.approx(lon2, abs=1e-3)


def test_per_frame_positions_cross_checked_with_pymodes():
    """两帧各自位置与 pyModeS 按 even_is_newer 两个方向的解算一致。"""
    from pyModeS.position import airborne_position_pair

    from app.modes import parse_airborne_frame

    lat1, lon1, lat2, lon2 = 10.0, -50.0, 10.02, -49.97
    (t1, h1), (t2, h2) = make_moving_pair(
        lat1, lon1, lat2, lon2, ICAO, T0, T0 + 1000
    )
    fe = parse_airborne_frame(bytes.fromhex(h1), t1, 1)
    fo = parse_airborne_frame(bytes.fromhex(h2), t2, 2)
    # even_is_newer=True 给出偶帧（frame1）位置，False 给出奇帧（frame2）位置
    ref_even = airborne_position_pair(
        fe.cpr_lat, fe.cpr_lon, fo.cpr_lat, fo.cpr_lon, even_is_newer=True
    )
    ref_odd = airborne_position_pair(
        fe.cpr_lat, fe.cpr_lon, fo.cpr_lat, fo.cpr_lon, even_is_newer=False
    )
    pos = adjudicate(h1, t1, h2, t2)
    assert pos.latitude_frame1 == pytest.approx(ref_even[0], abs=1e-9)
    assert pos.latitude_frame2 == pytest.approx(ref_odd[0], abs=1e-9)
    assert ((pos.longitude_frame1 - ref_even[1] + 540) % 360) - 180 == \
        pytest.approx(0.0, abs=1e-9)
    assert ((pos.longitude_frame2 - ref_odd[1] + 540) % 360) - 180 == \
        pytest.approx(0.0, abs=1e-9)


# --- 守卫关闭时语义不变 ---------------------------------------------------

def test_guard_disabled_leaves_result_untouched():
    (t1, h1), (t2, h2) = make_moving_pair(
        0.0, 0.0, 1.0, 1.0, ICAO, T0, T0 + 1
    )
    # 即便物理上不可能地速，未启用守卫时照常解算成功
    pos = adjudicate(h1, t1, h2, t2)
    assert pos.distance_nm is None
    assert pos.ground_speed_kt is None


def test_same_timestamp_ok_without_guard():
    (t1, h1), (t2, h2) = make_pair(35.0, 139.0, ICAO, t_even_ms=T0, t_odd_ms=T0)
    pos = adjudicate(h1, t1, h2, t2)
    assert pos.time_delta_ms == 0 and pos.newer_frame == 1


# --- 运动守卫裁决 ---------------------------------------------------------

def test_normal_motion_distance_and_speed():
    # 向北约 1.2 海里，5 秒 -> 约 860 节
    lat1, lon1 = 39.9, 116.4
    lat2 = lat1 + 1.2 / 60.0
    (t1, h1), (t2, h2) = make_moving_pair(
        lat1, lon1, lat2, lon1, ICAO, T0, T0 + 5000
    )
    pos = adjudicate(h1, t1, h2, t2, max_ground_speed_kt=2000)
    assert pos.distance_nm == pytest.approx(1.2, abs=1e-3)
    assert pos.ground_speed_kt == pytest.approx(
        pos.distance_nm * 3_600_000.0 / 5000.0, abs=1e-9
    )
    assert pos.ground_speed_kt == pytest.approx(864.0, abs=1.0)


def test_limit_exceeded():
    # 1 秒内向北 1 度（60 海里）=> 216000 节
    (t1, h1), (t2, h2) = make_moving_pair(
        20.0, 100.0, 21.0, 100.0, ICAO, T0, T0 + 1000
    )
    with pytest.raises(ModesError) as ei:
        adjudicate(h1, t1, h2, t2, max_ground_speed_kt=2000)
    assert ei.value.code == "MOTION_LIMIT_EXCEEDED"
    assert ei.value.frame is None


def test_limit_just_below_passes():
    # 0.05 度（约 3 海里）在 10 秒内 => 约 1080 节，低于 2000 上限通过
    (t1, h1), (t2, h2) = make_moving_pair(
        20.0, 100.0, 20.05, 100.0, ICAO, T0, T0 + 10_000
    )
    pos = adjudicate(h1, t1, h2, t2, max_ground_speed_kt=2000)
    assert pos.ground_speed_kt == pytest.approx(1080.0, abs=5.0)


def test_threshold_equality_passes():
    """地速与阈值严格相等（浮点同一值）时必须通过。"""
    (t1, h1), (t2, h2) = make_moving_pair(
        -10.0, -30.0, -9.99, -30.0, ICAO, T0, T0 + 1234
    )
    raw = adjudicate(h1, t1, h2, t2, max_ground_speed_kt=2000).ground_speed_kt
    pos = adjudicate(h1, t1, h2, t2, max_ground_speed_kt=raw)
    assert pos.ground_speed_kt == raw


def test_verdict_uses_unrounded_value():
    """原始地速 82.00023 节：整数阈值 82 必须拒绝，即使三位小数展示为 82.0。"""
    (t1, h1), (t2, h2) = make_moving_pair(
        30.0, 120.0, 30.001, 120.0, ICAO, T0, T0 + 2659
    )
    pos = adjudicate(h1, t1, h2, t2, max_ground_speed_kt=2000)
    assert 82.0 < pos.ground_speed_kt < 82.0005
    assert round(pos.ground_speed_kt, 3) == 82.0  # 展示值会落到阈值上
    with pytest.raises(ModesError) as ei:
        adjudicate(h1, t1, h2, t2, max_ground_speed_kt=82)
    assert ei.value.code == "MOTION_LIMIT_EXCEEDED"
    # 83 节阈值下同一报文通过
    assert adjudicate(h1, t1, h2, t2, max_ground_speed_kt=83).ground_speed_kt > 82.0


def test_same_timestamp_motion_time_unresolved():
    lat1, lon1, lat2, lon2 = 35.0, 139.0, 35.01, 139.0
    (_, h1), (_, h2) = make_moving_pair(
        lat1, lon1, lat2, lon2, ICAO, T0, T0
    )
    with pytest.raises(ModesError) as ei:
        adjudicate(h1, T0, h2, T0, max_ground_speed_kt=2000)
    assert ei.value.code == "MOTION_TIME_UNRESOLVED"
    assert ei.value.frame is None


def test_zero_distance_same_timestamp_still_unresolved():
    """即使两帧位置完全相同，同刻也算不出地速 → MOTION_TIME_UNRESOLVED。"""
    (_, h1), (_, h2) = make_pair(40.0, 116.0, ICAO, t_even_ms=T0, t_odd_ms=T0)
    with pytest.raises(ModesError) as ei:
        adjudicate(h1, T0, h2, T0, max_ground_speed_kt=2000)
    assert ei.value.code == "MOTION_TIME_UNRESOLVED"


def test_antimeridian_motion_gets_sane_speed():
    """跨日期变更线相邻位置按短弧得到合理速度（高纬 NL=2 带）。

    偶帧 179.9°E、奇帧 179.95°W，1 秒间隔；短弧不足 0.5 海里，
    2000 节阈值下必须通过，而不是被当成绕地球一圈的大跃迁。
    """
    lat = 86.9
    assert cpr_nl(lat) == 2
    (t1, h1), (t2, h2) = make_moving_pair(
        lat, 179.9, lat, -179.95, ICAO, T0, T0 + 1000, nl=2
    )
    pos = adjudicate(h1, t1, h2, t2, max_ground_speed_kt=2000)
    assert pos.distance_nm < 1.0
    assert pos.ground_speed_kt < 2000.0
    assert pos.ground_speed_kt == pytest.approx(
        pos.distance_nm * 3600.0, abs=1e-6
    )


def test_antimeridian_motion_equator_short_arc():
    """赤道附近跨线：0.002° 的短弧约 0.12 海里 / 1 秒 ≈ 430 节。"""
    (t1, h1), (t2, h2) = make_moving_pair(
        0.0, 179.999, 0.0, -179.999, ICAO, T0, T0 + 1000
    )
    pos = adjudicate(h1, t1, h2, t2, max_ground_speed_kt=2000)
    assert pos.distance_nm == pytest.approx(0.12, abs=0.05)
    assert pos.ground_speed_kt < 600.0
