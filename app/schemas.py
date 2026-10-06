"""POST /api/adsb/positions/decode 的请求与响应模型。"""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, field_validator, model_validator

MAX_PAIRS = 200

PairId = Annotated[Union[str, int], Field(union_mode="left_to_right")]


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


class PairResult(BaseModel):
    id: PairId
    status: Literal["ok", "error"]
    result: PositionResult | None = None
    error: ErrorDetail | None = None


class DecodeResponse(BaseModel):
    results: list[PairResult]
    ok_count: int
    error_count: int
