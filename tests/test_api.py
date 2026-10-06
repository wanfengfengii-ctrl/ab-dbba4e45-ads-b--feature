"""接口层测试：批量裁决、坏组隔离、编号、校验与健康检查。"""
from __future__ import annotations

import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app  # noqa: E402
from tests.encoding import make_pair  # noqa: E402

client = TestClient(app)
T0 = 1_700_000_000_000


def _pair_payload(pid, lat=39.9, lon=116.4, t=T0, icao=0x780A1B):
    (t1, h1), (t2, h2) = make_pair(lat, lon, icao=icao, t_even_ms=t, t_odd_ms=t + 500)
    return {
        "id": pid,
        "frame1": {"received_at_ms": t1, "raw_hex": h1},
        "frame2": {"received_at_ms": t2, "raw_hex": h2},
    }


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_valid_pair_ok_fields():
    r = client.post("/api/adsb/positions/decode", json={"pairs": [_pair_payload("p1")]})
    assert r.status_code == 200
    body = r.json()
    assert body["ok_count"] == 1 and body["error_count"] == 0
    item = body["results"][0]
    assert item["id"] == "p1" and item["status"] == "ok"
    res = item["result"]
    assert res["icao"] == "780A1B"
    assert res["newer_frame"] == 2
    assert res["time_delta_ms"] == 500
    assert abs(res["latitude"] - 39.9) < 1e-4
    assert abs(res["longitude"] - 116.4) < 1e-4
    assert "error" not in item


def test_bad_group_does_not_mask_good_group():
    good = _pair_payload("good")
    bad = _pair_payload("bad")
    raw = bytearray.fromhex(bad["frame2"]["raw_hex"])
    raw[7] ^= 0x01  # 损坏数据位且不重算 CRC
    bad["frame2"]["raw_hex"] = raw.hex().upper()
    order = [bad, good]
    r = client.post("/api/adsb/positions/decode", json={"pairs": order})
    assert r.status_code == 200
    body = r.json()
    assert body["ok_count"] == 1 and body["error_count"] == 1
    assert [x["id"] for x in body["results"]] == ["bad", "good"]
    bad_item, good_item = body["results"]
    assert bad_item["status"] == "error"
    assert bad_item["error"]["code"] == "BAD_CRC"
    assert bad_item["error"]["frame"] == 2
    assert "result" not in bad_item
    assert good_item["status"] == "ok"


def test_all_error_codes_per_pair():
    cases = {
        "badcrc": lambda p: p["frame2"].__setitem__("raw_hex", "F" * 28),
        "len": lambda p: p["frame1"].__setitem__("raw_hex", "ABC"),
        "nonhex": lambda p: p["frame1"].__setitem__("raw_hex", "Z" * 28),
    }
    pairs = []
    for name, mutate in cases.items():
        p = _pair_payload(name)
        mutate(p)
        pairs.append(p)
    r = client.post("/api/adsb/positions/decode", json={"pairs": pairs})
    codes = {x["id"]: x["error"]["code"] for x in r.json()["results"]}
    assert codes == {"badcrc": "BAD_CRC", "len": "BAD_HEX", "nonhex": "BAD_HEX"}


def test_icao_mismatch_error():
    p = _pair_payload("icao")
    # 仅替换第二帧的 ICAO 字节并重算 CRC
    raw = bytearray.fromhex(p["frame2"]["raw_hex"])
    raw[1:4] = (0x123456).to_bytes(3, "big")
    from tests.encoding import fix_crc
    fix_crc(raw)
    p["frame2"]["raw_hex"] = raw.hex().upper()
    r = client.post("/api/adsb/positions/decode", json={"pairs": [p]})
    item = r.json()["results"][0]
    assert item["status"] == "error"
    assert item["error"]["code"] == "ICAO_MISMATCH"
    assert item["error"]["frame"] is None


def test_integer_ids_and_order_preserved():
    payload = {"pairs": [_pair_payload(7), _pair_payload("str-9"), _pair_payload(3)]}
    r = client.post("/api/adsb/positions/decode", json=payload)
    assert [x["id"] for x in r.json()["results"]] == [7, "str-9", 3]


def test_duplicate_ids_rejected():
    r = client.post(
        "/api/adsb/positions/decode",
        json={"pairs": [_pair_payload("dup"), _pair_payload("dup")]},
    )
    assert r.status_code == 422


@pytest.mark.parametrize("n", [0, 201])
def test_pair_count_bounds(n):
    pairs = [_pair_payload(f"i{i}") for i in range(n)]
    r = client.post("/api/adsb/positions/decode", json={"pairs": pairs})
    assert r.status_code == 422


def test_six_decimal_precision_and_wraparound():
    p = _pair_payload("wrap", lat=20.0, lon=179.999999)
    r = client.post("/api/adsb/positions/decode", json={"pairs": [p]})
    res = r.json()["results"][0]["result"]
    assert -180.0 <= res["longitude"] < 180.0
    # 六位小数：与 1e-6 网格对齐
    assert round(res["longitude"], 6) == res["longitude"]
    assert round(res["latitude"], 6) == res["latitude"]


def test_batch_of_200_performance_and_isolation():
    pairs = [_pair_payload(f"g{i}") for i in range(199)]
    bad = _pair_payload("bad200")
    raw = bytearray.fromhex(bad["frame1"]["raw_hex"])
    raw[5] ^= 0x80
    bad["frame1"]["raw_hex"] = raw.hex().upper()
    pairs.append(bad)
    r = client.post("/api/adsb/positions/decode", json={"pairs": pairs})
    body = r.json()
    assert body["ok_count"] == 199 and body["error_count"] == 1
    assert len(body["results"]) == 200
