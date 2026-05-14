"""Registration wrapper for the `sample_files` table.

Unlike the other CRUD modules, ``files.register`` doubles as a *gate*
between application code and the schema: it stats the file on disk,
validates that the path lives in the right storage tier, and only then
hands the row to MariaDB. The schema's CHECKs (absolute path, MD5 format,
UNIQUE path) are still in place but are now a backstop, not the first
line of defense.

Storage tier policy
-------------------
File-type → tier is fixed by lab convention:

    fastq_r1 / fastq_r2 / fastq_single / bam / counts  -> archive
    beer_norm / zigp_norm / edger_norm                 -> work

Roots are configurable via env vars (defaults shown):

    LABDB_ARCHIVE_ROOT  default /lisc/archive
    LABDB_WORK_ROOT     default /lisc/work

Callers can override ``storage_tier`` to ``'scratch'`` or ``'external'``
(escape hatches with no path-prefix check); overriding to swap
``archive``/``work`` against the type-derived value is rejected.
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Any

import mariadb

_ARCHIVE_TYPES = frozenset(
    {"fastq_r1", "fastq_r2", "fastq_single", "bam", "counts"}
)
_WORK_TYPES = frozenset({"beer_norm", "zigp_norm", "edger_norm"})
_ALL_TYPES = _ARCHIVE_TYPES | _WORK_TYPES
_ALL_TIERS = frozenset({"archive", "work", "scratch", "external"})

_FASTQ_TYPES = frozenset({"fastq_r1", "fastq_r2", "fastq_single"})
_FASTQ_EXTS = (".fastq", ".fastq.gz", ".fq", ".fq.gz")

_MD5_RE = re.compile(r"^[a-f0-9]{32}$")
_MD5_CHUNK = 8 * 1024 * 1024  # 8 MiB

_COLUMNS = (
    "file_id",
    "sample_id",
    "file_type",
    "file_path",
    "file_size_bytes",
    "checksum_md5",
    "storage_tier",
    "created_at",
)
_ORDERABLE = frozenset(_COLUMNS)


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #

def _archive_root() -> str:
    return os.environ.get("LABDB_ARCHIVE_ROOT", "/lisc/archive")


def _work_root() -> str:
    return os.environ.get("LABDB_WORK_ROOT", "/lisc/work")


def _expected_tier(file_type: str) -> str:
    if file_type in _ARCHIVE_TYPES:
        return "archive"
    if file_type in _WORK_TYPES:
        return "work"
    raise ValueError(
        f"Unknown file_type {file_type!r}; must be one of {sorted(_ALL_TYPES)}"
    )


def _validate_extension(path: str, file_type: str) -> None:
    """Strict for fastq and bam; other types not validated."""
    lower = path.lower()
    if file_type in _FASTQ_TYPES:
        if not lower.endswith(_FASTQ_EXTS):
            raise ValueError(
                f"file_type {file_type!r} requires extension in {_FASTQ_EXTS}, "
                f"got {path!r}"
            )
        return
    if file_type == "bam":
        if not lower.endswith(".bam"):
            raise ValueError(
                f"file_type 'bam' requires extension .bam, got {path!r}"
            )
        return
    # counts and *_norm: extension not validated.


def _is_under(path: str, root: str) -> bool:
    """True iff *path* lives inside *root* AFTER resolving symlinks.

    Uses ``realpath`` on both sides so a symlink under the tier root that
    points outside it (e.g. ``/lisc/archive/link.bam`` -> ``/etc/passwd``)
    is rejected. ``commonpath`` is used instead of ``startswith`` so a
    sibling with the same string prefix (``/lisc/archive_old``) is also
    rejected.
    """
    try:
        resolved_root = os.path.realpath(root)
        resolved_path = os.path.realpath(path)
        return os.path.commonpath([resolved_path, resolved_root]) == resolved_root
    except ValueError:
        # commonpath raises ValueError on mixed drive letters (Windows) etc.
        return False


def _validate_tier_path(path: str, tier: str, *, archive_root: str, work_root: str) -> None:
    if tier == "archive":
        if not _is_under(path, archive_root):
            raise ValueError(
                f"archive-tier path must live under {archive_root!r}, got {path!r}"
            )
    elif tier == "work":
        if not _is_under(path, work_root):
            raise ValueError(
                f"work-tier path must live under {work_root!r}, got {path!r}"
            )
    # scratch / external: no prefix check.


def _validate_md5(s: str) -> None:
    if not _MD5_RE.match(s):
        raise ValueError(
            f"checksum_md5 must be 32 lowercase hex chars, got {s!r}"
        )


def _compute_md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_MD5_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _stat_regular_file(path: str) -> os.stat_result:
    """Stat *path*, raising structured errors for the common failures."""
    if not os.path.isabs(path):
        raise ValueError(f"file_path must be absolute, got {path!r}")
    try:
        st = os.stat(path)  # follows symlinks
    except FileNotFoundError:
        raise FileNotFoundError(f"file does not exist: {path!r}") from None
    if os.path.isdir(path):
        raise IsADirectoryError(f"file_path is a directory: {path!r}")
    import stat as _stat
    if not _stat.S_ISREG(st.st_mode):
        raise ValueError(f"file_path is not a regular file: {path!r}")
    return st


def _resolve_tier(file_type: str, override: str | None) -> str:
    expected = _expected_tier(file_type)
    if override is None:
        return expected
    if override not in _ALL_TIERS:
        raise ValueError(
            f"storage_tier must be one of {sorted(_ALL_TIERS)}, got {override!r}"
        )
    return override


def _inspect_file(
    file_path: str,
    file_type: str,
    *,
    compute_md5: bool,
    checksum_md5: str | None,
    storage_tier: str | None,
    skip_disk_check: bool = False,
) -> dict[str, Any]:
    """Run all filesystem and policy checks. Returns the row to insert
    minus ``sample_id``. Raises before any SQL is touched.

    When ``skip_disk_check`` is ``True``, path-prefix and on-disk checks
    (stat, md5) are skipped; ``file_size_bytes`` is stored as ``None``.
    """
    if compute_md5 and checksum_md5 is not None:
        raise ValueError(
            "pass either compute_md5=True or checksum_md5=..., not both"
        )
    if file_type not in _ALL_TYPES:
        raise ValueError(
            f"Unknown file_type {file_type!r}; must be one of {sorted(_ALL_TYPES)}"
        )
    _validate_extension(file_path, file_type)
    tier = _resolve_tier(file_type, storage_tier)
    size: int | None
    md5: str | None
    if skip_disk_check:
        size = None
        md5 = checksum_md5
        if md5 is not None:
            _validate_md5(md5)
    else:
        _validate_tier_path(
            file_path, tier,
            archive_root=_archive_root(),
            work_root=_work_root(),
        )
        st = _stat_regular_file(file_path)
        size = int(st.st_size)
        if checksum_md5 is not None:
            _validate_md5(checksum_md5)
            md5 = checksum_md5
        elif compute_md5:
            md5 = _compute_md5(file_path)
        else:
            md5 = None
    return {
        "file_type": file_type,
        "file_path": file_path,
        "file_size_bytes": size,
        "checksum_md5": md5,
        "storage_tier": tier,
    }


def _row_to_dict(cur, row) -> dict[str, Any]:
    return dict(zip([d[0] for d in cur.description], row))


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def register(
    cur,
    sample_id: int,
    file_path: str,
    file_type: str,
    *,
    compute_md5: bool = False,
    checksum_md5: str | None = None,
    storage_tier: str | None = None,
    skip_disk_check: bool = False,
) -> int:
    """Validate a file on disk and insert a `sample_files` row.

    Filesystem checks (path is absolute, regular file exists, extension
    matches ``file_type``, path lives under the tier root via realpath)
    run **before** any SQL is executed, so a failure leaves the
    transaction untouched.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        sample_id: Parent sample. Must already exist.
        file_path: Absolute path on disk.
        file_type: One of the known types (`fastq_r1`, `fastq_r2`,
            `fastq_single`, `bam`, `counts`, `beer_norm`, `zigp_norm`,
            `edger_norm`).
        compute_md5: If ``True``, hash the file. Mutually exclusive with
            ``checksum_md5``.
        checksum_md5: Caller-supplied 32-char lowercase-hex MD5.
        storage_tier: Override the file-type-derived tier. Only
            ``'scratch'`` and ``'external'`` are accepted as overrides;
            flipping ``archive`` ↔ ``work`` is rejected.

    Returns:
        The newly inserted ``file_id``.

    Raises:
        ValueError: Relative path, unknown ``file_type``, mismatched
            extension or tier, malformed ``checksum_md5``, or both
            ``compute_md5`` and ``checksum_md5`` set.
        FileNotFoundError: If the path does not exist.
        IsADirectoryError: If the path is a directory.
        mariadb.IntegrityError: Unknown ``sample_id`` (FK violation) or
            duplicate ``file_path`` (global UNIQUE).
    """
    row = _inspect_file(
        file_path, file_type,
        compute_md5=compute_md5,
        checksum_md5=checksum_md5,
        storage_tier=storage_tier,
        skip_disk_check=skip_disk_check,
    )
    cur.execute(
        "INSERT INTO sample_files "
        "(sample_id, file_type, file_path, file_size_bytes, checksum_md5, "
        "storage_tier) VALUES (?, ?, ?, ?, ?, ?)",
        (
            sample_id,
            row["file_type"],
            row["file_path"],
            row["file_size_bytes"],
            row["checksum_md5"],
            row["storage_tier"],
        ),
    )
    return cur.lastrowid


def get_or_register(
    cur,
    sample_id: int,
    file_path: str,
    file_type: str,
    *,
    compute_md5: bool = False,
    checksum_md5: str | None = None,
    storage_tier: str | None = None,
    skip_disk_check: bool = False,
) -> tuple[int, bool]:
    """Idempotently register a file. Returns ``(file_id, registered)``.

    If a row with this ``file_path`` already exists, it is returned
    as-is — the file is NOT re-stat'd and the other arguments are not
    used to update the existing row. This means a stale path that was
    registered in the past keeps returning its id even if the file has
    since been deleted; call
    [`restat`][dbmaria_utils.files.restat] if you need to refresh it.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        sample_id: Parent sample (used only on insert).
        file_path: Absolute path on disk. Globally unique.
        file_type: See [`register`][dbmaria_utils.files.register].
        compute_md5: Used only on insert.
        checksum_md5: Used only on insert.
        storage_tier: Used only on insert.

    Returns:
        ``(file_id, registered)`` where ``registered`` is ``True`` iff
        this call inserted the row.

    Raises:
        mariadb.IntegrityError: If the race-recovery fetch also misses.
        Plus everything [`register`][dbmaria_utils.files.register] raises
        on insert.
    """
    existing = get_by_path(cur, file_path)
    if existing is not None:
        return int(existing["file_id"]), False
    try:
        new_id = register(
            cur, sample_id, file_path, file_type,
            compute_md5=compute_md5,
            checksum_md5=checksum_md5,
            storage_tier=storage_tier,
            skip_disk_check=skip_disk_check,
        )
    except mariadb.IntegrityError:
        existing = get_by_path(cur, file_path)
        if existing is None:
            raise
        return int(existing["file_id"]), False
    return new_id, True


def get(cur, file_id: int) -> dict[str, Any] | None:
    """Return the file row for a given id.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        file_id: Primary key to look up.

    Returns:
        The row as ``dict[str, Any]``, or ``None`` if not found.
    """
    cur.execute("SELECT * FROM sample_files WHERE file_id = ?", (file_id,))
    row = cur.fetchone()
    return _row_to_dict(cur, row) if row is not None else None


def get_by_path(cur, file_path: str) -> dict[str, Any] | None:
    """Return the file row for a given path.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        file_path: Absolute path on disk.

    Returns:
        The row as ``dict[str, Any]``, or ``None`` if not found.
    """
    cur.execute("SELECT * FROM sample_files WHERE file_path = ?", (file_path,))
    row = cur.fetchone()
    return _row_to_dict(cur, row) if row is not None else None


def list_for_sample(
    cur, sample_id: int, *, order_by: str = "file_id"
) -> list[dict[str, Any]]:
    """Return all ``sample_files`` rows for a sample.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        sample_id: Sample to list.
        order_by: Column name to order by. Must be a column of ``sample_files``.

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
        f"SELECT * FROM sample_files WHERE sample_id = ? ORDER BY {order_by}",
        (sample_id,),
    )
    rows = cur.fetchall()
    columns = [d[0] for d in cur.description]
    return [dict(zip(columns, row)) for row in rows]


