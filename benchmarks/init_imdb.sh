#!/usr/bin/env bash
###############################################################################
# init_imdb.sh -- idempotently create the JOB (Join Order Benchmark) / IMDB
#                database in PostgreSQL
#
# Reference: https://zhuanlan.zhihu.com/p/637260071
#   The IMDB data CSVs escape quoted fields with `\` (backslash), so COPY must
#   specify `escape as '\'` instead of PostgreSQL's default `""` escape.
#
# Function:
#   1. Create the database (default 'imdb'), reuse if it already exists
#   2. Apply the schema
#   3. Load JOB data with the official COPY parameters (benchmarks/JOB/data/*.csv)
#   4. Create query-required indexes (benchmarks/JOB/fkindexes.sql)
#   5. Run ANALYZE to collect statistics
#
# Idempotency:
#   - Default mode: if the database exists and all tables already have data,
#     skip the import entirely (nothing to do). If some tables are missing,
#     import only those and fill in the remaining steps.
#   - FORCE=1 mode: DROP DATABASE and fully rebuild + re-import everything.
#
# Usage:
#   ./benchmarks/init_imdb.sh [--force] [--db NAME] ...
#
# Common env vars (consistent with psql):
#   PGHOST / PGPORT / PGUSER / PGPASSWORD / DB_NAME
#
# Examples:
#   ./benchmarks/init_imdb.sh                                   # idempotent import
#   FORCE=1 ./benchmarks/init_imdb.sh                           # force rebuild
#   PGHOST=10.0.0.5 PGPORT=5433 PGPASSWORD=secret ./benchmarks/init_imdb.sh
###############################################################################

set -euo pipefail
export PGAPPNAME="init_imdb"

# ---------------------------------------------------------------------------
# Default configuration (overridable by env vars / CLI args)
# ---------------------------------------------------------------------------
DB_NAME="${DB_NAME:-imdb}"
PGHOST="${PGHOST:-localhost}"
PGPORT="${PGPORT:-5432}"
PGUSER="${PGUSER:-postgres}"
PGPASSWORD="${PGPASSWORD:-}"

# Absolute path to the JOB data
BENCH_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
JOB_DIR="${BENCH_ROOT}/benchmarks/JOB"
# Official schema co-located with the data (schematext.sql); fall back to schema.sql
SCHEMA_SQL="${JOB_DIR}/data/schematext.sql"
SCHEMA_FALLBACK="${JOB_DIR}/schema.sql"
FK_INDEX_SQL="${JOB_DIR}/fkindexes.sql"
DATA_DIR="${JOB_DIR}/data"

# Table -> CSV filename mapping (order matches the reference example)
readonly -a TABLE_NAMES=(
  aka_name aka_title cast_info char_name comp_cast_type
  company_name company_type complete_cast info_type keyword
  kind_type link_type movie_companies movie_info movie_info_idx
  movie_keyword movie_link name person_info role_type title
)
# All table names (for import-completeness checks)
readonly -a ALL_TABLES=( "${TABLE_NAMES[@]}" )

FORCE=0

