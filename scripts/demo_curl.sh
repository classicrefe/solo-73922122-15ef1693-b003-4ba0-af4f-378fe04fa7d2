#!/usr/bin/env bash
# 演示脚本：设置待结月 -> 导入基础批 -> 封账 -> 补报/更正 -> 封账
#           -> 重报（副本只留变化项）-> 改回（释放副本）-> 封账 -> 零抵消下期扣减。
# 用法：bash scripts/demo_curl.sh [base_url]   （默认 http://127.0.0.1:8000）
set -euo pipefail

BASE="${1:-http://127.0.0.1:8000}"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
JAR="$(mktemp)"
trap 'rm -f "$JAR"' EXIT

step() { printf '\n== %s\n' "$1"; }
get()  { curl -s -c "$JAR" -b "$JAR" "$BASE$1"; }
post() { curl -s -c "$JAR" -b "$JAR" -L "$BASE$1" "${@:2}"; }
import_csv() {
  curl -s -c "$JAR" -b "$JAR" -F "file=@$DIR/demo/$1" "$BASE/import" \
    | grep -o '批次 #[0-9]* 导入成功[^<]*' || true
}
snap_count() {
  get /close | grep -o '逐笔副本：<strong>[0-9]*</strong>' | grep -o '[0-9]*' || true
}

curl -sf -o /dev/null "$BASE/" || { echo "服务不可达：$BASE"; exit 1; }

step "1) 设置首个待结月 2026-01"
post /setup -d "pending_month=2026-01" | grep -o '首个待结月已设为 [0-9-]*' || echo "（可能已设置过，继续）"

step "2) 导入基础批 demo/claims_2026-01_base.csv"
import_csv claims_2026-01_base.csv

step "3) 封账 2026-01（封账后释放本期副本，副本数应为 0）"
post /close -d "close_month=2026-01" | grep -o '已封账 2026-01[^<]*' || true
echo "  额外留存副本：$(snap_count) 份"

step "4) 导入补报/更正批 demo/claims_2026-02_supplement.csv"
import_csv claims_2026-02_supplement.csv

step "5) 封账 2026-02（C-0002 改金额、C-0004 改支付月各留 1 份副本）"
post /close -d "close_month=2026-02" | grep -o '已封账 2026-02[^<]*' || true
echo "  额外留存副本：$(snap_count) 份"

step "6) 导入重报批 demo/claims_2026-03_revise.csv（5 笔与上次封账不同）"
import_csv claims_2026-03_revise.csv
echo "  额外留存副本：$(snap_count) 份（预期 5：C-0002/C-0003/C-0004/C-0005/C-0008）"
get /close | grep -o '<td>[0-9-]*</td><td>[0-9-]*</td><td class="num">[+-][0-9,]*</td>'
echo "  封账页预览：http://localhost:8000/close"

step "7) 导入改回批 demo/claims_2026-03_revert.csv（3 笔改回上次封账值）"
import_csv claims_2026-03_revert.csv
echo "  额外留存副本：$(snap_count) 份（预期 2：C-0004/C-0008 为零抵消更正）"

step "8) 封账 2026-03（零抵消，归档 0 个组合，副本释放）"
post /close -d "close_month=2026-03" | grep -o '已封账 2026-03[^<]*' || true
echo "  额外留存副本：$(snap_count) 份"

step "9) 导入 demo/claims_2026-04_after_zero.csv：下期只改 C-0004"
import_csv claims_2026-04_after_zero.csv
echo "  额外留存副本：$(snap_count) 份（预期 1）"

step "10) 封账 2026-04：按本次封账逐笔金额扣减（55000 -> 52000，-3,000）"
post /close -d "close_month=2026-04" | grep -o '已封账 2026-04[^<]*' || true
echo "  额外留存副本：$(snap_count) 份"

step "11) 三角回查与归档明细（浏览器打开）"
echo "  四期：$BASE/triangle?start=2025-10&close=2026-04"
echo "  首期：$BASE/triangle?start=2025-10&close=2026-01"
echo "  归档明细：$BASE/archive/2026-02 与 $BASE/archive/2026-04"
