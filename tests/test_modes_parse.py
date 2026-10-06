"""CRC、DF17 解析与错误码单元测试。"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.modes import (  # noqa: E402
    ModesError,
    crc_remainder,
    parse_airborne_frame,
)
from tests.encoding import fix_crc, make_airborne_msg  # noqa: E402


def test_crc_known_zero_remainder():
    # pyModeS 文档中的 DF17 示例，合法报文余数为 0
    msg = bytes.fromhex("8D406B902015A678D4D220AA4BDA")
    assert crc_remainder(msg) == 0


def test_crc_matches_reference():
    """与 pyModeS 的 CRC 实现逐字节随机对照。"""
    from pyModeS._bits import crc_remainder as ref_crc

    rng = __import__("random").Random(20261006)
    for _ in range(300):
        raw = bytes(rng.getrandbits(8) for _ in range(14))
        assert crc_remainder(raw) == ref_crc(int.from_bytes(raw, "big"), 112)


def test_parse_valid_frame_fields():
    msg = bytes(make_airborne_msg(0x780A1B, cpr_lat=12345, cpr_lon=67890, parity=1))
    f = parse_airborne_frame(msg, 1000, 1)
    assert f.icao == 0x780A1B
    assert f.parity == 1
    assert f.cpr_lat == 12345
    assert f.cpr_lon == 67890
    assert f.recv_ms == 1000


def _corrupt(msg: bytearray) -> bytearray:
    msg[5] ^= 0x01
    return msg  # 不改 CRC，制造余数非 0


def test_bad_crc():
    msg = bytes(_corrupt(make_airborne_msg(0xABCDEF, 1, 2, 0)))
    with pytest.raises(ModesError) as ei:
        parse_airborne_frame(msg, 0, 2)
    assert ei.value.code == "BAD_CRC"
    assert ei.value.frame == 2


def test_non_df17():
    msg = bytearray(make_airborne_msg(0xABCDEF, 1, 2, 0))
    msg[0] = (18 << 3) | 1  # DF18
    fix_crc(msg)
    with pytest.raises(ModesError) as ei:
        parse_airborne_frame(msg, 0, 1)
    assert ei.value.code == "NOT_DF17"
    assert ei.value.frame == 1


@pytest.mark.parametrize("tc", [0, 1, 8, 19, 20, 28])
def test_wrong_type_code(tc):
    msg = bytes(make_airborne_msg(0xABCDEF, 1, 2, 0, tc=tc))
    with pytest.raises(ModesError) as ei:
        parse_airborne_frame(msg, 0, 1)
    assert ei.value.code == "NOT_AIRBORNE_POSITION"


@pytest.mark.parametrize("tc", [9, 11, 18])
def test_airborne_type_codes_accepted(tc):
    msg = bytes(make_airborne_msg(0xABCDEF, 1, 2, 0, tc=tc))
    assert parse_airborne_frame(msg, 0, 1).icao == 0xABCDEF
