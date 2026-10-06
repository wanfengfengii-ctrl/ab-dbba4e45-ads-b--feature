# ADS-B 成对空中位置报文裁决服务

雷达融合前，对成对的 ADS-B DF17 空中位置报文做核对与全球 CPR 解算，
防止纬度分区交界与经度回绕把航迹投到错误半球。

- **运行时仅依赖标准库 + FastAPI/uvicorn**，CPR 与 CRC 均为自研实现
- 全球 CPR 解码与 [pyModeS](https://github.com/junzis/pyModeS) 在
  数百个随机点与真实报文上逐位交叉验证
- 逐组独立裁决：同批中的坏组不会遮蔽合格组
- 经度严格归一化到 `[-180, 180)`，跨日期变更线与纬度分区边界结果稳定

## 接口

### `GET /health`

返回 `200 {"status":"ok"}`。容器与 Compose 均以此做健康检查。

### `POST /api/adsb/positions/decode`

接收 1–200 组编号唯一的报文对，逐组返回裁决结果。

请求：

```json
{
  "pairs": [
    {
      "id": "track-001",
      "frame1": {"received_at_ms": 1700000000000, "raw_hex": "8D40621D58C382D690C8AC2863A7"},
      "frame2": {"received_at_ms": 1700000000800, "raw_hex": "8D40621D58C386435CC412692AD6"}
    }
  ]
}
```

- `id`：组内唯一编号，字符串或整数均可，响应顺序与请求一致
- `received_at_ms`：接收时刻（毫秒，非负整数）
- `raw_hex`：28 位十六进制 DF17 原文（大小写、首尾空白均可）
- `motion_guard`（可选）：`{"max_ground_speed_kt": 1..2000 的整数}`。
  省略或为 `null` 时请求、响应与错误语义完全保持不变；启用后按两帧
  **各自代表的位置**计算最短地表位移与平均地速，拦截两帧各自合法却因
  错配形成的失真航迹

响应：

```json
{
  "results": [
    {
      "id": "track-001",
      "status": "ok",
      "result": {
        "icao": "40621D",
        "latitude": 52.26578,
        "longitude": 3.938913,
        "newer_frame": 2,
        "time_delta_ms": 800,
        "distance_nm": 0.142,
        "ground_speed_kt": 639.0
      }
    }
  ],
  "ok_count": 1,
  "error_count": 0
}
```

位置取自**较新一帧**（`received_at_ms` 相同取 frame1），经纬度为
六位小数十进制度，经度保证在 `[-180, 180)`。`distance_nm` 与
`ground_speed_kt` 仅在启用 `motion_guard` 且该组通过时出现，保留
**三位小数**；注意阈值裁决使用未舍入值，与阈值相等视为通过
（例如真实地速 82.00023 节在阈值 82 下仍判超限，尽管展示为 82.0）。

失败组结构：

```json
{
  "id": "track-002",
  "status": "error",
  "error": {"code": "BAD_CRC", "message": "CRC 校验失败，报文已损坏", "frame": 2}
}
```

### 稳定错误码

| 错误码 | 含义 | `frame` |
|---|---|---|
| `BAD_HEX` | 非 28 位十六进制原文 | 1/2 |
| `BAD_CRC` | Mode-S CRC-24 校验失败 | 1/2 |
| `NOT_DF17` | 不是 DF17 报文 | 1/2 |
| `NOT_AIRBORNE_POSITION` | DF17 但 TC 不是 9–18（非空中位置 BDS 0,5） | 1/2 |
| `ICAO_MISMATCH` | 两帧 ICAO 24 位地址不一致 | `null` |
| `PARITY_NOT_OPPOSITE` | 两帧 CPR 奇偶标志相同 | `null` |
| `TIME_GAP_EXCEEDED` | 接收时间相隔超过 10000 ms | `null` |
| `LATITUDE_ZONE_MISMATCH` | 两帧落入不同纬度带（NL 不一致） | `null` |
| `INVALID_POSITION` | 解算结果超出经纬度范围 | `null` |
| `MOTION_TIME_UNRESOLVED` | 启用运动守卫但两帧接收时刻相同，无法计算地速 | `null` |
| `MOTION_LIMIT_EXCEEDED` | 两帧间平均地速（未舍入值）超过 `max_ground_speed_kt` | `null` |
| `INTERNAL_ERROR` | 未预期服务端异常（不影响同批其他组） | `null` |

两类运动错误只影响所在组：同批其他组照常裁决，批次顺序与
`ok_count`/`error_count` 计数保持不变。运动守卫在 CPR 解算成功之后才
执行，因此上游错误（如 `BAD_CRC`）优先返回、语义不变。

请求体本身的结构错误（组数越界、编号重复、字段缺失、
`max_ground_speed_kt` 不在 1..2000 等）返回 `422`，不进入逐组裁决。

## 本地运行（无 Docker）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

## Docker

```bash
# 宿主机端口可配置，默认 8080
HOST_PORT=9090 docker compose up -d api
curl http://localhost:9090/health
```

### 一次性校验服务 verify

```bash
# CI/流水线用法：构建镜像，verify 结束后连同 api 一起退出，并透传 verify 退出码
docker compose up --build --abort-on-container-exit --exit-code-from verify verify
echo "verify exit code: $?"
```

`verify` 是一次性服务，串行执行：

1. `pytest` 全部代码测试（107 项，含与 pyModeS 的独立交叉验证）
2. 等待 `api` 服务健康检查通过
3. 对运行中的 API 做接口冒烟，覆盖：
   - **兼容回归**：省略 `motion_guard` 的有效报文 + 坏 CRC 报文，
     验证解算、错误码、帧编号、坏组不遮蔽，且响应不含运动字段
   - **正常运动**：启用守卫的合法位移，返回三位小数位移与地速
   - **超限**：不可能跃迁返回 `MOTION_LIMIT_EXCEEDED`，只影响所在组
   - **跨日期变更线**：±180° 两侧相邻位置按短弧得到合理速度并放行

全部通过则容器以 **退出码 0** 退出，任一步失败以非零码退出；
镜像构建失败时同样不会进入校验且整条命令失败。可用
`docker inspect ... --format '{{.State.ExitCode}}'` 查看退出码。

## 项目结构

```
app/
  modes.py    # CRC-24、DF17 解析、NL 表、全球 CPR 解码、运动守卫、逐组裁决
  schemas.py  # 请求/响应模型与入参校验
  main.py     # FastAPI 路由
tests/        # pytest 单元/接口测试 + 报文构造器
scripts/      # verify.sh、健康等待、接口冒烟
Dockerfile    # runtime / test 多阶段
docker-compose.yml
```

## 实现要点

- **CRC-24**：Annex 10 生成多项式 `0xFFF409`，逐字节长除法，合法 DF17 余数为 0
- **字段提取**：DF=首字节高 5 位；TC=ME 首字节高 5 位；F=奇偶标志；
  CPR 纬/经度各 17 位
- **全球 CPR**（DO-260B A.1.7）：`j=floor(59·yz0−60·yz1+0.5)` 求纬向带索引，
  两帧 NL 必须一致；再按较新帧奇偶性以 `ni=NL`（偶）或 `NL−1`（奇）
  求经度索引，最后折回 `[-180,180)`
- **回绕安全**：所有经度运算按循环经度处理，`+179.9999` 与 `-179.9999`
  不会互相镜像
- **运动守卫**：全球解算同时给出两帧各自位置，位移取大圆短弧
  haversine（经度差先归一到 `[-180,180)`，跨日期变更线不会绕行地球），
  地速 = 位移（海里）× 3.6e6 / 时间差（毫秒）；阈值比较只用未舍入值，
  展示层才保留三位小数
