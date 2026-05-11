"""CRUD wrapper for the `visits` table.

Same call style as the other table modules: cursor first, dict returns,
writes audit-logged via ``_LoggingCursor``.

Natural key is the composite ``(subject_id, timepoint)`` — but
``timepoint`` is nullable, and MariaDB's UNIQUE treats NULLs as
distinct, so multiple rows with ``timepoint IS NULL`` for the same
subject are permitted by the schema.
[`get_or_create`][dbmaria_utils.visits.get_or_create] rejects
``timepoint=None`` for that reason.
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

    Args:
        cur: Audit-logging cursor from `transaction()`.
        subject_id: Parent subject. Must already exist.
        group_test: Group/test label for this visit.
        age: Subject age at visit. The DB enforces ``age >= 0``.
        timepoint: Optional timepoint string; nullable.

    Returns:
        The newly inserted ``visit_id``.

    Raises:
        mariadb.IntegrityError: If ``(subject_id, timepoint)`` already
            exists for a non-null timepoint, ``subject_id`` does not
            reference an existing subject, or ``age`` is negative.
    """
    cur.execute(
        "INSERT INTO visits (subject_id, timepoint, group_test, age) "
        "VALUES (?, ?, ?, ?)",
        (subject_id, timepoint, group_test, age),
    )
    return cur.lastrowid


def get(cur, visit_id: int) -> dict[str, Any] | None:
    """Return the visit row for a given id.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        visit_id: Primary key to look up.

    Returns:
        The row as ``dict[str, Any]``, or ``None`` if not found.
    """
    cur.execute("SELECT * FROM visits WHERE visit_id = ?", (visit_id,))
    row = cur.fetchone()
    return _row_to_dict(cur, row) if row is not None else None


def get_by_subject_timepoint(
    cur, subject_id: int, timepoint: str | None
) -> dict[str, Any] | None:
    """Return the visit row for the natural key.

    When *timepoint* is ``None`` this matches via ``IS NULL``. Because
    NULL timepoints are not deduplicated by the UNIQUE, this lookup may
    match an arbitrary row among several NULL-timepoint visits for the
    same subject. Don't rely on it for idempotent inserts when timepoint
    is None.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        subject_id: Parent subject.
        timepoint: Timepoint string, or ``None`` to match ``IS NULL``.

    Returns:
        The row as ``dict[str, Any]``, or ``None`` if not found.
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
    """Idempotently return the visit id, inserting if needed.

    Existing rows are returned as-is; *group_test* and *age* are not
    used to update an existing row. Falls back to a re-fetch on the
    UNIQUE-violation race.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        subject_id: Parent subject.
        timepoint: Timepoint string. Must not be ``None`` — the
            ``(subject_id, timepoint)`` UNIQUE does not deduplicate NULL
            timepoints, so an idempotent contract is impossible there.
        group_test: Used only on insert.
        age: Used only on insert.

    Returns:
        ``(visit_id, created)`` where ``created`` is ``True`` iff this
        call inserted the row.

    Raises:
        ValueError: If ``timepoint`` is ``None``.
        mariadb.IntegrityError: If the race-recovery fetch also misses.
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
    """Return all visits belonging to a subject.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        subject_id: Subject to list.
        order_by: Column name to order by. Must be a column of ``visits``.

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
        f"SELECT * FROM visits WHERE subject_id = ? ORDER BY {order_by}",
        (subject_id,),
    )
    rows = cur.fetchall()
    columns = [d[0] for d in cur.description]
    return [dict(zip(columns, row)) for row in rows]


def count_for_subject(cur, subject_id: int) -> int:
    """Return the number of visits for a subject.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        subject_id: Subject to count.

    Returns:
        Number of visit rows.
    """
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
    """Partial update of a visit row.

    Only kwargs with non-None values are written. ``subject_id`` and
    ``created_at`` are intentionally NOT updatable — re-parenting a
    visit would corrupt downstream lineage. Setting *timepoint* to NULL
    is also out of scope (the helper treats None as "skip"); use raw
    SQL if you need that.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        visit_id: Row to update.
        timepoint: New timepoint (if not None).
        group_test: New group/test label (if not None).
        age: New age (if not None).

    Returns:
        ``True`` iff exactly one row was updated.
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
    """Delete a visit.

    ``samples.fk_samples_visit`` and
    ``visit_metadata.fk_visit_metadata_visit`` are ``ON DELETE
    CASCADE``, so this also removes every sample (and its metadata) and
    every visit_metadata row owned by the visit. ``sample_files`` uses
    ``ON DELETE RESTRICT`` and will block the delete instead.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        visit_id: Row to delete.

    Returns:
        ``True`` iff a row was removed.
    """
    cur.execute("DELETE FROM visits WHERE visit_id = ?", (visit_id,))
    return cur.rowcount > 0


def exists(cur, visit_id: int) -> bool:
    """Return whether a visit with the given id exists.

    For natural-key existence checks use
    ``get_by_subject_timepoint(...) is not None``.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        visit_id: Id to check.

    Returns:
        ``True`` if a matching row exists.
    """
    cur.execute("SELECT 1 FROM visits WHERE visit_id = ?", (visit_id,))
    return cur.fetchone() is not None
