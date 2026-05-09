"""CRUD wrapper for the `visits` table.

Same call style as the other table modules: cursor first, dict returns,
writes audit-logged via ``_LoggingCursor``.

Natural key is the composite ``(subject_id, timepoint)`` — but ``timepoint``
is nullable, and MariaDB's UNIQUE treats NULLs as distinct, so multiple
rows with ``timepoint IS NULL`` for the same subject are permitted by the
schema. :func:`get_or_create` rejects ``timepoint=None`` for that reason.
"""

from __future__ import annotations

from typing import Any

import mariadb

_COLUMNS = (
    "visit_id",
    "subject_id",
    "timepoint",
    "group_test",
    "age",
    "created_at",
)
_ORDERABLE = frozenset(_COLUMNS)


def _row_to_dict(cur, row) -> dict[str, Any]:
    return dict(zip([d[0] for d in cur.description], row))


def create(
    cur,
    subject_id: int,
    group_test: str,
    age: int,
    *,
    timepoint: str | None = None,
) -> int:
    """Insert a visit and return its new ``visit_id``.

    Raises ``mariadb.IntegrityError`` if (subject_id, timepoint) already
    exists for a non-null timepoint, or if *subject_id* doesn't reference
    an existing subject. The DB enforces ``age >= 0`` and will reject
    negative values.
    """
    cur.execute(
        "INSERT INTO visits (subject_id, timepoint, group_test, age) "
        "VALUES (?, ?, ?, ?)",
        (subject_id, timepoint, group_test, age),
    )
    return cur.lastrowid


def get(cur, visit_id: int) -> dict[str, Any] | None:
    """Return the visit row for *visit_id*, or ``None`` if not found."""
    cur.execute("SELECT * FROM visits WHERE visit_id = ?", (visit_id,))
    row = cur.fetchone()
    return _row_to_dict(cur, row) if row is not None else None


def get_by_subject_timepoint(
    cur, subject_id: int, timepoint: str | None
) -> dict[str, Any] | None:
    """Return the visit row for the natural key, or ``None``.

    When *timepoint* is ``None`` this matches via ``IS NULL``. NOTE: because
    NULL timepoints are not deduplicated by the UNIQUE, this lookup may
    match an arbitrary row among several NULL-timepoint visits for the same
    subject. Don't rely on it for idempotent inserts when timepoint is None.
    """
    if timepoint is None:
        cur.execute(
            "SELECT * FROM visits WHERE subject_id = ? AND timepoint IS NULL",
            (subject_id,),
        )
    else:
        cur.execute(
            "SELECT * FROM visits WHERE subject_id = ? AND timepoint = ?",
            (subject_id, timepoint),
        )
    row = cur.fetchone()
    return _row_to_dict(cur, row) if row is not None else None


def get_or_create(
    cur,
    subject_id: int,
    timepoint: str,
    group_test: str,
    age: int,
) -> tuple[int, bool]:
    """Return ``(visit_id, created)``. Idempotent on (subject_id, timepoint).

    *timepoint* must not be ``None`` — the UNIQUE constraint does not
    deduplicate NULL timepoints, so an idempotent contract is impossible
    there. Raises ``ValueError`` if you pass ``None``.

    Existing rows are returned as-is; *group_test* and *age* are not used
    to update an existing row.
    """
    if timepoint is None:
        raise ValueError(
            "get_or_create requires a non-null timepoint; the (subject_id, "
            "timepoint) UNIQUE does not deduplicate NULL timepoints."
        )
    existing = get_by_subject_timepoint(cur, subject_id, timepoint)
    if existing is not None:
        return int(existing["visit_id"]), False
    try:
        new_id = create(
            cur, subject_id, group_test, age, timepoint=timepoint,
        )
        return new_id, True
    except mariadb.IntegrityError:
        existing = get_by_subject_timepoint(cur, subject_id, timepoint)
        if existing is None:
            raise
        return int(existing["visit_id"]), False


def list_for_subject(
    cur, subject_id: int, *, order_by: str = "visit_id"
) -> list[dict[str, Any]]:
    """Return all visits belonging to *subject_id*."""
    if order_by not in _ORDERABLE:
        raise ValueError(
            f"order_by must be one of {sorted(_ORDERABLE)}, got {order_by!r}"
        )
    cur.execute(
        f"SELECT * FROM visits WHERE subject_id = ? ORDER BY {order_by}",
        (subject_id,),
    )
    rows = cur.fetchall()
    columns = [d[0] for d in cur.description]
    return [dict(zip(columns, row)) for row in rows]


def count_for_subject(cur, subject_id: int) -> int:
    """Return the number of visits for *subject_id*."""
    cur.execute(
        "SELECT COUNT(*) FROM visits WHERE subject_id = ?", (subject_id,)
    )
    return int(cur.fetchone()[0])


def update(
    cur,
    visit_id: int,
    *,
    timepoint: str | None = None,
    group_test: str | None = None,
    age: int | None = None,
) -> bool:
    """Partial update: only kwargs with non-None values are written.

    ``subject_id`` and ``created_at`` are intentionally NOT updatable —
    re-parenting a visit would corrupt downstream lineage. Setting
    *timepoint* to NULL is also out of scope (the helper treats None as
    "skip"); use raw SQL if you need that.

    Returns True iff one row was actually updated.
    """
    fields = {
        "timepoint": timepoint,
        "group_test": group_test,
        "age": age,
    }
    assignments = [(col, val) for col, val in fields.items() if val is not None]
    if not assignments:
        return False
    set_clause = ", ".join(f"{col} = ?" for col, _ in assignments)
    params = [val for _, val in assignments]
    params.append(visit_id)
    cur.execute(
        f"UPDATE visits SET {set_clause} WHERE visit_id = ?", tuple(params)
    )
    return cur.rowcount > 0


def delete(cur, visit_id: int) -> bool:
    """Delete a visit. Returns True iff a row was removed.

    NOTE: ``samples.fk_samples_visit`` and ``visit_metadata.fk_visit_metadata_visit``
    are ``ON DELETE CASCADE``, so this also removes every sample (and its
    metadata) and every visit_metadata row owned by the visit. Sample files
    use ``ON DELETE RESTRICT`` and will block the delete instead.
    """
    cur.execute("DELETE FROM visits WHERE visit_id = ?", (visit_id,))
    return cur.rowcount > 0


def exists(cur, visit_id: int) -> bool:
    """Return True if a visit with the given id exists.

    For natural-key existence checks use
    ``get_by_subject_timepoint(...) is not None``.
    """
    cur.execute("SELECT 1 FROM visits WHERE visit_id = ?", (visit_id,))
    return cur.fetchone() is not None
