"""全球 CPR 解码测试：正常位置、跨日期变更线、纬度分区边界、错误裁决。

真值对照采用 pyModeS 的独立实现（tests/encoding.py 的编码器与生产
解码器同源，因此还需要一个独立实现做交叉验证）。
"""
from __future__ import annotations

import math
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.modes import (  # noqa: E402
    ModesError,
    adjudicate,
    cpr_nl,
)
from tests.encoding import angular_lon_diff, make_pair  # noqa: E402

ICAO = 0x780A1B
T0 = 1_700_000_000_000  # 固定基准毫秒，避免依赖“当前时间”

# 覆盖各半球、各纬度带与经度区间的代表位置（十进制度）
SAMPLE_POSITIONS = [
    (39.9042, 116.4074),      # 北京
    (51.4700, -0.4543),       # 伦敦（西经）
    (-33.8688, 151.2093),     # 悉尼（南半球）
    (1.3592, 103.9894),       # 新加坡（低纬 NL=59）
    (64.1283, -21.9426),      # 雷克雅未克（高纬 NL 小）
    (0.0, 0.0),
    (-86.9, 45.0),            # 近南极 NL=2
    (86.9, -45.0),
    (25.0799, 121.2342),      # 台北
    (-1.4558, -48.5016),      # 贝伦（南美）
]


@pytest.mark.parametrize("lat,lon", SAMPLE_POSITIONS)
def test_decode_matches_true_position(lat, lon):
    (t1, h1), (t2, h2) = make_pair(lat, lon, ICAO, t_even_ms=T0, t_odd_ms=T0 + 500)
    pos = adjudicate(h1, t1, h2, t2)
    assert pos.latitude == pytest.approx(lat, abs=1e-4)
    assert angular_lon_diff(pos.longitude, lon) == pytest.approx(0.0, abs=1e-4)
    assert pos.newer_frame == 2
    assert pos.time_delta_ms == 500


@pytest.mark.parametrize("lat,lon", SAMPLE_POSITIONS)
def test_decode_matches_pymodes_reference(lat, lon):
    """与 pyModeS.airborne_position_pair 的结果逐位对照。"""
    from pyModeS.position import airborne_position_pair

    from app.modes import parse_airborne_frame

    (t1, h1), (t2, h2) = make_pair(lat, lon, ICAO, t_even_ms=T0)
    fe = parse_airborne_frame(bytes.fromhex(h1), t1, 1)
    fo = parse_airborne_frame(bytes.fromhex(h2), t2, 2)
    ref = airborne_position_pair(
        fe.cpr_lat, fe.cpr_lon, fo.cpr_lat, fo.cpr_lon, even_is_newer=False
    )
    assert ref is not None
    pos = adjudicate(h1, t1, h2, t2)
    assert pos.latitude == pytest.approx(ref[0], abs=1e-9)
    assert pos.longitude == pytest.approx(ref[1], abs=1e-9)


def test_newer_frame_selection_even_newer():
    # 偶帧较新：位置应取自偶帧
    lat, lon = 48.8566, 2.3522
    (t1, h1), (t2, h2) = make_pair(lat, lon, ICAO, t_even_ms=T0 + 800, t_odd_ms=T0)
    pos = adjudicate(h1, T0 + 800, h2, T0)
    assert pos.newer_frame == 1
    assert pos.latitude == pytest.approx(lat, abs=1e-4)
    assert pos.longitude == pytest.approx(lon, abs=1e-4)


def test_same_timestamp_first_is_newer():
    lat, lon = 35.6895, 139.6917
    (_, h1), (_, h2) = make_pair(lat, lon, ICAO, t_even_ms=T0, t_odd_ms=T0)
    pos = adjudicate(h1, T0, h2, T0)
    assert pos.newer_frame == 1


