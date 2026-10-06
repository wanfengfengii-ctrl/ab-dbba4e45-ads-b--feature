"""接口层测试：批量裁决、坏组隔离、编号、校验与健康检查。"""
from __future__ import annotations

import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app  # noqa: E402
from tests.encoding import make_moving_pair, make_pair  # noqa: E402

client = TestClient(app)
T0 = 1_700_000_000_000


def _pair_payload(pid, lat=39.9, lon=116.4, t=T0, icao=0x780A1B):
    (t1, h1), (t2, h2) = make_pair(lat, lon, icao=icao, t_even_ms=t, t_odd_ms=t + 500)
    return {
        "id": pid,
        "frame1": {"received_at_ms": t1, "raw_hex": h1},
        "frame2": {"received_at_ms": t2, "raw_hex": h2},
    }


def _moving_payload(pid, lat1, lon1, lat2, lon2, t1=T0, t2=T0 + 1000, icao=0x780A1B):
    (ta, h1), (tb, h2) = make_moving_pair(
        lat1, lon1, lat2, lon2, icao=icao, t_even_ms=t1, t_odd_ms=t2
    )
    return {
        "id": pid,
        "frame1": {"received_at_ms": ta, "raw_hex": h1},
        "frame2": {"received_at_ms": tb, "raw_hex": h2},
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


# --- 运动守卫 motion_guard ------------------------------------------------

def test_motion_guard_omitted_response_unchanged():
    """省略 motion_guard 时请求、响应与错误语义保持不变。"""
    r = client.post(
        "/api/adsb/positions/decode", json={"pairs": [_pair_payload("p1")]}
    )
    assert r.status_code == 200
    res = r.json()["results"][0]["result"]
    assert "distance_nm" not in res
    assert "ground_speed_kt" not in res


def test_motion_guard_normal_motion_fields():
    # 10 秒内向北约 1.2 海里 => 约 430 节
    lat1 = 39.9
    lat2 = lat1 + 1.2 / 60.0
    payload = {
        "pairs": [
            _moving_payload("moving", lat1, 116.4, lat2, 116.4, T0, T0 + 10_000)
        ],
        "motion_guard": {"max_ground_speed_kt": 2000},
    }
    r = client.post("/api/adsb/positions/decode", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["ok_count"] == 1 and body["error_count"] == 0
    res = body["results"][0]["result"]
    assert res["distance_nm"] == pytest.approx(1.2, abs=1e-3)
    assert res["ground_speed_kt"] == pytest.approx(432.0, abs=1.0)
    # 保留三位小数：与 1e-3 网格对齐
    assert round(res["distance_nm"], 3) == res["distance_nm"]
    assert round(res["ground_speed_kt"], 3) == res["ground_speed_kt"]


def test_motion_guard_limit_exceeded_isolated_and_ordered():
    """超限组只影响所在组：计数、顺序与其他组均保持。"""
    good = _moving_payload("ok", 20.0, 100.0, 20.01, 100.0, T0, T0 + 10_000)
    fast = _moving_payload("fast", 20.0, 100.0, 21.0, 100.0, T0, T0 + 1000)
    also_ok = _pair_payload("static")
    payload = {
        "pairs": [good, fast, also_ok],
        "motion_guard": {"max_ground_speed_kt": 2000},
    }
    r = client.post("/api/adsb/positions/decode", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["ok_count"] == 2 and body["error_count"] == 1
    assert [x["id"] for x in body["results"]] == ["ok", "fast", "static"]
    item = body["results"][1]
    assert item["status"] == "error"
    assert item["error"]["code"] == "MOTION_LIMIT_EXCEEDED"
    assert item["error"]["frame"] is None
    assert "result" not in item
    for i in (0, 2):
        assert body["results"][i]["status"] == "ok"
        assert "distance_nm" in body["results"][i]["result"]


def test_motion_guard_same_timestamp_unresolved():
    p = _moving_payload("same", 35.0, 139.0, 35.01, 139.0, T0, T0)
    payload = {
        "pairs": [p],
        "motion_guard": {"max_ground_speed_kt": 2000},
    }
    r = client.post("/api/adsb/positions/decode", json=payload)
    body = r.json()
    assert body["ok_count"] == 0 and body["error_count"] == 1
    err = body["results"][0]["error"]
    assert err["code"] == "MOTION_TIME_UNRESOLVED"
    assert err["frame"] is None


def test_motion_guard_threshold_equality_passes():
    """阈值相等通过：地速 82 节整阈值下，82.x 被拒、精确相等放行由单元测试覆盖；
    此处固定一个明显低于阈值的报文并以其精确速度为阈值，验证边界放行。"""
    p = _moving_payload("eq", -10.0, -30.0, -9.99, -30.0, T0, T0 + 1234)
    # 先用大阈值拿到真实地速（字段为三位小数，服务端裁决并不使用该舍入值）
    pre = client.post(
        "/api/adsb/positions/decode",
        json={"pairs": [p], "motion_guard": {"max_ground_speed_kt": 2000}},
    ).json()
    gs = pre["results"][0]["result"]["ground_speed_kt"]
    # 高一个整节的阈值必然通过
    r = client.post(
        "/api/adsb/positions/decode",
        json={"pairs": [p], "motion_guard": {"max_ground_speed_kt": int(gs) + 1}},
    )
    assert r.json()["results"][0]["status"] == "ok"


def test_motion_guard_antimeridian_reasonable_speed():
    """跨日期变更线相邻位置按短弧得到合理速度，高纬 NL=2 带同样成立。"""
    p = _moving_payload("cross", 86.9, 179.9, 86.9, -179.95, T0, T0 + 1000)
    r = client.post(
        "/api/adsb/positions/decode",
        json={"pairs": [p], "motion_guard": {"max_ground_speed_kt": 2000}},
    )
    body = r.json()
    assert body["ok_count"] == 1, body
    res = body["results"][0]["result"]
    assert res["distance_nm"] < 1.0
    assert res["ground_speed_kt"] < 2000.0


@pytest.mark.parametrize("v", [0, -1, 2001, 20000, "fast", 1.5, None])
def test_motion_guard_bounds_validation(v):
    payload = {
        "pairs": [_pair_payload("p")],
        "motion_guard": {"max_ground_speed_kt": v},
    }
    r = client.post("/api/adsb/positions/decode", json=payload)
    assert r.status_code == 422


def test_motion_guard_null_means_disabled():
    """motion_guard: null 等同于省略，响应无运动字段。"""
    r = client.post(
        "/api/adsb/positions/decode",
        json={"pairs": [_pair_payload("p")], "motion_guard": None},
    )
    res = r.json()["results"][0]["result"]
    assert "distance_nm" not in res


def test_motion_guard_does_not_change_upstream_errors():
    """守卫启用后，上游错误码与帧编号语义不变。"""
    bad = _pair_payload("bad")
    raw = bytearray.fromhex(bad["frame2"]["raw_hex"])
    raw[7] ^= 0x01
    bad["frame2"]["raw_hex"] = raw.hex().upper()
    r = client.post(
        "/api/adsb/positions/decode",
        json={"pairs": [bad], "motion_guard": {"max_ground_speed_kt": 2000}},
    )
    err = r.json()["results"][0]["error"]
    assert err["code"] == "BAD_CRC" and err["frame"] == 2
