"""Connection pooling, transactions, and audit logging for dbmaria_project.

Public API:
    get_connection()  -- context manager yielding a pooled mariadb.Connection
    transaction()     -- context manager yielding an audit-logging cursor
    execute()         -- one-shot query helper returning list[dict]
    init_pool()       -- explicit pool configuration
    close_pool()      -- shutdown / test teardown

Credentials are read from ``~/.my.cnf`` by default. The section name and any
individual fields can be overridden via ``init_pool(...)`` keyword arguments.
The database name additionally honors the ``LABDB_DATABASE`` env variable
(env var loses to an explicit ``init_pool(database=...)`` override).

SSH tunneling
-------------
The production database lives on a Galera cluster inside the LiSC network and
is only reachable by SSH-ing through the project VM at
``ccr-lab.lisc.univie.ac.at``. To connect from outside LiSC, supply SSH
parameters and ``init_pool()`` will open a local-port-forwarding tunnel before
creating the pool. The DB ``host``/``port`` you configure are interpreted as
the *remote* DB endpoint (i.e. the Galera cluster as seen from the VM).

SSH parameters (kwargs > ``LABDB_SSH_*`` env vars > ``[labdb-ssh]`` INI section):
    ssh_host, ssh_port (default 22), ssh_user, ssh_password, ssh_pkey,
    ssh_pkey_password.

If ``ssh_host`` is unset the tunnel is skipped and the driver connects
directly to ``host:port`` (useful when running on the VM itself).

Write statements (INSERT/UPDATE/DELETE/REPLACE) issued via ``execute()`` or via
the cursor yielded by ``transaction()`` are appended to an audit log at
``~/.labdb/audit.log`` (override with ``LABDB_AUDIT_LOG``).
"""

from __future__ import annotations

import configparser
import getpass
import logging
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import mariadb


DEFAULT_POOL_SIZE = 10
DEFAULT_CONFIG_PATH = "~/.my.cnf"
DEFAULT_SECTION = "labdb"
DEFAULT_SSH_SECTION = "labdb-ssh"
DEFAULT_DATABASE = "dbmaria_project"

_pool: mariadb.ConnectionPool | None = None
_pool_counter = 0  # appended to pool_name so re-inits do not collide
_tunnel: Any = None  # SSHTunnelForwarder | None; Any avoids importing sshtunnel at module load

_logger = logging.getLogger("dbmaria_utils.audit")
_logger.setLevel(logging.INFO)
_logger.propagate = False  # do not bubble to root logger
_USER = getpass.getuser()

_WRITE_KEYWORDS = ("INSERT", "UPDATE", "DELETE", "REPLACE")
_LEADING_NOISE_RE = re.compile(
    r"\A(?:\s+|--[^\n]*\n?|/\*.*?\*/)+",
    re.DOTALL,
)
_KEYWORD_RE = re.compile(r"\b([A-Za-z_]+)\b")


def _is_write_query(query: str) -> bool:
    """Return True if the first DML keyword of *query* is a write.

    Strips leading whitespace and SQL comments, and skips an optional CTE
    (``WITH ... AS (...)``) prefix before inspecting the keyword. Paren
    depth is tracked so commas/keywords inside the CTE body don't confuse
    the scan.
    """
    s = _LEADING_NOISE_RE.sub("", query)
    if s[:4].upper() == "WITH" and (len(s) == 4 or not s[4].isalnum() and s[4] != "_"):
        depth = 0
        i = 4
        n = len(s)
        while i < n:
            c = s[i]
            if c == "(":
                depth += 1
                i += 1
            elif c == ")":
                depth -= 1
                i += 1
            elif c == "-" and i + 1 < n and s[i + 1] == "-":
                nl = s.find("\n", i)
                i = n if nl == -1 else nl + 1
            elif c == "/" and i + 1 < n and s[i + 1] == "*":
                end = s.find("*/", i + 2)
                i = n if end == -1 else end + 2
            elif c in ("'", '"', "`"):
                j = i + 1
                while j < n:
                    if s[j] == "\\" and j + 1 < n:
                        j += 2
                        continue
                    if s[j] == c:
                        j += 1
                        break
                    j += 1
                i = j
            elif depth == 0 and (c.isalpha() or c == "_"):
                m = _KEYWORD_RE.match(s, i)
                if m:
                    kw = m.group(1).upper()
                    if kw in _WRITE_KEYWORDS:
                        return True
                    if kw == "SELECT":
                        return False
                    i = m.end()
                else:
                    i += 1
            else:
                i += 1
        return False
    m = _KEYWORD_RE.match(s)
    return bool(m) and m.group(1).upper() in _WRITE_KEYWORDS


