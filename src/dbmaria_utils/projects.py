"""CRUD wrapper for the `projects` table.

All functions take a cursor as the first argument so callers control the
transaction boundary:

    from dbmaria_utils import projects, transaction

    with transaction() as cur:
        pid, created = projects.get_or_create(cur, "STUDY42", pi_name="Dr. Test")

Single rows come back as ``dict[str, Any]`` (or ``None`` when missing);
collections come back as ``list[dict[str, Any]]``. Writes go through
``_LoggingCursor`` so they are audit-logged automatically.
"""

from __future__ import annotations

from typing import Any

import mariadb

_COLUMNS = ("project_id", "project_name", "description", "pi_name", "created_at")
_UPDATABLE = ("project_name", "description", "pi_name")
_ORDERABLE = frozenset(_COLUMNS)


def _row_to_dict(cur, row) -> dict[str, Any]:
    return dict(zip([d[0] for d in cur.description], row))


def create(
    cur,
    project_name: str,
    *,
    description: str | None = None,
    pi_name: str | None = None,
) -> int:
    """Insert a project and return its new ``project_id``.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        project_name: Unique name for the new project (column is UNIQUE).
        description: Optional free-text description.
        pi_name: Optional principal investigator name.

    Returns:
        The newly inserted ``project_id``.

    Raises:
        mariadb.IntegrityError: If ``project_name`` already exists. Use
            [`get_or_create`][dbmaria_utils.projects.get_or_create] for
            idempotent inserts.
    """
    cur.execute(
        "INSERT INTO projects (project_name, description, pi_name) "
        "VALUES (?, ?, ?)",
        (project_name, description, pi_name),
    )
    return cur.lastrowid


def get(cur, project_id: int) -> dict[str, Any] | None:
    """Return the project row for a given id.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        project_id: Primary key to look up.

    Returns:
        The row as ``dict[str, Any]``, or ``None`` if no project has that id.
    """
    cur.execute("SELECT * FROM projects WHERE project_id = ?", (project_id,))
    row = cur.fetchone()
    return _row_to_dict(cur, row) if row is not None else None


def get_by_name(cur, project_name: str) -> dict[str, Any] | None:
    """Return the project row for a given name.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        project_name: Name to look up (column is UNIQUE).

    Returns:
        The row as ``dict[str, Any]``, or ``None`` if no project has that name.
    """
    cur.execute("SELECT * FROM projects WHERE project_name = ?", (project_name,))
    row = cur.fetchone()
    return _row_to_dict(cur, row) if row is not None else None


def get_or_create(
    cur,
    project_name: str,
    *,
    description: str | None = None,
    pi_name: str | None = None,
) -> tuple[int, bool]:
    """Idempotently return the project id, inserting if needed.

    Tries to fetch first; on miss inserts. If a parallel transaction wins
    a UNIQUE-violation race, falls back to a second fetch. Existing rows
    are returned as-is — *description* and *pi_name* are not used to
    update an existing row.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        project_name: Unique project name.
        description: Used only on insert.
        pi_name: Used only on insert.

    Returns:
        ``(project_id, created)`` where ``created`` is ``True`` iff this
        call inserted the row.

    Raises:
        mariadb.IntegrityError: If the race-recovery fetch also misses
            (i.e. the IntegrityError was not due to a duplicate name).
    """
    existing = get_by_name(cur, project_name)
    if existing is not None:
        return int(existing["project_id"]), False
    try:
        return create(cur, project_name, description=description, pi_name=pi_name), True
    except mariadb.IntegrityError:
        existing = get_by_name(cur, project_name)
        if existing is None:
            raise
        return int(existing["project_id"]), False


def list_all(cur, *, order_by: str = "project_id") -> list[dict[str, Any]]:
    """Return all projects.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        order_by: Column name to order by. Must be one of the columns of
            the ``projects`` table.

    Returns:
        All rows as ``list[dict[str, Any]]``.

    Raises:
        ValueError: If ``order_by`` is not a known column name.
    """
    if order_by not in _ORDERABLE:
        raise ValueError(f"order_by must be one of {sorted(_ORDERABLE)}, got {order_by!r}")
    cur.execute(f"SELECT * FROM projects ORDER BY {order_by}")
    rows = cur.fetchall()
    columns = [d[0] for d in cur.description]
    return [dict(zip(columns, row)) for row in rows]


def update(
    cur,
    project_id: int,
    *,
    project_name: str | None = None,
    description: str | None = None,
    pi_name: str | None = None,
) -> bool:
    """Partial update of a project row.

    Only kwargs with non-None values are written. Setting a column to
    SQL NULL is not supported by this helper.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        project_id: Row to update.
        project_name: New name (if not None).
        description: New description (if not None).
        pi_name: New PI name (if not None).

    Returns:
        ``True`` iff exactly one row was updated. ``False`` (and no SQL
        is run) when every kwarg is None.
    """
    fields = {
        "project_name": project_name,
        "description": description,
        "pi_name": pi_name,
    }
    assignments = [(col, val) for col, val in fields.items() if val is not None]
    if not assignments:
        return False
    set_clause = ", ".join(f"{col} = ?" for col, _ in assignments)
    params = [val for _, val in assignments]
    params.append(project_id)
    cur.execute(f"UPDATE projects SET {set_clause} WHERE project_id = ?", tuple(params))
    return cur.rowcount > 0


def delete(cur, project_id: int) -> bool:
    """Delete a project.

    ``subjects.project_id`` declares ``ON DELETE CASCADE``, so this also
    removes every subject, visit, sample, and metadata row owned by the
    project. ``sample_files`` uses ``ON DELETE RESTRICT`` and will block
    the delete instead — clean those up first.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        project_id: Row to delete.

    Returns:
        ``True`` iff a row was removed.
    """
    cur.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))
    return cur.rowcount > 0


def exists(
    cur,
    project_id: int | None = None,
    *,
    name: str | None = None,
) -> bool:
    """Return whether a project with the given id or name exists.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        project_id: Id to check (exclusive with ``name``).
        name: Name to check (exclusive with ``project_id``).

    Returns:
        ``True`` if a matching row exists.

    Raises:
        ValueError: If both or neither of ``project_id`` / ``name`` is given.
    """
    if (project_id is None) == (name is None):
        raise ValueError("exists() requires exactly one of project_id or name")
    if project_id is not None:
        cur.execute("SELECT 1 FROM projects WHERE project_id = ?", (project_id,))
    else:
        cur.execute("SELECT 1 FROM projects WHERE project_name = ?", (name,))
    return cur.fetchone() is not None


def count(cur) -> int:
    """Return the total number of projects.

    Args:
        cur: Audit-logging cursor from `transaction()`.

    Returns:
        Row count of the ``projects`` table.
    """
    cur.execute("SELECT COUNT(*) FROM projects")
    row = cur.fetchone()
    return int(row[0])