@pytest.mark.parametrize(
    "lon",
    [179.9999, -179.9999, 179.5, -179.5, 180.0 - 1e-6, -180.0 + 1e-6],
)
def test_antimeridian_stability(lon):
    """跨日期变更线两侧的结果必须稳定落在正确半球。"""
    lat = 40.0
    (t1, h1), (t2, h2) = make_pair(lat, lon, ICAO, t_even_ms=T0)
    pos = adjudicate(h1, t1, h2, t2)
    assert -180.0 <= pos.longitude < 180.0
    # 距目标经度的最短经度差应极小（而不是相差 ~360° 的镜像）
    assert angular_lon_diff(pos.longitude, lon) < 1e-3
    assert pos.latitude == pytest.approx(lat, abs=1e-4)


def test_antimeridian_two_sides_distinct_hemispheres():
    # +179.9 与 -179.9 实际相距仅 0.2°，但分别在东西半球，不能镜像翻转
    (t1, h1), (t2, h2) = make_pair(20.0, 179.9, t_even_ms=T0)
    pe = adjudicate(h1, t1, h2, t2)
    (t1, h1), (t2, h2) = make_pair(20.0, -179.9, t_even_ms=T0)
    pw = adjudicate(h1, t1, h2, t2)
    assert pe.longitude > 0
    assert pw.longitude < 0
    assert angular_lon_diff(pe.longitude, pw.longitude) < 0.5


def test_nl_boundary_table_matches_formula():
    """NL 分界表与 DO-260B 闭式公式在密集网格上一致。"""
    def nl_formula(lat: float) -> int:
        if abs(lat) >= 87.0:
            return 1 if abs(lat) > 87.0 else 2
        nz = 1 - math.cos(math.pi / 30.0)
        d = math.cos(math.radians(lat))
        n = math.acos(1 - nz / (d * d))
        return max(1, int(math.floor(2 * math.pi / n)))

    for i in range(-8700, 8701):
        lat = i / 100.0
        assert cpr_nl(lat) == nl_formula(lat), lat


@pytest.mark.parametrize(
    "boundary",
    [
        10.47047129996848, 14.828174368686794, 21.029394926028463,
        36.85025107593526, 51.893424691687684, 64.2661652256744,
        74.43893415725137, 82.13956980510606,
    ],
)
def test_decode_near_latitude_zone_boundary(boundary):
    """纬度分区边界两侧 ±0.005° 内的报文对应能稳定解出。"""
    for lat in (boundary - 0.005, boundary + 0.005):
        for lon in (-120.0, 130.0, 5.0):
            (t1, h1), (t2, h2) = make_pair(lat, lon, t_even_ms=T0)
            pos = adjudicate(h1, t1, h2, t2)
            assert abs(pos.latitude - lat) < 1e-3
            assert abs(pos.longitude - lon) < 1e-3


def test_random_positions_against_reference():
    rng = random.Random(424242)
    from pyModeS.position import airborne_position_pair
    from app.modes import parse_airborne_frame

    for _ in range(120):
        lat = rng.uniform(-86.9, 86.9)
        lon = rng.uniform(-179.999, 179.999)
        nl = cpr_nl(lat)
        (t1, h1), (t2, h2) = make_pair(lat, lon, t_even_ms=T0, nl=nl)
        fe = parse_airborne_frame(bytes.fromhex(h1), t1, 1)
        fo = parse_airborne_frame(bytes.fromhex(h2), t2, 2)
        ref = airborne_position_pair(
            fe.cpr_lat, fe.cpr_lon, fo.cpr_lat, fo.cpr_lon, even_is_newer=False
        )
        pos = adjudicate(h1, t1, h2, t2)
        assert ref is not None
        assert pos.latitude == pytest.approx(ref[0], abs=1e-9)
        assert pos.longitude == pytest.approx(ref[1], abs=1e-9)


# --- 错误裁决 -------------------------------------------------------------

def _flip_bit(hexmsg: str, byte: int, bitmask: int = 0x01) -> str:
    b = bytearray.fromhex(hexmsg)
    b[byte] ^= bitmask
    return b.hex().upper()


def test_error_bad_crc_marked_frame():
    (t1, h1), (t2, h2) = make_pair(30.0, 120.0, t_even_ms=T0)
    h2 = _flip_bit(h2, 7)
    with pytest.raises(ModesError) as ei:
        adjudicate(h1, t1, h2, t2)
    assert ei.value.code == "BAD_CRC"
    assert ei.value.frame == 2