# --------------------------------------------------------------------------- #
# credentials
# --------------------------------------------------------------------------- #

def _load_credentials(config_path: Path, section: str) -> dict[str, Any]:
    """Read credentials from an INI file. Validates required keys."""
    if not config_path.exists():
        raise FileNotFoundError(
            f"Credentials file not found: {config_path}. "
            f"Create it with a [{section}] section containing user/password."
        )

    cfg = configparser.ConfigParser()
    cfg.read(config_path)

    if section not in cfg:
        raise RuntimeError(
            f"Section [{section}] not found in {config_path}. "
            f"Available sections: {cfg.sections() or '(none)'}"
        )

    sect = cfg[section]
    for required in ("user", "password"):
        if required not in sect:
            raise RuntimeError(
                f"Missing required key {required!r} in [{section}] of {config_path}"
            )

    return {
        "host": sect.get("host", "localhost"),
        "port": int(sect.get("port", "3306")),
        "user": sect["user"],
        "password": sect["password"],
        "database": sect.get("database", DEFAULT_DATABASE),
    }


def _resolve_credentials(
    config_path: str | Path | None,
    section: str,
    overrides: dict[str, Any],
) -> dict[str, Any]:
    """Merge file-based credentials with explicit overrides.

    Resolution order (highest priority first):
      1. Explicit overrides passed to init_pool()
      2. LABDB_DATABASE env var (database field only)
      3. Values from the INI file (if config_path is not None)
      4. Hardcoded defaults
    """
    if config_path is None:
        creds: dict[str, Any] = {
            "host": "localhost",
            "port": 3306,
            "database": DEFAULT_DATABASE,
        }
    else:
        creds = _load_credentials(Path(config_path).expanduser(), section)

    # env-var override for database (loses to explicit init_pool override below)
    env_db = os.environ.get("LABDB_DATABASE")
    if env_db:
        creds["database"] = env_db

    # explicit overrides win over everything
    for key, value in overrides.items():
        if value is not None:
            creds[key] = value

    if not creds.get("user"):
        raise RuntimeError(
            "Cannot initialize pool: 'user' is missing. "
            "Provide it via init_pool(user=...) or in the config file."
        )
    if "password" not in creds or creds["password"] is None:
        raise RuntimeError(
            "Cannot initialize pool: 'password' is missing. "
            "Provide it via init_pool(password=...) or in the config file."
        )

    return creds


# --------------------------------------------------------------------------- #
# SSH tunnel
# --------------------------------------------------------------------------- #

_SSH_ENV_MAP = {
    "ssh_host": "LABDB_SSH_HOST",
    "ssh_port": "LABDB_SSH_PORT",
    "ssh_user": "LABDB_SSH_USER",
    "ssh_password": "LABDB_SSH_PASSWORD",
    "ssh_pkey": "LABDB_SSH_PKEY",
    "ssh_pkey_password": "LABDB_SSH_PKEY_PASSWORD",
}


def _load_ssh_credentials(config_path: Path, section: str) -> dict[str, Any]:
    """Read SSH credentials from an INI section. Returns {} if file or section absent."""
    if not config_path.exists():
        return {}
    cfg = configparser.ConfigParser()
    cfg.read(config_path)
    if section not in cfg:
        return {}

    sect = cfg[section]
    creds: dict[str, Any] = {}
    if "ssh_host" in sect:
        creds["ssh_host"] = sect["ssh_host"]
    if "ssh_port" in sect:
        creds["ssh_port"] = int(sect["ssh_port"])
    for key in ("ssh_user", "ssh_password", "ssh_pkey", "ssh_pkey_password"):
        if key in sect:
            creds[key] = sect[key]
    return creds


def _resolve_ssh_credentials(
    config_path: str | Path | None,
    section: str,
    overrides: dict[str, Any],
) -> dict[str, Any]:
    """Merge INI SSH section with env vars and explicit overrides.

    Resolution order (highest priority first):
      1. Explicit overrides passed to init_pool()
      2. LABDB_SSH_* env vars
      3. Values from the [labdb-ssh] INI section (if config_path is not None)
    """
    if config_path is None:
        creds: dict[str, Any] = {}
    else:
        creds = _load_ssh_credentials(Path(config_path).expanduser(), section)

    for key, env_name in _SSH_ENV_MAP.items():
        env_val = os.environ.get(env_name)
        if env_val:
            creds[key] = int(env_val) if key == "ssh_port" else env_val

    for key, value in overrides.items():
        if value is not None:
            creds[key] = value

    return creds