def count_for_sample(cur, sample_id: int) -> int:
    """Return the number of files registered for a sample.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        sample_id: Sample to count.

    Returns:
        Number of ``sample_files`` rows.
    """
    cur.execute(
        "SELECT COUNT(*) FROM sample_files WHERE sample_id = ?", (sample_id,)
    )
    return int(cur.fetchone()[0])


def update(
    cur,
    file_id: int,
    *,
    file_size_bytes: int | None = None,
    checksum_md5: str | None = None,
    storage_tier: str | None = None,
) -> bool:
    """Partial update of a file row.

    Only kwargs with non-None values are written. ``file_path``,
    ``file_type``, ``sample_id``, and ``created_at`` are NOT updatable
    — those describe a different file. Use
    [`restat`][dbmaria_utils.files.restat] to refresh size/checksum from
    disk after a file is rewritten in place.

    Updating ``storage_tier`` enforces the same `file_type → tier`
    invariant as [`register`][dbmaria_utils.files.register]: flipping
    ``archive`` ↔ ``work`` is rejected; ``scratch`` / ``external``
    overrides are allowed.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        file_id: Row to update.
        file_size_bytes: New size (if not None).
        checksum_md5: New 32-char lowercase-hex MD5 (if not None).
        storage_tier: New tier (if not None).

    Returns:
        ``True`` iff exactly one row was updated.

    Raises:
        ValueError: Malformed ``checksum_md5``, unknown
            ``storage_tier``, or a tier override that violates the
            file-type invariant.
    """
    if checksum_md5 is not None:
        _validate_md5(checksum_md5)
    if storage_tier is not None:
        if storage_tier not in _ALL_TIERS:
            raise ValueError(
                f"storage_tier must be one of {sorted(_ALL_TIERS)}, got {storage_tier!r}"
            )
        # Enforce the same file_type -> tier invariant as register():
        # callers may move a file to scratch/external, but cannot flip
        # archive <-> work for a given file_type.
        cur.execute(
            "SELECT file_type FROM sample_files WHERE file_id = ?", (file_id,)
        )
        row = cur.fetchone()
        if row is not None:
            _resolve_tier(row[0], storage_tier)
    fields = {
        "file_size_bytes": file_size_bytes,
        "checksum_md5": checksum_md5,
        "storage_tier": storage_tier,
    }
    assignments = [(col, val) for col, val in fields.items() if val is not None]
    if not assignments:
        return False
    set_clause = ", ".join(f"{col} = ?" for col, _ in assignments)
    params = [val for _, val in assignments]
    params.append(file_id)
    cur.execute(
        f"UPDATE sample_files SET {set_clause} WHERE file_id = ?", tuple(params)
    )
    return cur.rowcount > 0


