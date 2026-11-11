#!/usr/bin/env bash
# 端到端演示脚本：调岗补录 + 月结口径冻结 + 整批拒绝。
# 用法：
#   docker compose up -d --build       # 先启动服务
#   ./scripts/demo.sh                  # 本机演示（默认 BASE=http://localhost:8000）
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"

pretty() {
  if command -v jq >/dev/null 2>&1; then jq .;
  elif command -v python3 >/dev/null 2>&1; then python3 -m json.tool;
  else cat; fi
}

post() { # $1=path  $2=json body
  curl -sS -X POST "${BASE}$1" -H 'Content-Type: application/json' -d "$2"
}

echo "==================================================================="
echo "0. 健康检查  GET /health"
echo "==================================================================="
curl -sS "${BASE}/health" | pretty
echo

echo "==================================================================="
echo "1. 修订 1：人事初始归属（Q1）"
echo "   E001 -> CC-SH-01 [2026-01-01, 2026-04-01)"
echo "   E002 -> CC-BJ-01 [2026-01-01, 2026-04-01)"
echo "==================================================================="
post /api/revisions '{
  "note": "HR 初始归属 Q1",
  "entries": [
    {"employee_id": "E001", "cost_center": "CC-SH-01",
     "effective_from": "2026-01-01", "effective_to": "2026-04-01"},
    {"employee_id": "E002", "cost_center": "CC-BJ-01",
     "effective_from": "2026-01-01", "effective_to": "2026-04-01"}
  ]
}' | pretty
echo

echo "==================================================================="
echo "2. 按修订 1 口径查询工时（含左闭右开边界、空档、未归属员工）"
echo "   2026-01-01 命中(左端点含); 2026-04-01 不命中(右端点不含/空档);"
echo "   E999 无任何归属"
echo "==================================================================="
post /api/attribution/query '{
  "revision_id": 1,
  "records": [
    {"employee_id": "E001", "work_date": "2026-01-01", "hours": 8},
    {"employee_id": "E001", "work_date": "2026-03-31", "hours": 6},
    {"employee_id": "E001", "work_date": "2026-04-01", "hours": 8},
    {"employee_id": "E002", "work_date": "2026-02-10", "hours": 7.5},
    {"employee_id": "E999", "work_date": "2026-02-10", "hours": 4}
  ]
}' | pretty
echo

echo "==================================================================="
echo "3. 非法批次 A：起日不早于止日 -> 整批拒绝 (400)"
echo "==================================================================="
post /api/revisions '{
  "entries": [
    {"employee_id": "E001", "cost_center": "CC-X",
     "effective_from": "2026-05-01", "effective_to": "2026-05-01"}
  ]
}' | pretty
echo

echo "==================================================================="
echo "4. 非法批次 B：同员工批内区间重叠 -> 整批拒绝 (400)"
echo "==================================================================="
post /api/revisions '{
  "entries": [
    {"employee_id": "E001", "cost_center": "CC-X",
     "effective_from": "2026-06-01", "effective_to": "2026-08-01"},
    {"employee_id": "E001", "cost_center": "CC-Y",
     "effective_from": "2026-07-01", "effective_to": "2026-09-01"}
  ]
}' | pretty
echo

echo "==================================================================="
echo "5. 确认修订号未被失败批次消耗：下一个成功批次仍是修订 2"
echo "==================================================================="
curl -sS "${BASE}/api/revisions" | pretty
echo

echo "==================================================================="
echo "6. 修订 2：调岗补录 E001 4~6 月转到 CC-GZ-02（跨段覆盖，区间外保留）"
echo "   与修订 1 跨批重叠是允许的；批内重叠才拒绝"
echo "==================================================================="
post /api/revisions '{
  "note": "HR 调岗补录：E001 四月起转广州成本中心",
  "entries": [
    {"employee_id": "E001", "cost_center": "CC-GZ-02",
     "effective_from": "2026-04-01", "effective_to": "2026-07-01"}
  ]
}' | pretty
echo

echo "==================================================================="
echo "7. 月结口径冻结验证：同样的请求再查修订 1，结果与补录前完全一致"
echo "==================================================================="
post /api/attribution/query '{
  "revision_id": 1,
  "records": [
    {"employee_id": "E001", "work_date": "2026-05-15", "hours": 8}
  ]
}' | pretty
echo

echo "==================================================================="
echo "8. 按修订 2 口径查询：5 月命中新成本中心；3 月区间外保留旧归属"
echo "==================================================================="
post /api/attribution/query '{
  "revision_id": 2,
  "records": [
    {"employee_id": "E001", "work_date": "2026-05-15", "hours": 8},
    {"employee_id": "E001", "work_date": "2026-03-15", "hours": 8},
    {"employee_id": "E001", "work_date": "2026-07-01", "hours": 8}
  ]
}' | pretty
echo

echo "==================================================================="
echo "9. 审计：列出全部修订号与修订 2 的原始区间"
echo "==================================================================="
curl -sS "${BASE}/api/revisions" | pretty
post /api/revisions/2/segments '{}' | pretty
echo
echo "演示完成。交互式文档：${BASE}/docs"