def _open_tunnel(ssh_creds: dict[str, Any], remote_host: str, remote_port: int) -> Any:
    """Start an SSH tunnel forwarding 127.0.0.1:<random> -> remote_host:remote_port."""
    try:
        from sshtunnel import SSHTunnelForwarder
    except ImportError as exc:
        raise RuntimeError(
            "sshtunnel is required for SSH-tunneled connections. "
            "Reinstall the package or run: pip install sshtunnel"
        ) from exc

    if not ssh_creds.get("ssh_user"):
        raise RuntimeError(
            "Cannot open SSH tunnel: 'ssh_user' is missing. "
            "Provide it via init_pool(ssh_user=...), LABDB_SSH_USER, or the [labdb-ssh] config section."
        )

    kwargs: dict[str, Any] = {
        "ssh_username": ssh_creds["ssh_user"],
        "remote_bind_address": (remote_host, int(remote_port)),
        "local_bind_address": ("127.0.0.1", 0),
    }
    # Auth: pass whichever credentials the user gave; paramiko prefers key over
    # password when both are present, and falls back to the agent / default
    # ~/.ssh/id_* keys when neither is set (look_for_keys is on by default).
    if ssh_creds.get("ssh_pkey"):
        kwargs["ssh_pkey"] = str(Path(ssh_creds["ssh_pkey"]).expanduser())
        if ssh_creds.get("ssh_pkey_password"):
            kwargs["ssh_private_key_password"] = ssh_creds["ssh_pkey_password"]
    if ssh_creds.get("ssh_password"):
        kwargs["ssh_password"] = ssh_creds["ssh_password"]

    ssh_address = (ssh_creds["ssh_host"], int(ssh_creds.get("ssh_port", 22)))

    try:
        tunnel = SSHTunnelForwarder(ssh_address, **kwargs)
        tunnel.start()
    except Exception as exc:
        raise RuntimeError(
            f"Failed to open SSH tunnel to {ssh_address[0]}:{ssh_address[1]}: {exc}"
        ) from exc
    return tunnel


# --------------------------------------------------------------------------- #
# audit logger
# --------------------------------------------------------------------------- #

def _setup_audit_logger() -> None:
    """Configure the audit FileHandler. Idempotent within one pool lifecycle."""
    if _logger.handlers:
        return
    log_path = Path(
        os.environ.get("LABDB_AUDIT_LOG", str(Path.home() / ".labdb" / "audit.log"))
    ).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    handler = logging.StreamHandler(os.fdopen(fd, "a", encoding="utf-8"))
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(user)s | %(message)s")
    )
    _logger.addHandler(handler)


def _teardown_audit_logger() -> None:
    """Close and detach all audit handlers. Used by close_pool()."""
    for handler in list(_logger.handlers):
        handler.close()
        _logger.removeHandler(handler)


def _log_if_write(query: str, params: Any, rowcount: int) -> None:
    """Append an audit entry for INSERT/UPDATE/DELETE/REPLACE statements."""
    if not _is_write_query(query):
        return
    snippet = query.strip().replace("\n", " ")
    if len(snippet) > 200:
        snippet = snippet[:200] + "..."
    if os.environ.get("LABDB_AUDIT_LOG_PARAMS") == "1":
        _logger.info(
            "%s | params=%r | rows=%d",
            snippet, params, rowcount,
            extra={"user": _USER},
        )
    else:
        _logger.info(
            "%s | rows=%d",
            snippet, rowcount,
            extra={"user": _USER},
        )


# --------------------------------------------------------------------------- #
# pool lifecycle
# --------------------------------------------------------------------------- #

