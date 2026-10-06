"""POST /api/adsb/positions/decode 的请求与响应模型。"""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, field_validator, model_validator

MAX_PAIRS = 200
MIN_GROUND_SPEED_KT = 1
MAX_GROUND_SPEED_KT = 2000

PairId = Annotated[Union[str, int], Field(union_mode="left_to_right")]


class MotionGuard(BaseModel):
    """运动合理性守卫（可选）：两帧间地速不得超过上限。"""

    max_ground_speed_kt: int = Field(
        ...,
        ge=MIN_GROUND_SPEED_KT,
        le=MAX_GROUND_SPEED_KT,
        description="两帧间最大允许地速（节），阈值相等视为通过",
    )


class FrameIn(BaseModel):
    """一帧 ADS-B 报文：接收时刻（毫秒）与 28 位十六进制原文。"""

    received_at_ms: int = Field(..., ge=0, description="接收时刻，Unix 毫秒")
    raw_hex: str = Field(..., min_length=1, description="28 位十六进制原文")

    @field_validator("raw_hex")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip().upper()


class PairIn(BaseModel):
    """编号唯一的报文对。"""

    id: PairId
    frame1: FrameIn
    frame2: FrameIn


class DecodeRequest(BaseModel):
    pairs: list[PairIn] = Field(..., min_length=1, max_length=MAX_PAIRS)
    motion_guard: MotionGuard | None = Field(
        None, description="可选运动合理性守卫；省略时行为与既有接口一致"
    )

    @model_validator(mode="after")
    def _unique_ids(self) -> "DecodeRequest":
        seen: set[str | int] = set()
        for pair in self.pairs:
            if pair.id in seen:
                raise ValueError(f"报文对编号必须唯一，发现重复编号：{pair.id!r}")
            seen.add(pair.id)
        return self


class ErrorDetail(BaseModel):
    code: str
    message: str
    frame: int | None = Field(
        None, description="出错帧编号 1/2；成对比较类错误为 null"
    )


class PositionResult(BaseModel):
    icao: str = Field(..., description="6 位十六进制 ICAO 地址")
    latitude: float
    longitude: float
    newer_frame: Literal[1, 2] = Field(..., description="位置取自的较新帧")
    time_delta_ms: int
    # 仅在启用 motion_guard 时出现：保留三位小数
    distance_nm: float | None = Field(
        None, description="两帧位置间最短地表距离（海里，三位小数）"
    )
    ground_speed_kt: float | None = Field(
        None, description="两帧间地速（节，三位小数）"
    )


class PairResult(BaseModel):
    id: PairId
    status: Literal["ok", "error"]
    result: PositionResult | None = None
    error: ErrorDetail | None = None


class DecodeResponse(BaseModel):
    results: list[PairResult]
    ok_count: int
    error_count: int
