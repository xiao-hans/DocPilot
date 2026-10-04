#!/usr/bin/env bash
# 阶段 G1 本机备份恢复演练（docs/17 §7、TC-REC-012）。
# pg_dump 源库 → 恢复到隔离库 → 核对结构/行数/关键指纹 → 删除隔离库。
# 只证明"备份脚本与恢复核对可在本机重复运行"，不代表异机灾备或 RPO/RTO 达标。
#
# 范围只到数据库。对象卷（原文件的存放处）不在这里，跑完这个脚本不等于"备份演练通过"——
# 结尾会把这条边界打印出来，避免"一个演练绿了"被读成"数据都有已验证的备份"。
#
# 用法（在 WSL 内，docker CLI 只存在于 Ubuntu 里）：
#   cd /mnt/d/workspace/DocPilot && bash scripts/backup_restore_drill.sh [源库名]
# 想在 Windows 侧一次跑完库 + 对象卷两项：bash scripts/run_backup_drills.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo "缺少 .env（含 POSTGRES_USER/POSTGRES_PASSWORD），参考 .env.example" >&2
  exit 1
fi
env_value() { grep -E "^$1=" .env | head -1 | cut -d= -f2- | tr -d '\r'; }

export PGUSER="$(env_value POSTGRES_USER)"
export PGPASSWORD="$(env_value POSTGRES_PASSWORD)"
SOURCE_DB="${1:-$(env_value POSTGRES_DB)}"
# 隔离库名带运行标识，默认每次运行唯一，不会落在上一次或别人的数据库上。
DRILL_DB="${DRILL_DB:-docpilot_drill_$(date -u +%Y%m%dT%H%M%S)_$$}"
[ -n "$PGUSER" ] && [ -n "$SOURCE_DB" ] || { echo "POSTGRES_USER 或源库未配置" >&2; exit 1; }
[ "$SOURCE_DB" != "$DRILL_DB" ] || { echo "源库与演练库不能同名" >&2; exit 1; }
# 名称白名单：既限定"脚本只会删除自己创建的库"，也让库名可以安全地拼进 SQL。
if [[ ! "$DRILL_DB" =~ ^docpilot_drill_[A-Za-z0-9_.-]+$ ]]; then
  echo "隔离库名必须匹配 ^docpilot_drill_[A-Za-z0-9_.-]+\$，当前为 $DRILL_DB" >&2
  exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
psql_db() { docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U "$PGUSER" -d "$1" "${@:2}"; }

echo "drill: 备份 $SOURCE_DB"
docker compose exec -T postgres pg_dump -U "$PGUSER" --format=custom "$SOURCE_DB" > "$WORK/backup.dump"
echo "       $(wc -c < "$WORK/backup.dump") 字节"

# 查询失败必须中止：把"查不到"当成"库不存在"会让守卫静默放行，比没有守卫更危险。
# 库名已由上面的正则白名单约束，这里按字面量拼进 SQL 是安全的。
db_exists() {
  local result
  if ! result="$(docker compose exec -T postgres psql -U "$PGUSER" -d postgres -At \
      -v ON_ERROR_STOP=1 -c "SELECT 1 FROM pg_database WHERE datname = '$1'")"; then
    echo "无法查询数据库 $1 是否存在，中止演练" >&2
    exit 1
  fi
  printf '%s' "$result"
}

echo "drill: 恢复到隔离库 $DRILL_DB"
# 先取值再判断：写在 [ -n "$(db_exists ...)" ] 里时，守卫自身的失败会被 if 条件吞掉，
# 变成"查不到就等于不存在"的静默放行。
DRILL_EXISTS="$(db_exists "$DRILL_DB")"
if [ -n "$DRILL_EXISTS" ]; then
  echo "隔离库 $DRILL_DB 已存在，拒绝继续：演练只删除本次自己创建的库，不清理既有数据库" >&2
  exit 1