def init_pool(
    pool_size: int = DEFAULT_POOL_SIZE,
    *,
    config_path: str | Path | None = DEFAULT_CONFIG_PATH,
    section: str = DEFAULT_SECTION,
    host: str | None = None,
    port: int | None = None,
    user: str | None = None,
    password: str | None = None,
    database: str | None = None,
    ssh_host: str | None = None,
    ssh_port: int | None = None,
    ssh_user: str | None = None,
    ssh_password: str | None = None,
    ssh_pkey: str | None = None,
    ssh_pkey_password: str | None = None,
) -> None:
    """Create the connection pool.

    Raises RuntimeError if the pool is already initialized; call close_pool()
    first to reconfigure. Pass config_path=None to skip the INI file entirely
    and rely solely on the keyword overrides (useful for CI / tests).

    When ssh_host resolves to a non-empty value (via kwarg, LABDB_SSH_HOST, or
    the [labdb-ssh] config section), an SSH tunnel is opened to that host and
    the pool connects through it; the configured DB host:port is the tunnel's
    remote bind target.
    """
    global _pool, _pool_counter, _tunnel
    if _pool is not None:
        raise RuntimeError(
            "pool already initialized; call close_pool() before re-initializing"
        )

    creds = _resolve_credentials(
        config_path,
        section,
        {
            "host": host,
            "port": port,
            "user": user,
            "password": password,
            "database": database,
        },
    )

    ssh_creds = _resolve_ssh_credentials(
        config_path,
        DEFAULT_SSH_SECTION,
        {
            "ssh_host": ssh_host,
            "ssh_port": ssh_port,
            "ssh_user": ssh_user,
            "ssh_password": ssh_password,
            "ssh_pkey": ssh_pkey,
            "ssh_pkey_password": ssh_pkey_password,
        },
    )

    if ssh_creds.get("ssh_host"):
        _tunnel = _open_tunnel(ssh_creds, creds["host"], creds["port"])
        local_host, local_port = _tunnel.local_bind_address
        creds["host"] = local_host
        creds["port"] = int(local_port)

    try:
        _pool_counter += 1
        pool_name = f"dbmaria_utils_{os.getpid()}_{_pool_counter}"
        _pool = mariadb.ConnectionPool(
            pool_name=pool_name,
            pool_size=pool_size,
            autocommit=False,
            **creds,
        )
    except Exception:
        if _tunnel is not None:
            try:
                _tunnel.stop()
            except Exception:
                pass
            _tunnel = None
        raise
    _setup_audit_logger()


def close_pool() -> None:
    """Close the pool, tear down any SSH tunnel, and release the audit log handler."""
    global _pool, _tunnel
    if _pool is not None:
        try:
            _pool.close()
        except Exception:
            pass
        _pool = None
    if _tunnel is not None:
        try:
            _tunnel.stop()
        except Exception:
            pass
        _tunnel = None
    _teardown_audit_logger()


def _get_pool() -> mariadb.ConnectionPool:
    """Return the pool, initializing it lazily with default settings."""
    if _pool is None:
        init_pool()
    assert _pool is not None
    return _pool


# --------------------------------------------------------------------------- #
# connection / transaction / execute
# --------------------------------------------------------------------------- #

@contextmanager
def get_connection() -> Iterator[mariadb.Connection]:
    """Yield a pooled connection. Commits on success, rolls back on exception."""
    conn = _get_pool().get_connection()
    # Pool reset between uses can revert autocommit to the server default
    # (True on MariaDB), which would silently break our rollback path.
    # Force it explicitly on every checkout.
    conn.autocommit = False
    try:
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            # Don't mask the original exception with a rollback failure.
            logging.getLogger(__name__).exception("rollback failed")
        raise
    finally:
        # close() returns the connection to the pool, it does not destroy it.
        try:
            conn.close()
        except Exception:
            logging.getLogger(__name__).exception("connection close failed")


class _LoggingCursor:
    """Cursor wrapper that audits write statements.

    Composition rather than inheritance keeps us decoupled from the driver's
    internal cursor class hierarchy. Unknown attributes are forwarded to the
    underlying cursor.
    """

    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor

    def execute(self, query: str, params: Any = None) -> Any:
        result = self._cursor.execute(query, params if params is not None else ())
        _log_if_write(query, params, self._cursor.rowcount)
        return result

    def executemany(self, query: str, params_seq: Any) -> Any:
        result = self._cursor.executemany(query, params_seq)
        try:
            n = len(params_seq)
        except TypeError:
            n = -1
        _log_if_write(query, f"<batch of {n} rows>", self._cursor.rowcount)
        return result

    def __iter__(self) -> Iterator[Any]:
        return iter(self._cursor)

    def __enter__(self) -> "_LoggingCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        self._cursor.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


@contextmanager
def transaction() -> Iterator[_LoggingCursor]:
    """Yield an audit-logging cursor. All statements share one transaction.

    Commit and rollback are inherited from get_connection(): if the with-block
    exits normally everything commits; if any statement raises, everything
    rolls back atomically.
    """
    with get_connection() as conn:
        cursor = _LoggingCursor(conn.cursor())
        try:
            yield cursor
        finally:
            cursor.close()


def execute(query: str, params: Any = None) -> list[dict[str, Any]]:
    """Run one query and return rows as a list of dicts.

    SELECT statements return one dict per row (column name -> value).
    INSERT/UPDATE/DELETE/REPLACE statements return [] and are audit-logged.

    Each call uses its own pooled connection and its own transaction; for
    multi-statement atomicity use transaction() instead.
    """
    with get_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(query, params if params is not None else ())
            _log_if_write(query, params, cur.rowcount)
            if cur.description is None:
                return []
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]
        finally:
            cur.close()
