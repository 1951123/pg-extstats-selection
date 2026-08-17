"""PostgreSQL connection and execution helpers (psycopg 3).

Provides a thin wrapper around psycopg so the rest of the toolkit deals with
plain Python objects. All connections are made via the DSN-style parameters
from config.DBConfig.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row

from .config import DBConfig


def dsn(cfg: DBConfig) -> str:
    """Build a libpq DSN string from a DBConfig."""
    kwargs = {
        "host": cfg.host,
        "port": str(cfg.port),
        "user": cfg.user,
        "dbname": cfg.dbname,
    }
    # Only include password when set (matching psql's lenient handling).
    if cfg.password:
        kwargs["password"] = cfg.password
    return " ".join(f"{k}={v}" for k, v in kwargs.items())


@contextmanager
def connect(cfg: DBConfig) -> Iterator[Connection]:
    """Open a psycopg connection as a context manager.

    Rows are returned as dicts (row_factory=dict_row). Autocommit is left off
    by default so callers can wrap DDL / inserts in a transaction; use
    ``conn.autocommit = True`` explicitly where needed.
    """
    conn = psycopg.connect(dsn(cfg), row_factory=dict_row)
    try:
        yield conn
    finally:
        conn.close()


def execute(conn: Connection, sql: str, params: tuple[Any, ...] = ()) -> None:
    """Execute a statement (DDL/DML) that returns no rows."""
    with conn.cursor() as cur:
        cur.execute(sql, params)
    conn.commit()


def fetch_all(conn: Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    """Run a query and return all rows as a list of dicts."""
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def drop_statistics(conn: Connection, qualified_name: str, *, if_exists: bool = True) -> None:
    """Drop an extended statistic object by qualified name."""
    sql = f"DROP STATISTICS {'IF EXISTS' if if_exists else ''} {qualified_name}"
    execute(conn, sql)
