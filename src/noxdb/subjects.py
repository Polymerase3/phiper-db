"""CRUD wrapper for the `subjects` table.

Same call style as [`noxdb.projects`][noxdb.projects] —
every function takes a cursor first; callers wrap them in
``with transaction() as cur:``.

``subject_code`` is **globally unique** — subjects carry no project
affiliation (project membership lives in ``project_samples``, reached
via the sample → visit → subject lineage). The natural-key lookup is
[`get_by_code`][noxdb.subjects.get_by_code] rather than
[`get`][noxdb.subjects.get].
"""

from __future__ import annotations

from typing import Any

import mariadb

_COLUMNS = (
    "subject_id",
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
    subject_code: str,
    sex: str | None,
    *,
    origin: str | None = None,
) -> int:
    """Insert a subject and return its new ``subject_id``.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        subject_code: Stable, globally unique subject code.
        sex: ``'M'``, ``'F'``, or ``None`` for controls without a
            known sex (DB-side CHECK allows NULL).
        origin: Optional free-text origin.

    Returns:
        The newly inserted ``subject_id``.

    Raises:
        mariadb.IntegrityError: If ``subject_code`` already exists
            (global UNIQUE), or ``sex`` is a non-null value outside
            ``('M', 'F')``. Use
            [`get_or_create`][noxdb.subjects.get_or_create]
            for idempotent inserts.
    """
    cur.execute(
        "INSERT INTO subjects (subject_code, sex, origin) "
        "VALUES (?, ?, ?)",
        (subject_code, sex if sex and sex.upper() not in ("NA", "N/A") else None, origin),
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


def get_by_code(cur, subject_code: str) -> dict[str, Any] | None:
    """Return the subject row for the globally-unique natural key.

    Hot path for CSV importers: look up by ``subject_code`` before
    inserting.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        subject_code: Globally unique subject code.

    Returns:
        The row as ``dict[str, Any]``, or ``None`` if not found.
    """
    cur.execute(
        "SELECT * FROM subjects WHERE subject_code = ?",
        (subject_code,),
    )
    row = cur.fetchone()
    return _row_to_dict(cur, row) if row is not None else None


def _norm_sex(sex: str | None) -> str | None:
    """Same NA→NULL normalization `create` applies, for conflict checks."""
    return sex if sex and sex.upper() not in ("NA", "N/A") else None


def _assert_no_conflict(existing: dict[str, Any], sex: str | None, origin: str | None) -> None:
    """Raise if a reused ``subject_code`` carries different attributes.

    ``subject_code`` is globally UNIQUE: a row with this code already
    exists. Reusing it is intentional for genuinely shared subjects,
    but a *different* sex/origin almost always means an accidental
    cross-study code collision silently merging two unrelated subjects.
    Fail loudly instead. Only compares when both the existing value and
    the incoming value are non-NULL (an incoming NULL asserts nothing;
    enriching a NULL existing value is out of scope for get_or_create).
    """
    incoming_sex = _norm_sex(sex)
    if (
        incoming_sex is not None
        and existing["sex"] is not None
        and existing["sex"] != incoming_sex
    ):
        raise ValueError(
            f"subject_code {existing['subject_code']!r} already exists with "
            f"sex={existing['sex']!r}; refusing to reuse it for "
            f"sex={incoming_sex!r} (likely a cross-study subject_code "
            "collision — subject_code is globally unique)"
        )
    if (
        origin is not None
        and existing["origin"] is not None
        and existing["origin"] != origin
    ):
        raise ValueError(
            f"subject_code {existing['subject_code']!r} already exists with "
            f"origin={existing['origin']!r}; refusing to reuse it for "
            f"origin={origin!r} (likely a cross-study subject_code collision)"
        )


def get_or_create(
    cur,
    subject_code: str,
    sex: str | None,
    *,
    origin: str | None = None,
) -> tuple[int, bool]:
    """Idempotently return the subject id, inserting if needed.

    Existing rows are returned as-is and never *updated* by this call.
    *sex* / *origin* are still checked against the existing row: a
    mismatch raises (see Raises) so an accidental cross-study
    ``subject_code`` collision fails loudly instead of silently
    merging two unrelated subjects. Falls back to a re-fetch on the
    UNIQUE-violation race where another transaction inserted the same
    key in parallel.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        subject_code: Globally unique subject code.
        sex: Used only on insert.
        origin: Used only on insert.

    Returns:
        ``(subject_id, created)`` where ``created`` is ``True`` iff this
        call inserted the row.

    Raises:
        ValueError: If a subject with this ``subject_code`` already
            exists but with a different non-NULL ``sex`` / ``origin``
            (a likely accidental cross-study collision — see
            :func:`_assert_no_conflict`).
        mariadb.IntegrityError: If the race-recovery fetch also misses.
    """
    existing = get_by_code(cur, subject_code)
    if existing is not None:
        _assert_no_conflict(existing, sex, origin)
        return int(existing["subject_id"]), False
    try:
        new_id = create(
            cur, subject_code, sex, origin=origin,
        )
        return new_id, True
    except mariadb.IntegrityError:
        existing = get_by_code(cur, subject_code)
        if existing is None:
            raise
        _assert_no_conflict(existing, sex, origin)
        return int(existing["subject_id"]), False


def list_for_project(
    cur, project_id: int, *, order_by: str = "subject_id"
) -> list[dict[str, Any]]:
    """Return all subjects with at least one sample in a project.

    Project membership lives in ``project_samples``; this traverses
    ``project_samples → samples → visits → subjects`` and de-duplicates,
    since a subject can have many samples in the same project.

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
        "SELECT DISTINCT sub.* FROM project_samples ps "
        "JOIN samples sm   ON sm.sample_id   = ps.sample_id "
        "JOIN visits v     ON v.visit_id     = sm.visit_id "
        "JOIN subjects sub ON sub.subject_id = v.subject_id "
        f"WHERE ps.project_id = ? ORDER BY sub.{order_by}",
        (project_id,),
    )
    rows = cur.fetchall()
    columns = [d[0] for d in cur.description]
    return [dict(zip(columns, row)) for row in rows]


def count_for_project(cur, project_id: int) -> int:
    """Return the number of distinct subjects with samples in a project.

    Traverses ``project_samples → samples → visits → subjects`` and
    counts distinct subjects (a subject can have many samples in the
    project).

    Args:
        cur: Audit-logging cursor from `transaction()`.
        project_id: Project to count.

    Returns:
        Number of distinct subject rows.
    """
    cur.execute(
        "SELECT COUNT(DISTINCT sub.subject_id) FROM project_samples ps "
        "JOIN samples sm   ON sm.sample_id   = ps.sample_id "
        "JOIN visits v     ON v.visit_id     = sm.visit_id "
        "JOIN subjects sub ON sub.subject_id = v.subject_id "
        "WHERE ps.project_id = ?",
        (project_id,),
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

    Only kwargs with non-None values are written. ``created_at`` is
    intentionally NOT updatable here. Use raw SQL if you really need
    it.

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
