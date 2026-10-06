"""FastAPI 应用：成对 ADS-B 空中位置报文裁决接口。"""
from __future__ import annotations

import logging

from fastapi import FastAPI

from app.modes import ModesError, adjudicate, motion_estimate
from app.schemas import DecodeRequest

logger = logging.getLogger("adsb")

app = FastAPI(
    title="ADS-B 成对位置报文裁决 API",
    version="1.0.0",
    description="雷达融合前的 DF17 空中位置报文对 CRC/一致性核对与全球 CPR 解算",
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/adsb/positions/decode")
def decode_positions(req: DecodeRequest) -> dict:
    results: list[dict] = []
    ok_count = 0
    error_count = 0

    # 逐组独立裁决：任何一组失败都不影响同批其他组
    guard = req.motion_guard
    max_gs = guard.max_ground_speed_kt if guard is not None else None
    for pair in req.pairs:
        try:
            # 阈值裁决（相等通过、不使用舍入值）在 adjudicate 内完成，
            # 与单元测试走同一条路径；启用守卫时同刻抛 MOTION_TIME_UNRESOLVED
            pos = adjudicate(
                pair.frame1.raw_hex,
                pair.frame1.received_at_ms,
                pair.frame2.raw_hex,
                pair.frame2.received_at_ms,
                max_ground_speed_kt=max_gs,
            )
            distance_nm: float | None = None
            ground_speed_kt: float | None = None
            if guard is not None:
                # 守卫已放行，这里仅取未舍入运动量用于三位小数展示
                distance_nm, ground_speed_kt = motion_estimate(pos)
        except ModesError as exc:
            error_count += 1
            results.append(
                {
                    "id": pair.id,
                    "status": "error",
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                        "frame": exc.frame,
                    },
                }
            )
        except Exception:  # noqa: BLE001 - 隔离未知解析异常，避免遮蔽同批其他组
            error_count += 1
            logger.exception("pair %s 裁决时发生未预期异常", pair.id)
            results.append(
                {
                    "id": pair.id,
                    "status": "error",
                    "error": {
                        "code": "INTERNAL_ERROR",
                        "message": "服务端解算异常",
                        "frame": None,
                    },
                }
            )
        else:
            ok_count += 1
            lat = round(pos.latitude, 6) + 0.0
            lon = round(pos.longitude, 6)
            # 取整可能把 179.999999x 抬到 180.000000，取整后再归一化一次，
            # 保证经度严格落在 [-180, 180)，并消除 -0.0
            if lon >= 180.0:
                lon -= 360.0
            elif lon < -180.0:
                lon += 360.0
            lon += 0.0
            result = {
                "icao": f"{pos.icao:06X}",
                "latitude": lat,
                "longitude": lon,
                "newer_frame": pos.newer_frame,
                "time_delta_ms": pos.time_delta_ms,
            }
            if guard is not None:
                # 输出保留三位小数；裁决使用的是上面的未舍入值
                result["distance_nm"] = round(distance_nm, 3)
                result["ground_speed_kt"] = round(ground_speed_kt, 3)
            results.append(
                {
                    "id": pair.id,
                    "status": "ok",
                    "result": result,
                }
            )

    return {
        "results": results,
        "ok_count": ok_count,
        "error_count": error_count,
    }
