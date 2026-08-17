#!/usr/bin/env bash
###############################################################################
# init_census.sh — 幂等地在 PostgreSQL 中创建 USCensus1990 数据库
#
# Census 1990 数据集 (USCensus1990.data.txt):
#   - 单表基准: 所有查询都是 SELECT COUNT(*) FROM climate WHERE ...
#   - 数据为简单的逗号分隔整数 CSV, 首行为属性名(header), 共 69 列
#   - 无空字段, 无内嵌引号/反斜杠, 可直接 COPY 导入
#
# 功能:
#   1. 创建数据库 (默认 census), 已存在则复用
#   2. 依据数据 header 自动生成 climate 表 (全部 integer 列)
#   3. 导入 USCensus1990.data.txt
#   4. 运行 ANALYZE 收集统计信息
#
# 幂等性:
#   - 默认: 若 climate 表已有数据则跳过导入。
#   - FORCE=1: DROP DATABASE 并完整重建、重新导入。
#
# 用法:
#   ./benchmarks/init_census.sh [--force] [--db NAME] ...
#   环境变量: PGHOST / PGPORT / PGUSER / PGPASSWORD / DB_NAME
#
# 例子:
#   PGPASSWORD=postgres ./benchmarks/init_census.sh
#   PGPASSWORD=postgres FORCE=1 ./benchmarks/init_census.sh
###############################################################################

set -euo pipefail
export PGAPPNAME="init_census"

# ---------------------------------------------------------------------------
# 默认配置
# ---------------------------------------------------------------------------
DB_NAME="${DB_NAME:-census}"
PGHOST="${PGHOST:-localhost}"
PGPORT="${PGPORT:-5432}"
PGUSER="${PGUSER:-postgres}"
PGPASSWORD="${PGPASSWORD:-}"

BENCH_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CENSUS_DIR="${BENCH_ROOT}/benchmarks/Census"
DATA_DIR="${CENSUS_DIR}/data"
DATA_FILE="${DATA_DIR}/USCensus1990.data.txt"

# 目标表名 (与 benchmarks/Census/queries/query.sql 中的查询一致)
TABLE_NAME="climate"

FORCE=0

# ---------------------------------------------------------------------------
# 解析命令行参数
# ---------------------------------------------------------------------------
usage() {
  cat <<EOF
用法: $0 [--force] [--db NAME] [--pguser USER] [--pghost HOST] [--pgport PORT]

选项:
  --force        强制重建: DROP DATABASE 并完整重新导入
  --db NAME      数据库名 (默认: census)
  --pguser USER  PostgreSQL 用户名 (默认: postgres)
  --pghost HOST  PostgreSQL 主机 (默认: localhost)
  --pgport PORT  PostgreSQL 端口 (默认: 5432)
  -h, --help     显示本帮助

环境变量: PGHOST, PGPORT, PGUSER, PGPASSWORD, DB_NAME
示例:
  PGPASSWORD=postgres ./benchmarks/init_census.sh
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

psql_main() { psql -X -v ON_ERROR_STOP=1 -P pager=off "$@"; }
psql_db()   { psql -X -v ON_ERROR_STOP=1 -P pager=off -d "${DB_NAME}" "$@"; }

# ---------------------------------------------------------------------------
# 前置检查
# ---------------------------------------------------------------------------
if ! command -v psql >/dev/null 2>&1; then
  echo "错误: 未找到 psql, 请先安装 PostgreSQL 客户端。" >&2
  exit 1
fi
if [[ ! -f "${DATA_FILE}" ]]; then
  echo "错误: 找不到数据文件: ${DATA_FILE}" >&2
  exit 1
fi

echo "==> PostgreSQL 连接: ${PGUSER}@${PGHOST}:${PGPORT}"
echo "==> 目标数据库: ${DB_NAME}   FORCE=${FORCE}"
echo "==> 数据文件: ${DATA_FILE}"

if ! psql_main -d postgres -tAc "SELECT 1" >/dev/null 2>&1; then
  echo "错误: 无法连接到 PostgreSQL 服务器 (${PGUSER}@${PGHOST}:${PGPORT})." >&2
  exit 1
fi
echo "==> 服务器连接成功"

# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------
db_exists() { psql_main -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1; }

table_rows() {
  # 表不存在返回空; 存在返回行数 (分两步, 避免对不存在表执行 count 报错)。
  if [[ "$(psql_db -tAc "SELECT to_regclass('public.${TABLE_NAME}') IS NOT NULL")" != "t" ]]; then
    echo ""
    return
  fi
  psql_db -tAc "SELECT count(*) FROM ${TABLE_NAME}" | tr -d '[:space:]'
}

# 依据数据 header 生成 CREATE TABLE 语句
gen_create_sql() {
  # 取出 header 第一行, 逗号分隔; 每列转为合法小写标识符 + integer
  local header cols=""
  header="$(head -1 "${DATA_FILE}")"
  # awk: 把每个列名清理为合法标识符 (仅字母数字下划线), 全部小写
  cols="$(printf '%s' "$header" | awk -F, '{for(i=1;i<=NF;i++){c=$i; gsub(/[^A-Za-z0-9_]/,"",c); if(i>1)printf ","; printf "\"%s\" integer", tolower(c)}}')"
  printf 'DROP TABLE IF EXISTS %s;\nCREATE TABLE %s (%s);\n' "${TABLE_NAME}" "${TABLE_NAME}" "${cols}"
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
# Step 1: 建表 (依据 header 动态生成, 幂等)
# ---------------------------------------------------------------------------
rows="$(table_rows)"
if [[ -n "$rows" && "$rows" != "0" ]]; then
  echo "==> 表 '${TABLE_NAME}' 已有数据 (${rows} 行), 跳过建表与导入。"
else
  echo "==> 依据数据 header 创建表 '${TABLE_NAME}' ..."
  gen_create_sql | psql_db -q || { echo "错误: 建表失败。" >&2; exit 1; }

  # ---------------------------------------------------------------------------
  # Step 2: 导入 CSV 数据
  #   CSV 带 header, 全部整数, 无引号/反斜杠/空字段, 标准 COPY.
  # ---------------------------------------------------------------------------
  echo "==> 导入数据: ${DATA_FILE##*/} (约 245 万行) ..."
  psql_db -q <<SQL
\copy ${TABLE_NAME} from '${DATA_FILE}' WITH (FORMAT csv, HEADER true);
SQL
fi

# ---------------------------------------------------------------------------
# Step 3: ANALYZE
# ---------------------------------------------------------------------------
echo "==> 运行 ANALYZE ..."
psql_db -c "ANALYZE ${TABLE_NAME};" >/dev/null

echo
echo "===> 完成! 数据库 '${DB_NAME}' 已就绪。"
n="$(table_rows)"
echo "     表 '${TABLE_NAME}' 共 ${n} 行。"
echo "     连接: psql -h ${PGHOST} -p ${PGPORT} -U ${PGUSER} -d ${DB_NAME}"
