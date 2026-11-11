#!/usr/bin/env bash
# 备份恢复端到端演示：生成 SQLite backup -> 上传校验 -> 冲突拒绝 -> 确认恢复 -> 幂等/重启持久化。
# 用法：
#   docker compose up -d --build        # 先启动服务
#   ./scripts/demo_recovery.sh          # 本机演示（默认 BASE=http://localhost:8000）
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"
WORK="$(mktemp -d -t recovery-demo-XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

pretty() {
  if command -v jq >/dev/null 2>&1; then jq .;
  elif command -v python3 >/dev/null 2>&1; then python3 -m json.tool;
  else cat; fi
}

post() { # $1=path  $2=json body
  curl -sS -X POST "${BASE}$1" -H 'Content-Type: application/json' -d "$2"
}

json_get() { # $1=json 文件 $2=jq 风格的 python 表达式
  python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print($2)" "$1"
}

echo "==================================================================="
echo "0. 健康检查  GET /health"
echo "==================================================================="
curl -sS "${BASE}/health" | pretty
echo

echo "==================================================================="
echo "1. 现库先产生一条补录（恢复前的在线数据）"
echo "==================================================================="
post /api/revisions '{
  "note": "恢复前的在线补录",
  "entries": [
    {"employee_id": "E900", "cost_center": "CC-LIVE",
     "effective_from": "2026-01-01", "effective_to": "2026-02-01"}
  ]
}' | pretty
echo

echo "==================================================================="
echo "2. 用 SQLite Online Backup API 生成合法备份与非法备份（孤儿外键）"
echo "   工具：scripts/make_backup.py"
echo "==================================================================="
python3 scripts/make_backup.py --sample "$WORK/backup.db"
python3 scripts/make_backup.py --sample "$WORK/bad.db" --tamper orphan-fk
echo

echo "==================================================================="
echo "3. 上传非法备份 -> 400 BACKUP_FOREIGN_KEY_VIOLATION，现库不变"
echo "==================================================================="
curl -sS -X POST "${BASE}/api/recovery/backups" \
  -F "file=@$WORK/bad.db" -F "note=非法备份演示" | pretty
echo

echo "==================================================================="
echo "4. 上传合法备份 -> 201，返回修订范围、记录数与一次性恢复凭据"
echo "==================================================================="
curl -sS -X POST "${BASE}/api/recovery/backups" \
  -F "file=@$WORK/backup.db" -F "note=离线恢复批次 B1" > "$WORK/stage.json"
cat "$WORK/stage.json" | pretty
BID=$(json_get "$WORK/stage.json" 'd["backup_id"]')
TOKEN=$(json_get "$WORK/stage.json" 'd["recovery_token"]')
echo

echo "==================================================================="
echo "5. 准备之后又来一条补录；再确认 -> 409 RECOVERY_STALE，现库保持"
echo "==================================================================="
post /api/revisions '{
  "note": "准备后的迟到补录",
  "entries": [
    {"employee_id": "E901", "cost_center": "CC-X",
     "effective_from": "2026-03-01", "effective_to": "2026-04-01"}
  ]
}' > /dev/null
post "/api/recovery/backups/$BID/confirm" "{\"recovery_token\":\"$TOKEN\"}" | pretty
echo

echo "==================================================================="
echo "6. 重新上传同一备份（现库指纹已更新，得到新的暂存与凭据）"
echo "==================================================================="
curl -sS -X POST "${BASE}/api/recovery/backups" -F "file=@$WORK/backup.db" \
  > "$WORK/stage2.json"
BID=$(json_get "$WORK/stage2.json" 'd["backup_id"]')
TOKEN=$(json_get "$WORK/stage2.json" 'd["recovery_token"]')
cat "$WORK/stage2.json" | pretty
echo

echo "==================================================================="
echo "7. 错误凭据确认 -> 403；正确凭据确认 -> 200，整体替换归属数据"
echo "==================================================================="
curl -sS -o /dev/null -w '错误凭据 HTTP %{http_code}\n' \
  -X POST "${BASE}/api/recovery/backups/$BID/confirm" \
  -H 'Content-Type: application/json' -d '{"recovery_token":"a-wrong-but-long-enough-token"}'
post "/api/recovery/backups/$BID/confirm" "{\"recovery_token\":\"$TOKEN\"}" | pretty
echo

echo "==================================================================="
echo "8. 验证替换结果：在线补录 E900 已消失；备份数据生效"
echo "==================================================================="
curl -sS "${BASE}/api/revisions" | pretty
post /api/attribution/query '{
  "revision_id": 2,
  "records": [
    {"employee_id": "E001", "work_date": "2026-05-15", "hours": 8},
    {"employee_id": "E900", "work_date": "2026-01-15", "hours": 8}
  ]
}' | pretty
echo

echo "==================================================================="
echo "9. 后续修订号从备份最大号继续递增（下一个应为 3）"
echo "==================================================================="
post /api/revisions '{
  "note": "恢复后的新补录",
  "entries": [
    {"employee_id": "E003", "cost_center": "CC-SZ",
     "effective_from": "2026-08-01", "effective_to": "2026-09-01"}
  ]
}' | pretty
echo

echo "==================================================================="
echo "10. 重复确认同一备份 -> 幂等返回原结果，不会再次恢复"
echo "==================================================================="
post "/api/recovery/backups/$BID/confirm" "{\"recovery_token\":\"$TOKEN\"}" \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print("status =",d["status"],"| confirmed_at =",d["confirmed_at"],"| next =",d["recovery"]["next_revision_id"])'
echo

echo "==================================================================="
echo "11. 结果查询（重启后仍可用）：GET /api/recovery/backups"
echo "==================================================================="
curl -sS "${BASE}/api/recovery/backups" | pretty
echo
echo "演示完成。交互式文档：${BASE}/docs"
