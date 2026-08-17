#!/usr/bin/env bash
###############################################################################
# init_imdb.sh — 幂等地在 PostgreSQL 中创建 JOB (Join Order Benchmark) / IMDB 数据库
#
# 参考: https://zhuanlan.zhihu.com/p/637260071
#   IMDB 数据 CSV 采用 `\`(反斜杠) 转义字段内的引号, 因此 COPY 需指定
#   `escape as '\'`, 而非 PostgreSQL 默认的 `""` 双引号转义。
#
# 功能:
#   1. 创建数据库 (默认 imdb, 与知乎示例一致), 已存在则复用
#   2. 应用 schema
#   3. 用官方 COPY 参数导入 JOB 数据 (benchmarks/JOB/data/*.csv)
#   4. 创建查询所需索引 (benchmarks/JOB/fkindexes.sql)
#   5. 运行 ANALYZE 收集统计信息
#
# 幂等性:
#   - 默认模式: 若数据库已存在且所有表均已有数据, 直接跳过导入(无事可做)。
#     若部分表未导入, 则只导入缺失的表并补齐后续步骤。
#   - FORCE=1 模式: DROP DATABASE 并完整重建、重新导入全部数据。
#
# 用法:
#   ./benchmarks/init_imdb.sh [--force] [--db NAME] ...
#
# 常用环境变量 (与 psql 一致):
#   PGHOST / PGPORT / PGUSER / PGPASSWORD / DB_NAME
#
# 例子:
#   ./benchmarks/init_imdb.sh                                  # 幂等导入
#   FORCE=1 ./benchmarks/init_imdb.sh                          # 强制重建
#   PGHOST=10.0.0.5 PGPORT=5433 PGPASSWORD=secret ./benchmarks/init_imdb.sh
###############################################################################

set -euo pipefail
export PGAPPNAME="init_imdb"

# ---------------------------------------------------------------------------
# 默认配置 (可被环境变量 / 命令行参数覆盖)
# ---------------------------------------------------------------------------
DB_NAME="${DB_NAME:-imdb}"
PGHOST="${PGHOST:-localhost}"
PGPORT="${PGPORT:-5432}"
PGUSER="${PGUSER:-postgres}"
PGPASSWORD="${PGPASSWORD:-}"

# JOB 数据的绝对路径
BENCH_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
JOB_DIR="${BENCH_ROOT}/benchmarks/JOB"
# 与数据同目录的官方 schema (schematext.sql); 也可改为仓库根下的 schema.sql
SCHEMA_SQL="${JOB_DIR}/data/schematext.sql"
SCHEMA_FALLBACK="${JOB_DIR}/schema.sql"
FK_INDEX_SQL="${JOB_DIR}/fkindexes.sql"
DATA_DIR="${JOB_DIR}/data"

# 表 -> 对应 CSV 文件名 的映射 (顺序与知乎示例一致)
readonly -a TABLE_NAMES=(
  aka_name aka_title cast_info char_name comp_cast_type
  company_name company_type complete_cast info_type keyword
  kind_type link_type movie_companies movie_info movie_info_idx
  movie_keyword movie_link name person_info role_type title
)
# 所有表名(供判断已导入) 
readonly -a ALL_TABLES=( "${TABLE_NAMES[@]}" )

FORCE=0

# ---------------------------------------------------------------------------
# 解析命令行参数
# ---------------------------------------------------------------------------
usage() {
  cat <<EOF
用法: $0 [--force] [--db NAME] [--pguser USER] [--pghost HOST] [--pgport PORT]

选项:
  --force        强制重建: DROP DATABASE 并完整重新导入全部数据
  --db NAME      数据库名 (默认: imdb)
  --pguser USER  PostgreSQL 用户名 (默认: postgres)
  --pghost HOST  PostgreSQL 主机 (默认: localhost)
  --pgport PORT  PostgreSQL 端口 (默认: 5432)
  -h, --help     显示本帮助

环境变量: PGHOST, PGPORT, PGUSER, PGPASSWORD, DB_NAME
  PGPASSWORD 默认留空; 若需要密码请设置环境变量或使用 ~/.pgpass。

示例:
  PGPASSWORD=postgres ./benchmarks/init_imdb.sh
  PGPASSWORD=postgres FORCE=1 ./benchmarks/init_imdb.sh
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --force) FORCE=1; shift ;;
    --db) DB_NAME="${2:-}"; shift 2 ;;
    --pguser) PGUSER="${2:-}"; shift 2 ;;
    --pghost) PGHOST="${2:-}"; shift 2 ;;
    --pgport) PGPORT="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage >&2; exit 1 ;;
  esac
