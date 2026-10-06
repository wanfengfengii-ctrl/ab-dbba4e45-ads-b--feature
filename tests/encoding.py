"""测试与冒烟脚本共用的 DF17 空中位置报文构造工具。

仅用于测试：把指定 CPR 字段与 ICAO 组装成报文并重算 CRC。
"""
from __future__ import annotations

import math

from app.modes import CPR_DENOM, crc_remainder


def cpr_mod(a: float, b: float) -> float:
    """非负模：结果恒在 [0, b)，与 CPR 编码的跨日期线处理一致。"""
    return a - b * math.floor(a / b)


def encode_cpr(lat: float, lon: float, parity: int, nl: int) -> tuple[int, int]:
    """按已知位置生成偶/奇帧的 17 位 CPR 经纬度字段。"""
    d_lat = 360.0 / 59.0 if parity else 360.0 / 60.0
    yz = cpr_mod(lat, d_lat)
    cpr_lat = int(yz / d_lat * CPR_DENOM + 0.5) % CPR_DENOM

    ni = nl if parity == 0 else max(nl - 1, 1)
    d_lon = 360.0 / ni
    xz = cpr_mod(lon, d_lon)
    cpr_lon = int(xz / d_lon * CPR_DENOM + 0.5) % CPR_DENOM
    return cpr_lat, cpr_lon


def fix_crc(msg: bytearray) -> bytearray:
    """清空并重算最后 3 字节 CRC，使 DF17 报文合法。"""
    msg[11] = msg[12] = msg[13] = 0
    rem = crc_remainder(msg)
    msg[11] = (rem >> 16) & 0xFF
    msg[12] = (rem >> 8) & 0xFF
    msg[13] = rem & 0xFF
    return msg


def make_airborne_msg(
    icao: int,
    cpr_lat: int,
    cpr_lon: int,
    parity: int,
    tc: int = 11,
    ac12: int = 0b000101101000,  # 高度码占位，裁决不使用
) -> bytearray:
    """组装一帧 DF17/BDS0,5 空中位置报文（14 字节，含正确 CRC）。

    ME(56bit) 布局（dump1090/DO-260B 一致）：
    TC5 | SS2/NICsb1(3) | AC12 | T1 | F1 | LAT17 | LON17。
    """
    msg = bytearray(14)
    msg[0] = (17 << 3) | 5  # DF17，CA=5
    msg[1] = (icao >> 16) & 0xFF
    msg[2] = (icao >> 8) & 0xFF
    msg[3] = icao & 0xFF
    me = (
        (tc << 51)
        | (ac12 << 36)
        | (parity << 34)
        | (cpr_lat << 17)
        | cpr_lon
    )
    msg[4:11] = me.to_bytes(7, "big")
    return fix_crc(msg)


def make_pair(
    lat: float,
    lon: float,
    icao: int = 0x780A1B,
    nl: int | None = None,
    t_even_ms: int = 1_000_000,
    t_odd_ms: int | None = None,
) -> tuple[tuple[int, str], tuple[int, str]]:
    """生成同一位置的一偶一奇两帧，返回 ((ts, hex), (ts, hex))。"""
    from app.modes import cpr_nl

    if nl is None:
        nl = cpr_nl(lat)
    if t_odd_ms is None:
        t_odd_ms = t_even_ms + 500
    yz0, xz0 = encode_cpr(lat, lon, 0, nl)
    yz1, xz1 = encode_cpr(lat, lon, 1, nl)
    even = make_airborne_msg(icao, yz0, xz0, 0)
    odd = make_airborne_msg(icao, yz1, xz1, 1)
    return (t_even_ms, even.hex().upper()), (t_odd_ms, odd.hex().upper())


def make_moving_pair(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
    icao: int = 0x780A1B,
    t_even_ms: int = 1_000_000,
    t_odd_ms: int | None = None,
    nl: int | None = None,
) -> tuple[tuple[int, str], tuple[int, str]]:
    """生成两个不同位置的一偶一奇两帧（运动报文对）。

    偶帧编码 (lat1, lon1)，奇帧编码 (lat2, lon2)。两位置必须落在同一
    CPR 纬度带（NL 一致），否则不构成合法全球解算对。
    """
    from app.modes import cpr_nl

    if nl is None:
        nl = cpr_nl(lat1)
    if cpr_nl(lat2) != nl:
        raise ValueError("两帧位置必须处于同一纬度带（NL 一致）")
    if t_odd_ms is None:
        t_odd_ms = t_even_ms + 500
    yz0, xz0 = encode_cpr(lat1, lon1, 0, nl)
    yz1, xz1 = encode_cpr(lat2, lon2, 1, nl)
    even = make_airborne_msg(icao, yz0, xz0, 0)
    odd = make_airborne_msg(icao, yz1, xz1, 1)
    return (t_even_ms, even.hex().upper()), (t_odd_ms, odd.hex().upper())


def angular_lon_diff(a: float, b: float) -> float:
    """两个经度之间的最小夹角（度），跨 ±180 回绕安全。"""
    return abs((a - b + 180.0) % 360.0 - 180.0)
