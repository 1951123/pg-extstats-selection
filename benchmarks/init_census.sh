#!/usr/bin/env bash
###############################################################################
# init_census.sh -- idempotently create the USCensus1990 database in PostgreSQL
#
# Census 1990 dataset (USCensus1990.data.txt):
#   - Single-table benchmark: all queries are SELECT COUNT(*) FROM climate WHERE ...
#   - Data is a simple comma-separated integer CSV, first row = attribute names
#     (header), 69 columns total
#   - No empty fields, no embedded quotes/backslashes, ready for COPY import
#
# Function:
#   1. Create the database (default 'census'); reuse if it already exists
#   2. Generate the 'climate' table from the data header (all integer columns)
#   3. Load USCensus1990.data.txt
#   4. Run ANALYZE to collect statistics
#
# Idempotency:
#   - Default: skip import if the climate table already has data.
#   - FORCE=1: DROP DATABASE and fully rebuild + re-import.
#
# Usage:
#   ./benchmarks/init_census.sh [--force] [--db NAME] ...
#   Env: PGHOST / PGPORT / PGUSER / PGPASSWORD / DB_NAME
#
# Examples:
#   PGPASSWORD=postgres ./benchmarks/init_census.sh
#   PGPASSWORD=postgres FORCE=1 ./benchmarks/init_census.sh
###############################################################################

set -euo pipefail
export PGAPPNAME="init_census"

# ---------------------------------------------------------------------------
# Default configuration
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

# Target table name (consistent with the queries in benchmarks/Census/queries/query.sql)
TABLE_NAME="climate"

FORCE=0

# ---------------------------------------------------------------------------
# Parse command-line arguments
# ---------------------------------------------------------------------------
usage() {
  cat <<EOF
Usage: $0 [--force] [--db NAME] [--pguser USER] [--pghost HOST] [--pgport PORT]

Options:
  --force        Force rebuild: DROP DATABASE and full re-import
  --db NAME      Database name (default: census)
  --pguser USER  PostgreSQL user (default: postgres)
  --pghost HOST  PostgreSQL host (default: localhost)
  --pgport PORT  PostgreSQL port (default: 5432)
  -h, --help     Show this help

Env: PGHOST, PGPORT, PGUSER, PGPASSWORD, DB_NAME
Example:
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
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
done

export PGHOST PGPORT PGUSER PGPASSWORD

psql_main() { psql -X -v ON_ERROR_STOP=1 -P pager=off "$@"; }
psql_db()   { psql -X -v ON_ERROR_STOP=1 -P pager=off -d "${DB_NAME}" "$@"; }

# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------
if ! command -v psql >/dev/null 2>&1; then
  echo "Error: psql not found; install the PostgreSQL client first." >&2
  exit 1
fi
if [[ ! -f "${DATA_FILE}" ]]; then
  echo "Error: data file not found: ${DATA_FILE}" >&2
  exit 1
fi

echo "==> PostgreSQL connection: ${PGUSER}@${PGHOST}:${PGPORT}"
echo "==> Target database: ${DB_NAME}   FORCE=${FORCE}"
echo "==> Data file: ${DATA_FILE}"

if ! psql_main -d postgres -tAc "SELECT 1" >/dev/null 2>&1; then
  echo "Error: cannot connect to PostgreSQL server (${PGUSER}@${PGHOST}:${PGPORT})." >&2
  exit 1
fi
echo "==> Server connection OK"

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
db_exists() { psql_main -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1; }

table_rows() {
  # Return empty if table does not exist; else return the row count
  # (in two steps to avoid a count error on a non-existent table).
  if [[ "$(psql_db -tAc "SELECT to_regclass('public.${TABLE_NAME}') IS NOT NULL")" != "t" ]]; then
    echo ""
    return
  fi
  psql_db -tAc "SELECT count(*) FROM ${TABLE_NAME}" | tr -d '[:space:]'
}

# Generate the CREATE TABLE statement from the data header
gen_create_sql() {
  # Take the first (header) line, comma-separated; map each column to a valid
  # lowercase identifier + integer
  local header cols=""
  header="$(head -1 "${DATA_FILE}")"
  # awk: clean each column name to a valid identifier (alphanumeric/underscore), lowercase
  cols="$(printf '%s' "$header" | awk -F, '{for(i=1;i<=NF;i++){c=$i; gsub(/[^A-Za-z0-9_]/,"",c); if(i>1)printf ","; printf "\"%s\" integer", tolower(c)}}')"
  printf 'DROP TABLE IF EXISTS %s;\nCREATE TABLE %s (%s);\n' "${TABLE_NAME}" "${TABLE_NAME}" "${cols}"
}

# ---------------------------------------------------------------------------
# Step 0: database existence
# ---------------------------------------------------------------------------
if [[ "$FORCE" == "1" ]]; then
  echo "==> [FORCE] dropping and rebuilding database '${DB_NAME}' ..."
  psql_main -d postgres -c "DROP DATABASE IF EXISTS \"${DB_NAME}\" WITH (FORCE);"
  createdb -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" "${DB_NAME}"
elif ! db_exists; then
  echo "==> Database '${DB_NAME}' does not exist; creating ..."
  createdb -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" "${DB_NAME}"
else
  echo "==> Database '${DB_NAME}' already exists; reusing."
fi

# ---------------------------------------------------------------------------
# Step 1: create table (generated from header, idempotent)
# ---------------------------------------------------------------------------
rows="$(table_rows)"
if [[ -n "$rows" && "$rows" != "0" ]]; then
  echo "==> Table '${TABLE_NAME}' already has data (${rows} rows); skipping table creation and import."
else
  echo "==> Creating table '${TABLE_NAME}' from data header ..."
  gen_create_sql | psql_db -q || { echo "Error: table creation failed." >&2; exit 1; }

  # ---------------------------------------------------------------------------
  # Step 2: import CSV data
  #   CSV has a header, all integers, no quotes/backslashes/empty fields, standard COPY.
  # ---------------------------------------------------------------------------
  echo "==> Loading data: ${DATA_FILE##*/} (~2.45M rows) ..."
  psql_db -q <<SQL
\copy ${TABLE_NAME} from '${DATA_FILE}' WITH (FORMAT csv, HEADER true);
SQL
fi

# ---------------------------------------------------------------------------
# Step 3: ANALYZE
# ---------------------------------------------------------------------------
echo "==> Running ANALYZE ..."
psql_db -c "ANALYZE ${TABLE_NAME};" >/dev/null

echo
echo "===> Done! Database '${DB_NAME}' is ready."
n="$(table_rows)"
echo "     Table '${TABLE_NAME}' has ${n} rows."
echo "     Connect: psql -h ${PGHOST} -p ${PGPORT} -U ${PGUSER} -d ${DB_NAME}"