done

export PGHOST PGPORT PGUSER PGPASSWORD

# psql 辅助: 都开启 ON_ERROR_STOP && 关闭 pager 防分页卡住
psql_main() { psql -X -v ON_ERROR_STOP=1 -P pager=off "$@"; }
psql_db()   { psql -X -v ON_ERROR_STOP=1 -P pager=off -d "${DB_NAME}" "$@"; }

# ---------------------------------------------------------------------------
# 前置检查
# ---------------------------------------------------------------------------
if ! command -v psql >/dev/null 2>&1; then
  echo "错误: 未找到 psql, 请先安装 PostgreSQL 客户端。" >&2
  exit 1
fi
if [[ ! -f "${SCHEMA_SQL}" && -f "${SCHEMA_FALLBACK}" ]]; then
  SCHEMA_SQL="${SCHEMA_FALLBACK}"
fi
if [[ ! -f "${SCHEMA_SQL}" ]]; then
  echo "错误: 找不到 schema 文件 (${SCHEMA_SQL} / ${SCHEMA_FALLBACK})" >&2
  exit 1
fi
if [[ ! -f "${FK_INDEX_SQL}" ]]; then
  echo "错误: 找不到索引文件: ${FK_INDEX_SQL}" >&2
  exit 1
fi

echo "==> PostgreSQL 连接: ${PGUSER}@${PGHOST}:${PGPORT}"
echo "==> 目标数据库: ${DB_NAME}   FORCE=${FORCE}"
echo "==> Schema: ${SCHEMA_SQL}"

if ! psql_main -d postgres -tAc "SELECT 1" >/dev/null 2>&1; then
  echo "错误: 无法连接到 PostgreSQL 服务器 (${PGUSER}@${PGHOST}:${PGPORT})." >&2
  echo "       请检查 PGHOST/PGPORT/PGUSER/PGPASSWORD 是否正确。" >&2
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
                  WHERE n.nspname='public' AND c.relname='${1}' AND c.relkind='r')" \
    | tr -d '[:space:]'
}

# 所有表是否均已导入数据
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
#   将 "CREATE TABLE" 替换为 "CREATE TABLE IF NOT EXISTS",
#   这样重复运行时表已存在也不报错 (幂等)。
# ---------------------------------------------------------------------------
echo "==> 应用 schema: ${SCHEMA_SQL}"
# 生成临时幂等化 schema: 仅把行首的 CREATE TABLE 加上 IF NOT EXISTS
TMP_SCHEMA="$(mktemp)"
sed -E 's/^CREATE TABLE /CREATE TABLE IF NOT EXISTS /I' "${SCHEMA_SQL}" > "${TMP_SCHEMA}"
psql_db -q -f "${TMP_SCHEMA}" || { echo "错误: schema 应用失败。" >&2; rm -f "${TMP_SCHEMA}"; exit 1; }
rm -f "${TMP_SCHEMA}"

# ---------------------------------------------------------------------------
# Step 2: 导入 CSV 数据 (默认只导入缺失的表; FORCE 时无缺失)
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
    # 官方 COPY 参数: delimiter ',' , csv, quote '"', escape '\'
    # (IMDB CSV 用反斜杠转义字段内引号, 不能用 PostgreSQL 默认的 "" 转义)
    psql_db -q <<SQL
\copy ${t} from '${csv}' with delimiter as ',' csv quote '"' escape as '\\';
SQL
  done
fi

# ---------------------------------------------------------------------------
# Step 3: 创建索引 (幂等)
#   将 "create index" 替换为 "create index if not exists",
#   重复运行时不因索引已存在而报错。
# ---------------------------------------------------------------------------
echo "==> 创建/校验索引: ${FK_INDEX_SQL}"
TMP_FK="$(mktemp)"
sed -E 's/^create index /create index if not exists /I' "${FK_INDEX_SQL}" > "${TMP_FK}"
psql_db -q -f "${TMP_FK}" || { echo "错误: 索引创建失败。" >&2; rm -f "${TMP_FK}"; exit 1; }
rm -f "${TMP_FK}"

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
