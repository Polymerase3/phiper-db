"""Test fixtures: reset the database, load the schema, configure the pool."""

from __future__ import annotations

import os
import re
from pathlib import Path

import mariadb
import pytest

from noxdb import close_pool, get_connection, init_pool

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = REPO_ROOT / "schema"
# Apply every numbered migration in order (001_initial, 002_*, 003_*, …)
# so the test DB matches production after all migrations.
SCHEMA_FILES = sorted(SCHEMA_DIR.glob("[0-9][0-9][0-9]_*.sql"))
DB_NAME = os.environ.get("DB_NAME", "ccr_metadata")

if not re.fullmatch(r"[A-Za-z0-9_]+", DB_NAME):
    raise RuntimeError(
        f"Invalid DB_NAME {DB_NAME!r}: must match ^[A-Za-z0-9_]+$"
    )


def _server_conn():
    """Direct admin connection used to bootstrap the database itself.

    This bypasses the pool intentionally: the pool is bound to a specific
    database, but DROP/CREATE DATABASE is server-level work that runs before
    the target database exists.
    """
    return mariadb.connect(
        host=os.environ.get("DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ.get("DB_USER", "root"),
        password=os.environ.get("DB_PASSWORD", ""),
        autocommit=True,
    )


def _split_sql(sql: str) -> list[str]:
    statements: list[str] = []
    buf: list[str] = []
    for raw_line in sql.splitlines():
        line = raw_line.rstrip()
        # Strip full-line SQL comments
        if line.lstrip().startswith("--") or not line.strip():
            continue
        buf.append(line)
        if line.rstrip().endswith(";"):
            stmt = "\n".join(buf).rstrip().rstrip(";").strip()
            if stmt:
                statements.append(stmt)
            buf = []
    return statements


def _is_db_selection_stmt(stmt: str) -> bool:
    head = stmt.lstrip().upper()
    return head.startswith("CREATE DATABASE") or head.startswith("USE ")


@pytest.fixture(scope="session")
def fresh_db():
    """Drop, recreate, and load schema. Yields nothing — env vars carry config."""
    conn = _server_conn()
    cur = conn.cursor()
    cur.execute(f"DROP DATABASE IF EXISTS `{DB_NAME}`")
    cur.execute(
        f"CREATE DATABASE `{DB_NAME}` "
        "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
    )
    cur.execute("SET sql_mode = 'NO_ENGINE_SUBSTITUTION'")
    cur.close()
    conn.close()

    conn = _server_conn()
    cur = conn.cursor()
    cur.execute(f"USE `{DB_NAME}`")
    for schema_file in SCHEMA_FILES:
        for stmt in _split_sql(schema_file.read_text()):
            if _is_db_selection_stmt(stmt):
                continue
            cur.execute(stmt)
    cur.close()
    conn.close()
    yield


@pytest.fixture(scope="session")
def _init_pool(fresh_db, tmp_path_factory):
    """Configure the connection pool against the freshly created test DB.

    Yields the audit log path so tests can verify audit entries on disk.
    Credentials come from env vars (DB_HOST/PORT/USER/PASSWORD) so CI does
    not need a ~/.my.cnf file.
    """
    audit_path = tmp_path_factory.mktemp("audit") / "audit.log"
    os.environ["NOXDB_AUDIT_LOG"] = str(audit_path)

    init_pool(
        config_path=None,
        host=os.environ.get("DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ.get("DB_USER", "root"),
        password=os.environ.get("DB_PASSWORD", ""),
        database=DB_NAME,
    )
    yield audit_path
    close_pool()
    os.environ.pop("NOXDB_AUDIT_LOG", None)


@pytest.fixture
def db_conn(_init_pool):
    """A pooled connection. Commits on success, rolls back on exception."""
    with get_connection() as conn:
        yield conn


