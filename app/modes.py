"""Mode-S DF17 报文校验、解析与全球 CPR 位置解码。

仅依赖标准库。规则来源：ICAO Annex 10 Vol. IV（CRC）、RTCA DO-260B
A.1.7（全球 CPR），与 readsb/dump1090 的实现等价。
"""
from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass

# --- 常量 -----------------------------------------------------------------

MSG_LEN = 14          # DF17 长帧 14 字节 / 28 个十六进制字符
MSG_BITS = 112
CRC_BITS = 24
CRC_POLY = 0xFFF409   # Annex 10 生成多项式（不含最高次 x^24 项）

CPR_DENOM = 1 << 17   # CPR 经纬度字段分母 2^17
MAX_TIME_DELTA_MS = 10_000

DF_ADSB = 17
TC_AIRBORNE_POS_MIN = 9
TC_AIRBORNE_POS_MAX = 18  # DO-260B：BDS 0,5 空中位置 TC 9..18（TC19 为空速）


class ModesError(ValueError):
    """报文对裁决过程中的稳定错误。"""

    def __init__(self, code: str, message: str, frame: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        # 出错帧：1 / 2；成对比较类错误为 None
        self.frame = frame


# --- CRC ------------------------------------------------------------------

def crc_remainder(raw: bytes) -> int:
    """返回 112 位 Mode-S 报文的 24 位 CRC 余数；DF17 合法时为 0。"""
    rem = 0
    # 前 88 位（11 字节）为数据位，逐字节做多项式长除法
    for byte in raw[: MSG_LEN - 3]:
        rem ^= byte << 16
        for _ in range(8):
            if rem & 0x800000:
                rem = ((rem << 1) ^ CRC_POLY) & 0xFFFFFF
            else:
                rem = (rem << 1) & 0xFFFFFF
    parity = (raw[11] << 16) | (raw[12] << 8) | raw[13]
    return (rem ^ parity) & 0xFFFFFF


# --- CPR 纬度带表 ---------------------------------------------------------

# NL（经向带数）随纬度跳变的分界点（北纬取绝对值），由 DO-260B
# A.1.7.2 的闭式公式离线求出，升序排列。第 i 项表示 NL 由 59-i
# 变为 58-i 的纬度。87° 以上 NL=1。
_NL_BOUNDARIES: tuple[float, ...] = (
    10.47047129996848, 14.828174368686794, 18.186263570713354,
    21.029394926028463, 23.545044865570706, 25.829247070587755,
    27.938987101219045, 29.911356857318083, 31.77209707681077,
    33.53993436298484, 35.22899597796385, 36.85025107593526,
    38.41241892412256, 39.922566843338615, 41.38651832260239,
    42.80914012243555, 44.194549514192744, 45.546267226602346,
    46.867332524987454, 48.160391280966216, 49.42776439255687,
    50.67150165553835, 51.893424691687684, 53.09516152796003,
    54.278174722729, 55.44378444495043, 56.59318756205918,
    57.72747353866114, 58.84763776148457, 59.954592766940294,
    61.04917774246351, 62.13216659210329, 63.20427479381928,
    64.2661652256744, 65.31845309682089, 66.36171008382617,
    67.39646774084667, 68.4232202208333, 69.44242631144024,
    70.454510749876, 71.45986473028982, 72.45884544728945,
    73.45177441667865, 74.43893415725137, 75.42056256653356,
    76.39684390794469, 77.36789461328188, 78.33374082922747,
    79.29428225456925, 80.24923213280512, 81.19801349271948,
    82.13956980510606, 83.07199444719814, 83.99173562980565,
    84.89166190702085, 85.75541620944418, 86.535369975121,
    87.0,
)


def cpr_nl(lat: float) -> int:
    """纬度 lat 处的经度带数 NL（1..59），南纬按绝对值处理。"""
    a = abs(lat)
    if a > 87.0:
        return 1
    if a == 87.0:
        return 2
    return 59 - bisect_right(_NL_BOUNDARIES, a)


# --- 报文解析 -------------------------------------------------------------

@dataclass(frozen=True)
class AirborneFrame:
    """一帧合法的 DF17 空中位置报文。"""

    icao: int
    parity: int       # CPR/F 位：0=偶，1=奇
    cpr_lat: int     # 17 位原始纬度
    cpr_lon: int     # 17 位原始经度
    recv_ms: int


def parse_airborne_frame(raw: bytes, recv_ms: int, frame: int) -> AirborneFrame:
    """校验并解析一帧 DF17 空中位置报文。"""
    if crc_remainder(raw) != 0:
        raise ModesError("BAD_CRC", "CRC 校验失败，报文已损坏", frame)

    df = raw[0] >> 3
    if df != DF_ADSB:
        raise ModesError(
            "NOT_DF17", f"仅支持 DF17 空中位置报文，收到 DF{df}", frame
        )

    tc = raw[4] >> 3
    if not (TC_AIRBORNE_POS_MIN <= tc <= TC_AIRBORNE_POS_MAX):
        raise ModesError(
            "NOT_AIRBORNE_POSITION",
            f"DF17 类型码 TC={tc} 不是空中位置报文（应为 9..18）",
            frame,
        )

    icao = (raw[1] << 16) | (raw[2] << 8) | raw[3]
    parity = (raw[6] >> 2) & 1
    cpr_lat = ((raw[6] & 0x03) << 15) | (raw[7] << 7) | (raw[8] >> 1)
    cpr_lon = ((raw[8] & 0x01) << 16) | (raw[9] << 8) | raw[10]
    return AirborneFrame(icao, parity, cpr_lat, cpr_lon, recv_ms)


# --- 全球 CPR 解码 --------------------------------------------------------

@dataclass(frozen=True)
class DecodedPosition:
    icao: int
    latitude: float
    longitude: float
    newer_frame: int      # 位置取自哪一帧：1 / 2
    time_delta_ms: int


def decode_global_pair(f1: AirborneFrame, f2: AirborneFrame) -> DecodedPosition:
    """按 DO-260B 全球 CPR 解出较新一帧的经纬度。"""
    even, odd = (f1, f2) if f1.parity == 0 else (f2, f1)
    # 较新帧取时间戳更大者；两帧同刻时按帧序取 frame1，故用同一对象
    # 比较而非简单比较时间戳（同刻且 frame1 为奇帧时结果会相反）
    newer_is_even = even is (f1 if f1.recv_ms >= f2.recv_ms else f2)

    yz_even = even.cpr_lat / CPR_DENOM
    yz_odd = odd.cpr_lat / CPR_DENOM
    xz_even = even.cpr_lon / CPR_DENOM
    xz_odd = odd.cpr_lon / CPR_DENOM

    d_lat_even = 360.0 / 60.0
    d_lat_odd = 360.0 / 59.0

    j = math.floor(59.0 * yz_even - 60.0 * yz_odd + 0.5)
    r_lat_even = d_lat_even * ((j % 60) + yz_even)
    r_lat_odd = d_lat_odd * ((j % 59) + yz_odd)
    if r_lat_even >= 270.0:
        r_lat_even -= 360.0
    if r_lat_odd >= 270.0:
        r_lat_odd -= 360.0

    # 两帧必须落在同一纬度带（NL 一致），否则无法无歧义定位
    nl_even, nl_odd = cpr_nl(r_lat_even), cpr_nl(r_lat_odd)
    if nl_even != nl_odd:
        raise ModesError(
            "LATITUDE_ZONE_MISMATCH",
            f"两帧纬度带不一致（NL={nl_even} vs NL={nl_odd}），"
            "报文跨过分区分界或时间跨度过大",
        )

    if newer_is_even:
        lat = r_lat_even
        nl = nl_even
        ni = max(nl, 1)
        xz_newer = xz_even
    else:
        lat = r_lat_odd
        nl = nl_odd
        ni = max(nl - 1, 1)
        xz_newer = xz_odd

    m = math.floor(xz_even * (nl - 1) - xz_odd * nl + 0.5)
    lon = (360.0 / ni) * ((m % ni) + xz_newer)

    if not -90.0 <= lat <= 90.0 or lon < 0.0 or lon >= 360.0:
        # 公式上的不可能结果，通常源于被纬度带检查漏掉的坏对
        raise ModesError(
            "INVALID_POSITION", "CPR 解算结果超出地球经纬度范围"
        )

    # 归一化到 [-180, 180)：180° 与 180° 东侧一律折回西经表示
    if lon >= 180.0:
        lon -= 360.0

    newer_frame = 1 if f1.recv_ms >= f2.recv_ms else 2
    return DecodedPosition(
        icao=f1.icao,
        latitude=lat,
        longitude=lon,
        newer_frame=newer_frame,
        time_delta_ms=abs(f1.recv_ms - f2.recv_ms),
    )


# --- 报文对裁决入口 -------------------------------------------------------

def adjudicate(
    hex1: str, ts1: int, hex2: str, ts2: int
) -> DecodedPosition:
    """对一对原始报文执行完整裁决，失败抛 ModesError（带稳定错误码）。"""
    raw1 = _parse_hex(hex1, 1)
    raw2 = _parse_hex(hex2, 2)

    f1 = parse_airborne_frame(raw1, ts1, 1)
    f2 = parse_airborne_frame(raw2, ts2, 2)

    if f1.icao != f2.icao:
        raise ModesError(
            "ICAO_MISMATCH",
            f"两帧 ICAO 地址不一致：{f1.icao:06X} vs {f2.icao:06X}",
        )
    if f1.parity == f2.parity:
        raise ModesError(
            "PARITY_NOT_OPPOSITE",
            f"两帧 CPR 奇偶标志相同（均为{'偶' if f1.parity == 0 else '奇'}帧）",
        )
    delta = abs(ts1 - ts2)
    if delta > MAX_TIME_DELTA_MS:
        raise ModesError(
            "TIME_GAP_EXCEEDED",
            f"两帧接收时间相隔 {delta} ms，超过 {MAX_TIME_DELTA_MS} ms 上限",
        )

    return decode_global_pair(f1, f2)


def _parse_hex(text: str, frame: int) -> bytes:
    if len(text) != MSG_LEN * 2:
        raise ModesError(
            "BAD_HEX",
            f"原文必须为 {MSG_LEN * 2} 个十六进制字符，实际 {len(text)} 个",
            frame,
        )
    try:
        raw = bytes.fromhex(text)
    except ValueError:
        raise ModesError(
            "BAD_HEX", "原文含有非十六进制字符", frame
        ) from None
    return raw