def test_error_icao_mismatch():
    (t1, h1), _ = make_pair(30.0, 120.0, icao=0xAAAAAA, t_even_ms=T0)
    _, (t2, h2) = make_pair(30.0, 120.0, icao=0xBBBBBB, t_even_ms=T0)
    with pytest.raises(ModesError) as ei:
        adjudicate(h1, t1, h2, t2)
    assert ei.value.code == "ICAO_MISMATCH"
    assert ei.value.frame is None


def test_error_same_parity():
    from tests.encoding import make_airborne_msg

    yz, xz = 1000, 2000
    m1 = bytes(make_airborne_msg(ICAO, yz, xz, 0)).hex().upper()
    m2 = bytes(make_airborne_msg(ICAO, yz + 50, xz + 50, 0)).hex().upper()
    with pytest.raises(ModesError) as ei:
        adjudicate(m1, T0, m2, T0 + 200)
    assert ei.value.code == "PARITY_NOT_OPPOSITE"


def test_error_time_gap_exceeded():
    (t1, h1), (t2, h2) = make_pair(
        30.0, 120.0, t_even_ms=T0, t_odd_ms=T0 + 10_001
    )
    with pytest.raises(ModesError) as ei:
        adjudicate(h1, t1, h2, t2)
    assert ei.value.code == "TIME_GAP_EXCEEDED"


def test_time_gap_boundary_10s_accepted():
    (t1, h1), (t2, h2) = make_pair(30.0, 120.0, t_even_ms=T0, t_odd_ms=T0 + 10_000)
    pos = adjudicate(h1, t1, h2, t2)
    assert pos.time_delta_ms == 10_000


def test_error_latitude_zone_mismatch():
    """两帧编码位置分别落在 NL 不同的纬度带 → LATITUDE_ZONE_MISMATCH。

    用边界两侧的真实点各自编码，再强行配成一对。
    """
    from tests.encoding import encode_cpr
    from tests.encoding import make_airborne_msg

    nl_lo, nl_hi = 59, 58
    yz0, xz0 = encode_cpr(10.46, 100.0, 0, nl_lo)
    yz1, xz1 = encode_cpr(10.49, 100.0, 1, nl_hi)
    m0 = bytes(make_airborne_msg(ICAO, yz0, xz0, 0)).hex().upper()
    m1 = bytes(make_airborne_msg(ICAO, yz1, xz1, 1)).hex().upper()
    with pytest.raises(ModesError) as ei:
        adjudicate(m0, T0, m1, T0 + 300)
    assert ei.value.code == "LATITUDE_ZONE_MISMATCH"


def test_error_bad_hex_length():
    with pytest.raises(ModesError) as ei:
        adjudicate("8D406B90", T0, "8D406B902015A678D4D220AA4BDA", T0)
    assert ei.value.code == "BAD_HEX"
    assert ei.value.frame == 1


def test_published_real_world_pair():
    """公开流传的 dump1090 教学用真实 DF17 报文对（ICAO 40621D）。

    注意：网上常见版本偶帧末字节 A5 实为转录错误（CRC 余数 2），
    正确字节为 A7；本测试同时固定这一事实。
    """
    even = "8D40621D58C382D690C8AC2863A7"
    odd = "8D40621D58C386435CC412692AD6"
    pos = adjudicate(even, 1000, odd, 2000)
    assert pos.icao == 0x40621D
    assert pos.latitude == pytest.approx(52.26578017412606, abs=1e-6)
    assert pos.longitude == pytest.approx(3.938912527901786, abs=1e-6)
    assert pos.newer_frame == 2
    assert pos.time_delta_ms == 1000

    # 转录错误版本必须被 CRC 拦截
    with pytest.raises(ModesError) as ei:
        adjudicate("8D40621D58C382D690C8AC2863A5", 1000, odd, 2000)
    assert ei.value.code == "BAD_CRC"
