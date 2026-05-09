"""CRUD wrapper for the `samples` table.

Same call style as the other table modules: cursor first, dict returns,
writes audit-logged via ``_LoggingCursor``.

``sample_name`` is **globally unique** (not scoped to a visit), so the
natural-key lookup is :func:`get_by_name` and :func:`get_or_create` keys
on ``sample_name`` alone.
"""

from __future__ import annotations

from typing import Any

import mariadb

_COLUMNS = (
    "sample_id",
    "visit_id",
    "sample_name",
    "sample_type",
    "SQR",
    "SQRP",
    "library",
    "antibody_class",
    "created_at",
)
_ORDERABLE = frozenset(_COLUMNS)


def _row_to_dict(cur, row) -> dict[str, Any]:
    return dict(zip([d[0] for d in cur.description], row))


def create(
    cur,
    visit_id: int,
    sample_name: str,
    sample_type: str,
    sqr: str,
    sqrp: str,
    library: str,
    *,
    antibody_class: str | None = None,
) -> int:
    """Insert a sample and return its new ``sample_id``.

    Raises ``mariadb.IntegrityError`` if *sample_name* already exists
    (global UNIQUE), or if *visit_id* doesn't reference an existing visit.
    The DB enforces ``sample_type IN ('sample','mockIP','input','anchor')``
    and will reject other values.
    """
    cur.execute(
        "INSERT INTO samples "
        "(visit_id, sample_name, sample_type, SQR, SQRP, library, antibody_class) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (visit_id, sample_name, sample_type, sqr, sqrp, library, antibody_class),
    )
    return cur.lastrowid


def get(cur, sample_id: int) -> dict[str, Any] | None:
    """Return the sample row for *sample_id*, or ``None`` if not found."""
    cur.execute("SELECT * FROM samples WHERE sample_id = ?", (sample_id,))
    row = cur.fetchone()
    return _row_to_dict(cur, row) if row is not None else None


def get_by_name(cur, sample_name: str) -> dict[str, Any] | None:
    """Return the sample row for *sample_name*, or ``None`` if not found."""
    cur.execute("SELECT * FROM samples WHERE sample_name = ?", (sample_name,))
    row = cur.fetchone()
    return _row_to_dict(cur, row) if row is not None else None


def get_or_create(
    cur,
    visit_id: int,
    sample_name: str,
    sample_type: str,
    sqr: str,
    sqrp: str,
    library: str,
    *,
    antibody_class: str | None = None,
) -> tuple[int, bool]:
    """Return ``(sample_id, created)``. Idempotent on ``sample_name``.

    Existing rows are returned as-is — the other columns are not used to
    update an existing row. Falls back to a re-fetch on the UNIQUE-violation
    race where another transaction inserted the same name in parallel.
    """
    existing = get_by_name(cur, sample_name)
    if existing is not None:
        return int(existing["sample_id"]), False
    try:
        new_id = create(
            cur,
            visit_id,
            sample_name,
            sample_type,
            sqr,
            sqrp,
            library,
            antibody_class=antibody_class,
        )
        return new_id, True
    except mariadb.IntegrityError:
        existing = get_by_name(cur, sample_name)
        if existing is None:
            raise
        return int(existing["sample_id"]), False


def list_for_visit(
    cur, visit_id: int, *, order_by: str = "sample_id"
) -> list[dict[str, Any]]:
    """Return all samples belonging to *visit_id*."""
    if order_by not in _ORDERABLE:
        raise ValueError(
            f"order_by must be one of {sorted(_ORDERABLE)}, got {order_by!r}"
        )
    cur.execute(
        f"SELECT * FROM samples WHERE visit_id = ? ORDER BY {order_by}",
        (visit_id,),
    )
    rows = cur.fetchall()
    columns = [d[0] for d in cur.description]
    return [dict(zip(columns, row)) for row in rows]


def count_for_visit(cur, visit_id: int) -> int:
    """Return the number of samples for *visit_id*."""
    cur.execute(
        "SELECT COUNT(*) FROM samples WHERE visit_id = ?", (visit_id,)
    )
    return int(cur.fetchone()[0])


def update(
    cur,
    sample_id: int,
    *,
    sample_name: str | None = None,
    sample_type: str | None = None,
    sqr: str | None = None,
    sqrp: str | None = None,
    library: str | None = None,
    antibody_class: str | None = None,
) -> bool:
    """Partial update: only kwargs with non-None values are written.

    ``visit_id`` and ``created_at`` are intentionally NOT updatable —
    re-parenting a sample would corrupt downstream lineage. Setting
    *antibody_class* to NULL is also out of scope (the helper treats None
    as "skip"); use raw SQL if you need that.

    Returns True iff one row was actually updated.
    """
    fields = {
        "sample_name": sample_name,
        "sample_type": sample_type,
        "SQR": sqr,
        "SQRP": sqrp,
        "library": library,
        "antibody_class": antibody_class,
    }
    assignments = [(col, val) for col, val in fields.items() if val is not None]
    if not assignments:
        return False
    set_clause = ", ".join(f"{col} = ?" for col, _ in assignments)
    params = [val for _, val in assignments]
    params.append(sample_id)
    cur.execute(
        f"UPDATE samples SET {set_clause} WHERE sample_id = ?", tuple(params)
    )
    return cur.rowcount > 0


def delete(cur, sample_id: int) -> bool:
    """Delete a sample. Returns True iff a row was removed.

    NOTE: ``sample_metadata.fk_sample_metadata_sample`` is ``ON DELETE
    CASCADE``, so this also removes every metadata row owned by the sample.
    ``sample_files.fk_sample_files_sample`` is ``ON DELETE RESTRICT`` and
    will block the delete instead — clean those up first.
    """
    cur.execute("DELETE FROM samples WHERE sample_id = ?", (sample_id,))
    return cur.rowcount > 0


def exists(
    cur,
    sample_id: int | None = None,
    *,
    name: str | None = None,
) -> bool:
    """Return True if a sample with the given id OR name exists.

    Exactly one of *sample_id* / *name* must be provided.
    """
    if (sample_id is None) == (name is None):
        raise ValueError("exists() requires exactly one of sample_id or name")
    if sample_id is not None:
        cur.execute("SELECT 1 FROM samples WHERE sample_id = ?", (sample_id,))
    else:
        cur.execute("SELECT 1 FROM samples WHERE sample_name = ?", (name,))
    return cur.fetchone() is not None
