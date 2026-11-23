#!/usr/bin/env bash
# 演示脚本：设置待结月 -> 导入基础批 -> 封账 -> 导入补报/更正批 -> 封账 -> 两期回查。
# 用法：bash scripts/demo_curl.sh [base_url]   （默认 http://127.0.0.1:8000）
set -euo pipefail

BASE="${1:-http://127.0.0.1:8000}"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
JAR="$(mktemp)"
trap 'rm -f "$JAR"' EXIT

step() { printf '\n== %s\n' "$1"; }

curl -sf -o /dev/null "$BASE/" || { echo "服务不可达：$BASE"; exit 1; }

step "1) 设置首个待结月 2026-01"
curl -s -c "$JAR" -b "$JAR" -d "pending_month=2026-01" -L "$BASE/setup" \
  | grep -o '首个待结月已设为 [0-9-]*' || echo "（可能已设置过，继续）"

step "2) 导入基础批 demo/claims_2026-01_base.csv"
curl -s -c "$JAR" -b "$JAR" -F "file=@$DIR/demo/claims_2026-01_base.csv" "$BASE/import" \
  | grep -o '批次 #[0-9]* 导入成功[^<]*'

step "3) 封账 2026-01"
curl -s -c "$JAR" -b "$JAR" -d "close_month=2026-01" -L "$BASE/close" \
  | grep -o '已封账 2026-01[^<]*'

step "4) 导入补报/更正批 demo/claims_2026-02_supplement.csv"
curl -s -c "$JAR" -b "$JAR" -F "file=@$DIR/demo/claims_2026-02_supplement.csv" "$BASE/import" \
  | grep -o '批次 #[0-9]* 导入成功[^<]*'

step "5) 封账 2026-02"
curl -s -c "$JAR" -b "$JAR" -d "close_month=2026-02" -L "$BASE/close" \
  | grep -o '已封账 2026-02[^<]*'

step "6) 两期回查（浏览器打开对比）"
echo "  一期：$BASE/triangle?start=2025-10&close=2026-01"
echo "  二期：$BASE/triangle?start=2025-10&close=2026-02"
echo "  归档明细：$BASE/archive/2026-01 与 $BASE/archive/2026-02"