fi
# 清理 trap 先于建库安装，但用 CREATED_DRILL 限定"只删自己建的库"：
# pg_restore 中途失败也要回收，绝不能删除本次运行没有创建的数据库。
CREATED_DRILL=0
cleanup() {
  if [ "$CREATED_DRILL" = "1" ]; then
    docker compose exec -T postgres dropdb -U "$PGUSER" --if-exists "$DRILL_DB" || true
    CREATED_DRILL=0
  fi
}
trap 'cleanup; rm -rf "$WORK"' EXIT
docker compose exec -T postgres createdb -U "$PGUSER" "$DRILL_DB"
CREATED_DRILL=1
docker compose exec -T postgres pg_restore -U "$PGUSER" -d "$DRILL_DB" --no-owner < "$WORK/backup.dump"

rowcounts() {
  psql_db "$1" -At -F'|' <<'SQL'
WITH q AS (
  SELECT string_agg(format('SELECT %L AS t, count(*) AS n FROM public.%I', c.relname, c.relname), ' UNION ALL ') AS s
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE n.nspname = 'public' AND c.relkind = 'r'
)
SELECT COALESCE(s || ' ORDER BY 1', 'NO TABLES') FROM q \gexec
SQL
}

# pg_dump 输出含随机 \restrict token 与注释差异，无法逐字节比较，因此用系统目录构造结构签名。
schema_signature() {
  psql_db "$1" -At -F'|' <<'SQL'
SELECT 'column', table_name, ordinal_position::int, column_name, data_type, is_nullable, -1
  FROM information_schema.columns WHERE table_schema = 'public'
UNION ALL
SELECT 'constraint', cls.relname::text, 0, con.conname, pg_get_constraintdef(con.oid), 'n', -1
  FROM pg_constraint con JOIN pg_class cls ON cls.oid = con.conrelid
  WHERE con.connamespace = 'public'::regnamespace
UNION ALL
SELECT 'index', tablename, 0, indexname, indexdef, 'n', -1
  FROM pg_indexes WHERE schemaname = 'public'
ORDER BY 1, 2, 3, 4;
SQL
}

fingerprints() {
  psql_db "$1" -At -F'|' -c "
    SELECT 'dv', count(*), md5(string_agg(storage_key || ':' || checksum || ':' || media_type || ':' || size_bytes, ',' ORDER BY id))
      FROM document_versions
    UNION ALL SELECT 'job', count(*), md5(string_agg(status || ':' || attempt_count, ',' ORDER BY id))
      FROM processing_jobs
    UNION ALL SELECT 'se', count(*), md5(string_agg(kind || '.' || field_name || ':' || md5(content), ',' ORDER BY id))
      FROM search_entries"
}

echo "drill: 核对结构、行数与关键指纹"
diff <(schema_signature "$SOURCE_DB") <(schema_signature "$DRILL_DB") > "$WORK/schema.diff" \
  || { echo "结构不一致：" && head -20 "$WORK/schema.diff"; exit 1; }
diff <(rowcounts "$SOURCE_DB") <(rowcounts "$DRILL_DB") > "$WORK/counts.diff" \
  || { echo "行数不一致：" && head -20 "$WORK/counts.diff"; exit 1; }
diff <(fingerprints "$SOURCE_DB") <(fingerprints "$DRILL_DB") > "$WORK/fp.diff" \
  || { echo "存储键/校验和/任务状态/索引指纹不一致：" && head -20 "$WORK/fp.diff"; exit 1; }

echo "drill: 核对通过"
fingerprints "$SOURCE_DB"
# 边界必须随结果一起打印：只跑这个脚本时，"演练通过"覆盖的是数据库，不是对象数据。
# 这条曾经真的被误读过（R-018 的登记理由就是"数据库演练不覆盖对象存储"）。
echo "drill: DRILL RESULT: DB PASS（范围=PostgreSQL 备份与恢复）"
echo "drill: 未覆盖对象卷。要覆盖对象数据，另跑 scripts/object_store_backup_drill.py；"
echo "drill: 一次跑完两项请用 bash scripts/run_backup_drills.sh。"
cleanup
trap - EXIT

