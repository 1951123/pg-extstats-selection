#!/usr/bin/env bash
###############################################################################
# init_stats_ceb.sh — 幂等地在 PostgreSQL 中创建 stats_CEB (Cardinality
#                     Estimation Benchmark on StackExchange data) 数据库
#
# stats_CEB 数据: StackExchange 的 posts/users/votes/badges/comments/
#                  postHistory/postLinks/tags, 用于基数估计(join order)测试。
#
# 与 JOB (init_imdb.sh) 的差异:
#   - CSV 带 header 行, COPY 需指定 HEADER true (JOB 的 CSV 无 header)
#   - CSV 无内嵌引号/反斜杠, 无需 escape '\' 处理, 用标准 CSV COPY 即可
#
# 功能:
#   1. 创建数据库 (默认 stats), 已存在则复用
#   2. 应用 schema (benchmarks/stats_CEB/data/stats.sql)
#   3. 导入 8 张表的 CSV 数据
#   4. (可选) 建索引 (默认关闭, 遵循基准"无二级索引"惯例, 保证 join order 测试公平)
#   5. 运行 ANALYZE 收集统计信息
#
# 幂等性:
#   - 默认: 若所有表均已有数据则跳过导入; 部分缺失只补缺失。
#   - FORCE=1: DROP DATABASE 并完整重建。
#
# 用法:
#   ./benchmarks/init_stats_ceb.sh [--force] [--db NAME] ...
#   环境变量: PGHOST / PGPORT / PGUSER / PGPASSWORD / DB_NAME
#
# 例子:
#   PGPASSWORD=postgres ./benchmarks/init_stats_ceb.sh
#   PGPASSWORD=postgres FORCE=1 ./benchmarks/init_stats_ceb.sh
###############################################################################

set -euo pipefail
export PGAPPNAME="init_stats_ceb"

# ---------------------------------------------------------------------------
# 默认配置 (可被环境变量 / 命令行参数覆盖)
# ---------------------------------------------------------------------------
DB_NAME="${DB_NAME:-stats}"
PGHOST="${PGHOST:-localhost}"
PGPORT="${PGPORT:-5432}"
PGUSER="${PGUSER:-postgres}"
PGPASSWORD="${PGPASSWORD:-}"

BENCH_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CEB_DIR="${BENCH_ROOT}/benchmarks/stats_CEB"
SCHEMA_SQL="${CEB_DIR}/data/stats.sql"
DATA_DIR="${CEB_DIR}/data"

# 是否创建二级索引 (0=不建, 1=建)。默认不建以符合基准"无二级索引"惯例。
BUILD_INDEX="${BUILD_INDEX:-0}"

# 8 张表的导入顺序 (无依赖, 顺序任意)
readonly -a ALL_TABLES=(
  users posts postLinks postHistory comments votes badges tags
)

FORCE=0

# ---------------------------------------------------------------------------
# 解析命令行参数
# ---------------------------------------------------------------------------
usage() {
  cat <<EOF
用法: $0 [--force] [--build-index] [--db NAME] [--pguser USER] \
[--pghost HOST] [--pgport PORT]

选项:
  --force        强制重建: DROP DATABASE 并完整重新导入全部数据
  --build-index  创建二级索引(供更快跑查询; 默认不建以符合基准惯例)
  --db NAME      数据库名 (默认: stats)
  --pguser USER  PostgreSQL 用户名 (默认: postgres)
  --pghost HOST  PostgreSQL 主机 (默认: localhost)
  --pgport PORT  PostgreSQL 端口 (默认: 5432)
  -h, --help     显示本帮助

环境变量: PGHOST, PGPORT, PGUSER, PGPASSWORD, DB_NAME, BUILD_INDEX
示例:
  PGPASSWORD=postgres ./benchmarks/init_stats_ceb.sh
  PGPASSWORD=postgres FORCE=1 ./benchmarks/init_stats_ceb.sh
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --force) FORCE=1; shift ;;
    --build-index) BUILD_INDEX=1; shift ;;
    --db) DB_NAME="${2:-}"; shift 2 ;;
    --pguser) PGUSER="${2:-}"; shift 2 ;;
    --pghost) PGHOST="${2:-}"; shift 2 ;;
    --pgport) PGPORT="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage >&2; exit 1 ;;
  esac
done

export PGHOST PGPORT PGUSER PGPASSWORD

psql_main() { psql -X -v ON_ERROR_STOP=1 -P pager=off "$@"; }
psql_db()   { psql -X -v ON_ERROR_STOP=1 -P pager=off -d "${DB_NAME}" "$@"; }

# ---------------------------------------------------------------------------
# 前置检查
# ---------------------------------------------------------------------------
if ! command -v psql >/dev/null 2>&1; then
  echo "错误: 未找到 psql, 请先安装 PostgreSQL 客户端。" >&2
  exit 1
fi
if [[ ! -f "${SCHEMA_SQL}" ]]; then
  echo "错误: 找不到 schema 文件: ${SCHEMA_SQL}" >&2
  exit 1
fi

echo "==> PostgreSQL 连接: ${PGUSER}@${PGHOST}:${PGPORT}"
echo "==> 目标数据库: ${DB_NAME}   FORCE=${FORCE}   BUILD_INDEX=${BUILD_INDEX}"
echo "==> Schema: ${SCHEMA_SQL}"

if ! psql_main -d postgres -tAc "SELECT 1" >/dev/null 2>&1; then
  echo "错误: 无法连接到 PostgreSQL 服务器 (${PGUSER}@${PGHOST}:${PGPORT})." >&2
  exit 1
fi
echo "==> 服务器连接成功"

# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------
db_exists() { psql_main -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1; }

# 返回某表当前行数 (表不存在或为空则返回空)
table_rows() {
  psql_db -tAc "SELECT (SELECT count(*) FROM ${1})::text
                WHERE EXISTS (
                  SELECT 1 FROM pg_class c JOIN pg_namespace n
                  ON n.oid=c.relnamespace
                  WHERE n.nspname='public' AND lower(c.relname)=lower('${1}') AND c.relkind='r')" \
    | tr -d '[:space:]'
}

is_fully_loaded() {
  local t n
  for t in "${ALL_TABLES[@]}"; do
    n="$(table_rows "$t")"
    [[ -z "$n" || "$n" == "0" ]] && return 1
  done
  return 0
}

# ---------------------------------------------------------------------------
# Step 0: 数据库存在性
# ---------------------------------------------------------------------------
if [[ "$FORCE" == "1" ]]; then
  echo "==> [FORCE] 删除并重建数据库 '${DB_NAME}' ..."
  psql_main -d postgres -c "DROP DATABASE IF EXISTS \"${DB_NAME}\" WITH (FORCE);"
  createdb -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" "${DB_NAME}"
elif ! db_exists; then
  echo "==> 数据库 '${DB_NAME}' 不存在, 创建中 ..."
  createdb -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" "${DB_NAME}"
else
  echo "==> 数据库 '${DB_NAME}' 已存在, 复用。"
fi

# ---------------------------------------------------------------------------
# Step 1: 应用 schema (幂等)
#   将 "CREATE TABLE" 替换为 "CREATE TABLE IF NOT EXISTS"
# ---------------------------------------------------------------------------
echo "==> 应用 schema: ${SCHEMA_SQL}"
TMP_SCHEMA="$(mktemp)"
sed -E 's/^CREATE TABLE /CREATE TABLE IF NOT EXISTS /I' "${SCHEMA_SQL}" > "${TMP_SCHEMA}"
psql_db -q -f "${TMP_SCHEMA}" || { echo "错误: schema 应用失败。" >&2; rm -f "${TMP_SCHEMA}"; exit 1; }
rm -f "${TMP_SCHEMA}"

# ---------------------------------------------------------------------------
# Step 2: 导入 CSV 数据
#   CSV 带 header, 用 HEADER true; 空字段为 NULL (NULL '').
#   无内嵌引号/反斜杠, 直接用标准 FORMAT csv.
# ---------------------------------------------------------------------------
missing=()
for t in "${ALL_TABLES[@]}"; do
  n="$(table_rows "$t")"
  [[ -z "$n" || "$n" == "0" ]] && missing+=("$t")
done

if [[ "${#missing[@]}" -eq 0 ]]; then
  echo "==> 所有表均已导入数据, 跳过数据导入。"
else
  echo "==> 待导入的表 (${#missing[@]} 个): ${missing[*]}"
  for t in "${missing[@]}"; do
    csv="${DATA_DIR}/${t}.csv"
    if [[ ! -f "$csv" ]]; then
      echo "    警告: 找不到数据文件 ${csv}, 跳过表 ${t}" >&2
      continue
    fi
    echo "    ── 导入表 '${t}' 自 ${t}.csv"
    # CSV 带 header 行, 自动跳过; 空字段 -> NULL
    psql_db -q <<SQL
\copy ${t} from '${csv}' WITH (FORMAT csv, HEADER true, NULL '');
SQL
  done
fi

# ---------------------------------------------------------------------------
# Step 3: (可选) 创建二级索引
#   默认关闭。启用后为 join 列建索引以加速查询;
#   注意: 这会影响 join order 基准的公平性, 谨慎使用。
# ---------------------------------------------------------------------------
if [[ "${BUILD_INDEX}" == "1" ]]; then
  echo "==> 创建二级索引 ..."
  psql_db -q <<'SQL'
CREATE INDEX IF NOT EXISTS idx_comments_userid   ON comments  (UserId);
CREATE INDEX IF NOT EXISTS idx_comments_postid   ON comments  (PostId);
CREATE INDEX IF NOT EXISTS idx_posts_owneruserid ON posts     (OwnerUserId);
CREATE INDEX IF NOT EXISTS idx_posthistory_userid ON postHistory (UserId);
CREATE INDEX IF NOT EXISTS idx_posthistory_postid ON postHistory (PostId);
CREATE INDEX IF NOT EXISTS idx_postlinks_postid   ON postLinks (PostId);
CREATE INDEX IF NOT EXISTS idx_postlinks_relpostid ON postLinks (RelatedPostId);
CREATE INDEX IF NOT EXISTS idx_votes_userid      ON votes     (UserId);
CREATE INDEX IF NOT EXISTS idx_votes_postid      ON votes     (PostId);
CREATE INDEX IF NOT EXISTS idx_badges_userid     ON badges    (UserId);
CREATE INDEX IF NOT EXISTS idx_tags_excerptpostid ON tags      (ExcerptPostId);
SQL
fi

# ---------------------------------------------------------------------------
# Step 4: ANALYZE
# ---------------------------------------------------------------------------
echo "==> 运行 ANALYZE ..."
psql_db -c "ANALYZE;" >/dev/null

echo
echo "===> 完成! 数据库 '${DB_NAME}' 已就绪。"
if is_fully_loaded; then
  echo "     所有 ${#ALL_TABLES[@]} 张表均已导入完整数据。"
else
  echo "     警告: 仍有表未导入数据, 请检查上面的日志。" >&2
fi
echo "     连接: psql -h ${PGHOST} -p ${PGPORT} -U ${PGUSER} -d ${DB_NAME}"
