"""CRUD wrapper for the `samples` table.

Same call style as the other table modules: cursor first, dict returns,
writes audit-logged via ``_LoggingCursor``.

``sample_name`` is **globally unique** (not scoped to a visit), so the
natural-key lookup is [`get_by_name`][noxdb.samples.get_by_name]
and [`get_or_create`][noxdb.samples.get_or_create] keys on
``sample_name`` alone.
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


# SQR/SQRP "absent" sentinels collapsed to a single canonical empty
# string. Plate identifiers are matched by exact string equality
# (control auto-link, queries, migration backfill), so formatting drift
# silently breaks linking unless every write goes through one canonical
# form. Padding (e.g. "01") is intentionally preserved — it is the
# established canonical shape in this dataset, not noise.
_PLATE_NULLISH = frozenset({"", "na", "n/a"})


def canonical_plate_id(value: str | None) -> str:
    """Return the canonical form of an SQR / SQRP plate identifier.

    Strips surrounding whitespace and collapses the "absent" sentinels
    (``""``, ``"NA"``, ``"N/A"``, case-insensitive) to a single
    canonical empty string. Any other value is returned stripped but
    otherwise verbatim — zero-padding is left intact because it *is*
    the canonical shape here.

    This is the one normalization chokepoint for plate identifiers;
    [`create`][noxdb.samples.create] / [`update`][noxdb.samples.update]
    call it on every write and the importer reuses it so the value the
    DB stores is always canonical and SQR+SQRP matching is reliable.

    Args:
        value: Raw SQR / SQRP cell (may be ``None``).

    Returns:
        The canonical identifier (possibly ``""`` for "no plate").
    """
    s = (value or "").strip()
    return "" if s.lower() in _PLATE_NULLISH else s


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

    Args:
        cur: Audit-logging cursor from `transaction()`.
        visit_id: Parent visit. Must already exist.
        sample_name: Globally unique sample name.
        sample_type: One of ``'sample'``, ``'mockIP'``, ``'input'``,
            ``'anchor'``, ``'NC'`` (DB-side `ENUM` constraint).
        sqr: SQR plate identifier. Canonicalized via
            [`canonical_plate_id`][noxdb.samples.canonical_plate_id]
            before storage.
        sqrp: SQRP plate identifier. Canonicalized like ``sqr``.
        library: Library identifier.
        antibody_class: Optional antibody class label.

    Returns:
        The newly inserted ``sample_id``.

    Raises:
        mariadb.IntegrityError: If ``sample_name`` already exists
            (global UNIQUE), ``visit_id`` does not reference an
            existing visit, or ``sample_type`` is outside the allowed
            enum.
    """
    cur.execute(
        "INSERT INTO samples "
        "(visit_id, sample_name, sample_type, SQR, SQRP, library, antibody_class) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            visit_id, sample_name, sample_type,
            canonical_plate_id(sqr), canonical_plate_id(sqrp),
            library, antibody_class,
        ),
    )
    return cur.lastrowid


def get(cur, sample_id: int) -> dict[str, Any] | None:
    """Return the sample row for a given id.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        sample_id: Primary key to look up.

    Returns:
        The row as ``dict[str, Any]``, or ``None`` if not found.
    """
    cur.execute("SELECT * FROM samples WHERE sample_id = ?", (sample_id,))
    row = cur.fetchone()
    return _row_to_dict(cur, row) if row is not None else None