# ---------------------------------------------------------------------------
# Parse command-line arguments
# ---------------------------------------------------------------------------
usage() {
  cat <<EOF
Usage: $0 [--force] [--db NAME] [--pguser USER] [--pghost HOST] [--pgport PORT]

Options:
  --force        Force rebuild: DROP DATABASE and full re-import of all data
  --db NAME      Database name (default: imdb)
  --pguser USER  PostgreSQL user (default: postgres)
  --pghost HOST  PostgreSQL host (default: localhost)
  --pgport PORT  PostgreSQL port (default: 5432)
  -h, --help     Show this help

Env: PGHOST, PGPORT, PGUSER, PGPASSWORD, DB_NAME
  PGPASSWORD is empty by default; set it or use ~/.pgpass if a password is needed.

Example:
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
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
done

export PGHOST PGPORT PGUSER PGPASSWORD

# psql helpers: always ON_ERROR_STOP, pager off to avoid hanging on long output
psql_main() { psql -X -v ON_ERROR_STOP=1 -P pager=off "$@"; }
psql_db()   { psql -X -v ON_ERROR_STOP=1 -P pager=off -d "${DB_NAME}" "$@"; }

# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------
if ! command -v psql >/dev/null 2>&1; then
  echo "Error: psql not found; install the PostgreSQL client first." >&2
  exit 1
fi
if [[ ! -f "${SCHEMA_SQL}" && -f "${SCHEMA_FALLBACK}" ]]; then
  SCHEMA_SQL="${SCHEMA_FALLBACK}"
fi
if [[ ! -f "${SCHEMA_SQL}" ]]; then
  echo "Error: schema file not found (${SCHEMA_SQL} / ${SCHEMA_FALLBACK})" >&2
  exit 1
fi
if [[ ! -f "${FK_INDEX_SQL}" ]]; then
  echo "Error: index file not found: ${FK_INDEX_SQL}" >&2
  exit 1
fi

echo "==> PostgreSQL connection: ${PGUSER}@${PGHOST}:${PGPORT}"
echo "==> Target database: ${DB_NAME}   FORCE=${FORCE}"
echo "==> Schema: ${SCHEMA_SQL}"

if ! psql_main -d postgres -tAc "SELECT 1" >/dev/null 2>&1; then
  echo "Error: cannot connect to PostgreSQL server (${PGUSER}@${PGHOST}:${PGPORT})." >&2
  echo "       Please check PGHOST/PGPORT/PGUSER/PGPASSWORD." >&2
  exit 1
fi
echo "==> Server connection OK"

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
db_exists() { psql_main -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1; }

# Return the row count of a table (empty if the table does not exist or is empty)
table_rows() {
  psql_db -tAc "SELECT (SELECT count(*) FROM ${1})::text
                WHERE EXISTS (
                  SELECT 1 FROM pg_class c JOIN pg_namespace n
                  ON n.oid=c.relnamespace
                  WHERE n.nspname='public' AND c.relname='${1}' AND c.relkind='r')" \
    | tr -d '[:space:]'
}

# Whether all tables have been loaded
is_fully_loaded() {
  local t n
  for t in "${ALL_TABLES[@]}"; do
    n="$(table_rows "$t")"
    [[ -z "$n" || "$n" == "0" ]] && return 1
  done
  return 0
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
# Step 1: apply schema (idempotent)
#   Replace "CREATE TABLE" with "CREATE TABLE IF NOT EXISTS" so re-running does
#   not fail when the tables already exist.
# ---------------------------------------------------------------------------
echo "==> Applying schema: ${SCHEMA_SQL}"
# Generate a temporary idempotent schema: only add IF NOT EXISTS to leading CREATE TABLE
TMP_SCHEMA="$(mktemp)"
sed -E 's/^CREATE TABLE /CREATE TABLE IF NOT EXISTS /I' "${SCHEMA_SQL}" > "${TMP_SCHEMA}"
psql_db -q -f "${TMP_SCHEMA}" || { echo "Error: schema application failed." >&2; rm -f "${TMP_SCHEMA}"; exit 1; }
rm -f "${TMP_SCHEMA}"

# ---------------------------------------------------------------------------
# Step 2: load CSV data (default: only missing tables; FORCE means none missing)
# ---------------------------------------------------------------------------
missing=()
for t in "${ALL_TABLES[@]}"; do
  n="$(table_rows "$t")"
  [[ -z "$n" || "$n" == "0" ]] && missing+=("$t")
done

if [[ "${#missing[@]}" -eq 0 ]]; then
  echo "==> All tables already loaded; skipping data import."
else
  echo "==> Tables to load (${#missing[@]}): ${missing[*]}"
  for t in "${missing[@]}"; do
    csv="${DATA_DIR}/${t}.csv"
    if [[ ! -f "$csv" ]]; then
      echo "    Warning: data file not found ${csv}; skipping table ${t}" >&2
      continue
    fi
    echo "    -- loading table '${t}' from ${t}.csv"
    # Official COPY parameters: delimiter ',', csv, quote '"', escape '\'
    # (IMDB CSV escapes in-field quotes with backslash, not PostgreSQL's default "")
    psql_db -q <<SQL
\copy ${t} from '${csv}' with delimiter as ',' csv quote '"' escape as '\\';
SQL
  done
fi

# ---------------------------------------------------------------------------
# Step 3: create indexes (idempotent)
#   Replace "create index" with "create index if not exists" so re-running does
#   not fail because the indexes already exist.
# ---------------------------------------------------------------------------
echo "==> Creating/verifying indexes: ${FK_INDEX_SQL}"
TMP_FK="$(mktemp)"
sed -E 's/^create index /create index if not exists /I' "${FK_INDEX_SQL}" > "${TMP_FK}"
psql_db -q -f "${TMP_FK}" || { echo "Error: index creation failed." >&2; rm -f "${TMP_FK}"; exit 1; }
rm -f "${TMP_FK}"

# ---------------------------------------------------------------------------
# Step 4: ANALYZE
# ---------------------------------------------------------------------------
echo "==> Running ANALYZE ..."
psql_db -c "ANALYZE;" >/dev/null

echo
echo "===> Done! Database '${DB_NAME}' is ready."
