"""CRUD wrapper for the `subjects` table.

Same call style as :mod:`dbmaria_utils.projects` — every function takes a
cursor first; callers wrap them in ``with transaction() as cur:``.

The natural key is the composite ``(project_id, subject_code)`` — a
``subject_code`` can repeat across projects, so most lookups go through
:func:`get_by_code` rather than :func:`get`.
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

    Raises ``mariadb.IntegrityError`` if (project_id, subject_code) already
    exists, or if *project_id* does not reference an existing project.
    The DB also enforces ``sex IN ('M', 'F')`` — invalid values raise as well.
    Use :func:`get_or_create` for idempotent inserts.
    """
    cur.execute(
        "INSERT INTO subjects (project_id, subject_code, sex, origin) "
        "VALUES (?, ?, ?, ?)",
        (project_id, subject_code, sex, origin),
    )
    return cur.lastrowid


def get(cur, subject_id: int) -> dict[str, Any] | None:
    """Return the subject row for *subject_id*, or ``None`` if not found."""
    cur.execute("SELECT * FROM subjects WHERE subject_id = ?", (subject_id,))
    row = cur.fetchone()
    return _row_to_dict(cur, row) if row is not None else None


def get_by_code(
    cur, project_id: int, subject_code: str
) -> dict[str, Any] | None:
    """Return the subject row for the composite natural key, or ``None``.

    Hot path for CSV importers: look up by (project_id, subject_code) before
    inserting.
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
    """Return ``(subject_id, created)``. Idempotent on (project_id, code).

    Existing rows are returned as-is — *sex* and *origin* are not used to
    update an existing row. Falls back to a re-fetch on the UNIQUE-violation
    race where another transaction inserted the same key in parallel.
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
    """Return all subjects belonging to *project_id*."""
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
    """Return the number of subjects in *project_id*."""
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
    """Partial update: only kwargs with non-None values are written.

    ``project_id`` and ``created_at`` are intentionally NOT updatable here —
    moving a subject between projects is not a routine operation and would
    silently corrupt downstream lineage. Use raw SQL if you really need it.

    Returns True iff one row was actually updated. Returns False (no SQL run)
    when every kwarg is None.
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
    """Delete a subject. Returns True iff a row was removed.

    NOTE: ``visits.fk_visits_subject`` is ``ON DELETE CASCADE``, so this
    also removes every visit, sample, and metadata row owned by the subject.
    Sample files use ``ON DELETE RESTRICT`` and will block the delete
    instead — clean those up first.
    """
    cur.execute("DELETE FROM subjects WHERE subject_id = ?", (subject_id,))
    return cur.rowcount > 0


def exists(cur, subject_id: int) -> bool:
    """Return True if a subject with the given id exists.

    For natural-key existence checks use ``get_by_code(...) is not None``.
    """
    cur.execute("SELECT 1 FROM subjects WHERE subject_id = ?", (subject_id,))
    return cur.fetchone() is not None