def get_by_name(cur, sample_name: str) -> dict[str, Any] | None:
    """Return the sample row for a given name.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        sample_name: Globally unique sample name.

    Returns:
        The row as ``dict[str, Any]``, or ``None`` if not found.
    """
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
    """Idempotently return the sample id, inserting if needed.

    Existing rows are returned as-is — the other columns are not used
    to update an existing row. Falls back to a re-fetch on the
    UNIQUE-violation race.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        visit_id: Parent visit (used only on insert).
        sample_name: Globally unique sample name.
        sample_type: Used only on insert. See
            [`create`][noxdb.samples.create] for allowed values.
        sqr: Used only on insert.
        sqrp: Used only on insert.
        library: Used only on insert.
        antibody_class: Used only on insert.

    Returns:
        ``(sample_id, created)`` where ``created`` is ``True`` iff this
        call inserted the row.

    Raises:
        mariadb.IntegrityError: If the race-recovery fetch also misses.
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


def link_to_project(cur, project_id: int, sample_id: int) -> None:
    """Register ``sample_id`` under ``project_id`` in ``project_samples``.

    Idempotent: ``INSERT IGNORE`` so re-linking an already-linked
    (project, sample) pair is a no-op. ``project_samples`` is the sole
    source of truth for which samples belong to which project — a
    sample may be linked to several projects (e.g. plate controls
    shared across studies).

    Args:
        cur: Audit-logging cursor from `transaction()`.
        project_id: Project to link the sample to. Must already exist.
        sample_id: Sample to link. Must already exist.
    """
    cur.execute(
        "INSERT IGNORE INTO project_samples (project_id, sample_id) "
        "VALUES (?, ?)",
        (project_id, sample_id),
    )


def list_for_visit(
    cur, visit_id: int, *, order_by: str = "sample_id"
) -> list[dict[str, Any]]:
    """Return all samples belonging to a visit.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        visit_id: Visit to list.
        order_by: Column name to order by. Must be a column of ``samples``.

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
        f"SELECT * FROM samples WHERE visit_id = ? ORDER BY {order_by}",
        (visit_id,),
    )
    rows = cur.fetchall()
    columns = [d[0] for d in cur.description]
    return [dict(zip(columns, row)) for row in rows]


def count_for_visit(cur, visit_id: int) -> int:
    """Return the number of samples for a visit.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        visit_id: Visit to count.

    Returns:
        Number of sample rows.
    """
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
    """Partial update of a sample row.

    Only kwargs with non-None values are written. ``visit_id`` and
    ``created_at`` are intentionally NOT updatable — re-parenting a
    sample would corrupt downstream lineage. Setting *antibody_class*
    to NULL is also out of scope (the helper treats None as "skip");
    use raw SQL if you need that.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        sample_id: Row to update.
        sample_name: New name (if not None).
        sample_type: New type (if not None). See
            [`create`][noxdb.samples.create] for allowed values.
        sqr: New SQR (if not None). Canonicalized via
            [`canonical_plate_id`][noxdb.samples.canonical_plate_id].
        sqrp: New SQRP (if not None). Canonicalized like ``sqr``.
        library: New library (if not None).
        antibody_class: New antibody class (if not None).

    Returns:
        ``True`` iff exactly one row was updated.
    """
    fields = {
        "sample_name": sample_name,
        "sample_type": sample_type,
        "SQR": canonical_plate_id(sqr) if sqr is not None else None,
        "SQRP": canonical_plate_id(sqrp) if sqrp is not None else None,
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
    """Delete a sample.

    ``sample_metadata.fk_sample_metadata_sample`` is ``ON DELETE
    CASCADE``, so this also removes every metadata row owned by the
    sample. ``sample_files.fk_sample_files_sample`` is ``ON DELETE
    RESTRICT`` and will block the delete instead — clean those up
    first.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        sample_id: Row to delete.

    Returns:
        ``True`` iff a row was removed.
    """
    cur.execute("DELETE FROM samples WHERE sample_id = ?", (sample_id,))
    return cur.rowcount > 0


def exists(
    cur,
    sample_id: int | None = None,
    *,
    name: str | None = None,
) -> bool:
    """Return whether a sample with the given id or name exists.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        sample_id: Id to check (exclusive with ``name``).
        name: Name to check (exclusive with ``sample_id``).

    Returns:
        ``True`` if a matching row exists.

    Raises:
        ValueError: If both or neither of ``sample_id`` / ``name`` is given.
    """
    if (sample_id is None) == (name is None):
        raise ValueError("exists() requires exactly one of sample_id or name")
    if sample_id is not None:
        cur.execute("SELECT 1 FROM samples WHERE sample_id = ?", (sample_id,))
    else:
        cur.execute("SELECT 1 FROM samples WHERE sample_name = ?", (name,))
    return cur.fetchone() is not None
