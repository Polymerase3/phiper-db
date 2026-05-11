"""CRUD wrapper for the `subjects` table.

Same call style as [`dbmaria_utils.projects`][dbmaria_utils.projects] —
every function takes a cursor first; callers wrap them in
``with transaction() as cur:``.

The natural key is the composite ``(project_id, subject_code)`` — a
``subject_code`` can repeat across projects, so most lookups go through
[`get_by_code`][dbmaria_utils.subjects.get_by_code] rather than
[`get`][dbmaria_utils.subjects.get].
"""

from __future__ import annotations

from typing import Any

import mariadb

_COLUMNS = (
    "subject_id",
    "project_id",
    "subject_code",
    "sex",
    "origin",
    "created_at",
)
_ORDERABLE = frozenset(_COLUMNS)


def _row_to_dict(cur, row) -> dict[str, Any]:
    return dict(zip([d[0] for d in cur.description], row))


def create(
    cur,
    project_id: int,
    subject_code: str,
    sex: str,
    *,
    origin: str | None = None,
) -> int:
    """Insert a subject and return its new ``subject_id``.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        project_id: Parent project. Must already exist.
        subject_code: Stable code, unique within the project.
        sex: ``'M'`` or ``'F'`` (DB-side `CHECK` constraint).
        origin: Optional free-text origin.

    Returns:
        The newly inserted ``subject_id``.

    Raises:
        mariadb.IntegrityError: If ``(project_id, subject_code)`` already
            exists, ``project_id`` does not reference an existing
            project, or ``sex`` is not in ``('M', 'F')``. Use
            [`get_or_create`][dbmaria_utils.subjects.get_or_create] for
            idempotent inserts.
    """
    cur.execute(
        "INSERT INTO subjects (project_id, subject_code, sex, origin) "
        "VALUES (?, ?, ?, ?)",
        (project_id, subject_code, sex, origin),
    )
    return cur.lastrowid


def get(cur, subject_id: int) -> dict[str, Any] | None:
    """Return the subject row for a given id.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        subject_id: Primary key to look up.

    Returns:
        The row as ``dict[str, Any]``, or ``None`` if not found.
    """
    cur.execute("SELECT * FROM subjects WHERE subject_id = ?", (subject_id,))
    row = cur.fetchone()
    return _row_to_dict(cur, row) if row is not None else None


def get_by_code(
    cur, project_id: int, subject_code: str
) -> dict[str, Any] | None:
    """Return the subject row for the composite natural key.

    Hot path for CSV importers: look up by ``(project_id, subject_code)``
    before inserting.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        project_id: Parent project.
        subject_code: Subject code, unique within the project.

    Returns:
        The row as ``dict[str, Any]``, or ``None`` if not found.
    """
    cur.execute(
        "SELECT * FROM subjects WHERE project_id = ? AND subject_code = ?",
        (project_id, subject_code),
    )
    row = cur.fetchone()
    return _row_to_dict(cur, row) if row is not None else None


def get_or_create(
    cur,
    project_id: int,
    subject_code: str,
    sex: str,
    *,
    origin: str | None = None,
) -> tuple[int, bool]:
    """Idempotently return the subject id, inserting if needed.

    Existing rows are returned as-is — *sex* and *origin* are not used
    to update an existing row. Falls back to a re-fetch on the
    UNIQUE-violation race where another transaction inserted the same
    key in parallel.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        project_id: Parent project.
        subject_code: Subject code, unique within the project.
        sex: Used only on insert.
        origin: Used only on insert.

    Returns:
        ``(subject_id, created)`` where ``created`` is ``True`` iff this
        call inserted the row.

    Raises:
        mariadb.IntegrityError: If the race-recovery fetch also misses.
    """
    existing = get_by_code(cur, project_id, subject_code)
    if existing is not None:
        return int(existing["subject_id"]), False
    try:
        new_id = create(
            cur, project_id, subject_code, sex, origin=origin,
        )
        return new_id, True
    except mariadb.IntegrityError:
        existing = get_by_code(cur, project_id, subject_code)
        if existing is None:
            raise
        return int(existing["subject_id"]), False


def list_for_project(
    cur, project_id: int, *, order_by: str = "subject_id"
) -> list[dict[str, Any]]:
    """Return all subjects belonging to a project.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        project_id: Project to list.
        order_by: Column name to order by. Must be a column of ``subjects``.

    Returns:
        All matching rows as ``list[dict[str, Any]]``.

    Raises:
        ValueError: If ``order_by`` is not a known column name.
    """
    if order_by not in _ORDERABLE:
        raise ValueError(
            f"order_by must be one of {sorted(_ORDERABLE)}, got {order_by!r}"
        )
    cur.execute(
        f"SELECT * FROM subjects WHERE project_id = ? ORDER BY {order_by}",
        (project_id,),
    )
    rows = cur.fetchall()
    columns = [d[0] for d in cur.description]
    return [dict(zip(columns, row)) for row in rows]


def count_for_project(cur, project_id: int) -> int:
    """Return the number of subjects in a project.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        project_id: Project to count.

    Returns:
        Number of subject rows.
    """
    cur.execute(
        "SELECT COUNT(*) FROM subjects WHERE project_id = ?", (project_id,)
    )
    return int(cur.fetchone()[0])


def update(
    cur,
    subject_id: int,
    *,
    subject_code: str | None = None,
    sex: str | None = None,
    origin: str | None = None,
) -> bool:
    """Partial update of a subject row.

    Only kwargs with non-None values are written. ``project_id`` and
    ``created_at`` are intentionally NOT updatable here — moving a
    subject between projects is not a routine operation and would
    silently corrupt downstream lineage. Use raw SQL if you really
    need it.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        subject_id: Row to update.
        subject_code: New code (if not None).
        sex: New sex (if not None).
        origin: New origin (if not None).

    Returns:
        ``True`` iff exactly one row was updated. ``False`` (no SQL
        run) when every kwarg is None.
    """
    fields = {
        "subject_code": subject_code,
        "sex": sex,
        "origin": origin,
    }
    assignments = [(col, val) for col, val in fields.items() if val is not None]
    if not assignments:
        return False
    set_clause = ", ".join(f"{col} = ?" for col, _ in assignments)
    params = [val for _, val in assignments]
    params.append(subject_id)
    cur.execute(
        f"UPDATE subjects SET {set_clause} WHERE subject_id = ?", tuple(params)
    )
    return cur.rowcount > 0


def delete(cur, subject_id: int) -> bool:
    """Delete a subject.

    ``visits.fk_visits_subject`` is ``ON DELETE CASCADE``, so this also
    removes every visit, sample, and metadata row owned by the subject.
    ``sample_files`` uses ``ON DELETE RESTRICT`` and will block the
    delete instead — clean those up first.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        subject_id: Row to delete.

    Returns:
        ``True`` iff a row was removed.
    """
    cur.execute("DELETE FROM subjects WHERE subject_id = ?", (subject_id,))
    return cur.rowcount > 0


def exists(cur, subject_id: int) -> bool:
    """Return whether a subject with the given id exists.

    For natural-key existence checks use
    ``get_by_code(...) is not None``.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        subject_id: Id to check.

    Returns:
        ``True`` if a matching row exists.
    """
    cur.execute("SELECT 1 FROM subjects WHERE subject_id = ?", (subject_id,))
    return cur.fetchone() is not None