def restat(cur, file_id: int, *, compute_md5: bool = False) -> bool:
    """Re-read size (and optionally md5) from disk for an existing row.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        file_id: Row to refresh.
        compute_md5: If ``True``, recompute the MD5. Otherwise the
            existing checksum is kept.

    Returns:
        ``True`` iff the row's columns actually changed.

    Raises:
        FileNotFoundError: If the path no longer resolves. No SQL runs
            in that case.
    """
    row = get(cur, file_id)
    if row is None:
        return False
    st = _stat_regular_file(row["file_path"])
    new_size = int(st.st_size)
    new_md5 = _compute_md5(row["file_path"]) if compute_md5 else row["checksum_md5"]
    return update(
        cur, file_id,
        file_size_bytes=new_size,
        checksum_md5=new_md5,
    )


def delete(cur, file_id: int) -> bool:
    """Delete a file row.

    Only removes the database record. The file on disk is untouched —
    clean it up separately.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        file_id: Row to delete.

    Returns:
        ``True`` iff a row was removed.
    """
    cur.execute("DELETE FROM sample_files WHERE file_id = ?", (file_id,))
    return cur.rowcount > 0


def exists(
    cur,
    file_id: int | None = None,
    *,
    path: str | None = None,
) -> bool:
    """Return whether a file with the given id or path exists.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        file_id: Id to check (exclusive with ``path``).
        path: Path to check (exclusive with ``file_id``).

    Returns:
        ``True`` if a matching row exists.

    Raises:
        ValueError: If both or neither of ``file_id`` / ``path`` is given.
    """
    if (file_id is None) == (path is None):
        raise ValueError("exists() requires exactly one of file_id or path")
    if file_id is not None:
        cur.execute("SELECT 1 FROM sample_files WHERE file_id = ?", (file_id,))
    else:
        cur.execute("SELECT 1 FROM sample_files WHERE file_path = ?", (path,))
    return cur.fetchone() is not None
