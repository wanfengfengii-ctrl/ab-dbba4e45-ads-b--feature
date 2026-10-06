#!/bin/sh
# verify 一次性服务入口：代码测试 -> 等待 API 健康 -> 接口冒烟。
# 任一步失败立即以非零退出码退出。
set -eu

BASE_URL="${API_BASE_URL:-http://api:8000}"

echo "== [1/3] 运行 pytest 代码测试 =="
python -m pytest -q

echo "== [2/3] 等待 API 健康检查通过：$BASE_URL/health =="
python scripts/wait_health.py "$BASE_URL"

echo "== [3/3] 接口冒烟（有效报文 + 坏 CRC） =="
python scripts/smoke.py "$BASE_URL"

echo "== verify 全部通过，退出码 0 =="
