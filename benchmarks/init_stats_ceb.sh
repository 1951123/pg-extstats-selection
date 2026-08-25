#!/usr/bin/env bash
###############################################################################
# init_stats_ceb.sh -- idempotently create the stats_CEB (Cardinality
#                     Estimation Benchmark on StackExchange data) database
#
# stats_CEB data: StackExchange posts/users/votes/badges/comments/
#                 postHistory/postLinks/tags, for cardinality-estimation
#                 (join order) testing.
#
# Difference vs. JOB (init_imdb.sh):
#   - CSV has a header row; COPY must specify HEADER true (JOB's CSV has no header)
#   - CSV has no embedded quotes/backslashes; no escape '\' handling needed, a
#     standard CSV COPY suffices
#
# Function:
#   1. Create the database (default 'stats'), reuse if it already exists
#   2. Apply the schema (benchmarks/stats_CEB/data/stats.sql)
#   3. Load the 8 tables' CSV data
#   4. (Optional) Build indexes (default off, following the benchmark's
#      "no secondary indexes" convention to keep join-order tests fair)
#   5. Run ANALYZE to collect statistics
#
# Idempotency:
#   - Default: skip import if all tables already have data; load only missing ones.
#   - FORCE=1: DROP DATABASE and fully rebuild.
#
# Usage:
#   ./benchmarks/init_stats_ceb.sh [--force] [--db NAME] ...
#   Env: PGHOST / PGPORT / PGUSER / PGPASSWORD / DB_NAME
#
# Examples:
#   PGPASSWORD=postgres ./benchmarks/init_stats_ceb.sh
#   PGPASSWORD=postgres FORCE=1 ./benchmarks/init_stats_ceb.sh
###############################################################################

set -euo pipefail
export PGAPPNAME="init_stats_ceb"

# ---------------------------------------------------------------------------
# Default configuration (overridable by env vars / CLI args)
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

# Whether to create secondary indexes (0=no, 1=yes). Off by default, following
# the benchmark's "no secondary index" convention.
BUILD_INDEX="${BUILD_INDEX:-0}"

# Import order of the 8 tables (no dependencies; order arbitrary)
readonly -a ALL_TABLES=(
  users posts postLinks postHistory comments votes badges tags
)

FORCE=0

# ---------------------------------------------------------------------------
# Parse command-line arguments
# ---------------------------------------------------------------------------
usage() {
  cat <<EOF
Usage: $0 [--force] [--build-index] [--db NAME] [--pguser USER] \
[--pghost HOST] [--pgport PORT]

Options:
  --force        Force rebuild: DROP DATABASE and full re-import of all data
  --build-index  Create secondary indexes (for faster queries; default off to
                 follow the benchmark's no-secondary-index convention)
  --db NAME      Database name (default: stats)
  --pguser USER  PostgreSQL user (default: postgres)
  --pghost HOST  PostgreSQL host (default: localhost)
  --pgport PORT  PostgreSQL port (default: 5432)
  -h, --help     Show this help

Env: PGHOST, PGPORT, PGUSER, PGPASSWORD, DB_NAME, BUILD_INDEX
Example:
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
if [[ ! -f "${SCHEMA_SQL}" ]]; then
  echo "Error: schema file not found: ${SCHEMA_SQL}" >&2
  exit 1
fi

echo "==> PostgreSQL connection: ${PGUSER}@${PGHOST}:${PGPORT}"
echo "==> Target database: ${DB_NAME}   FORCE=${FORCE}   BUILD_INDEX=${BUILD_INDEX}"
echo "==> Schema: ${SCHEMA_SQL}"

if ! psql_main -d postgres -tAc "SELECT 1" >/dev/null 2>&1; then
  echo "Error: cannot connect to PostgreSQL server (${PGUSER}@${PGHOST}:${PGPORT})." >&2
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
#   Replace "CREATE TABLE" with "CREATE TABLE IF NOT EXISTS"
# ---------------------------------------------------------------------------
echo "==> Applying schema: ${SCHEMA_SQL}"
TMP_SCHEMA="$(mktemp)"
sed -E 's/^CREATE TABLE /CREATE TABLE IF NOT EXISTS /I' "${SCHEMA_SQL}" > "${TMP_SCHEMA}"
psql_db -q -f "${TMP_SCHEMA}" || { echo "Error: schema application failed." >&2; rm -f "${TMP_SCHEMA}"; exit 1; }
rm -f "${TMP_SCHEMA}"

# ---------------------------------------------------------------------------
# Step 2: load CSV data
#   CSV has a header; use HEADER true; empty fields -> NULL (NULL '').
#   No embedded quotes/backslashes, standard FORMAT csv.
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
    # CSV has a header row (auto-skipped); empty fields -> NULL
    psql_db -q <<SQL
\copy ${t} from '${csv}' WITH (FORMAT csv, HEADER true, NULL '');
SQL
  done
fi

# ---------------------------------------------------------------------------
# Step 3: (optional) create secondary indexes
#   Off by default. When enabled, build indexes on join columns to speed up
#   queries; note this may affect the fairness of join-order benchmarks, use
#   with care.
# ---------------------------------------------------------------------------
if [[ "${BUILD_INDEX}" == "1" ]]; then
  echo "==> Creating secondary indexes ..."
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
echo "==> Running ANALYZE ..."
psql_db -c "ANALYZE;" >/dev/null

echo
echo "===> Done! Database '${DB_NAME}' is ready."
