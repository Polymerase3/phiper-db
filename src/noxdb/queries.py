"""Composite read-only queries and reports.

These helpers join the hierarchy (``projects → subjects → visits → samples``)
and pivot the EAV metadata tables into wide-form tables suitable for
notebook analysis. Everything here is **read-only**: nothing in this module
issues INSERT/UPDATE/DELETE.

All functions take a cursor as their first argument, so callers control
the transaction boundary just like with the CRUD modules:

    from noxdb import queries, transaction

    with transaction() as cur:
        df = queries.samples_with_metadata(cur, project_id=1)

DataFrame-returning helpers import :mod:`pandas` lazily — pandas is an
optional dependency declared under the ``analysis`` extra in
``pyproject.toml``. If pandas is not installed the call raises an
``ImportError`` with installation instructions.

Functions returning a ``dict`` (``project_summary``, ``integrity_check``)
do not depend on pandas.
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import-time only
    import pandas as pd

# Re-used internally for EAV row → native value coercion.
from noxdb.metadata import _row_to_value

# Re-used to derive the expected tier for a file_type.
from noxdb.files import (
    _ALL_TYPES as _FILE_TYPES,
    _archive_root,
    _expected_tier,
    _work_root,
)

# A metadata key must match this regex to be safe to interpolate as a
# DataFrame column header. The DB does not constrain key_name beyond
# VARCHAR(100), so we filter at read time rather than trusting it.
_SAFE_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# --------------------------------------------------------------------------- #
# Pandas lazy-loader
# --------------------------------------------------------------------------- #

def _pd() -> Any:
    """Import and return :mod:`pandas`, with a friendly error if missing."""
    try:
        import pandas as pd
    except ImportError as exc:  # pragma: no cover - exercised when extra missing
        raise ImportError(
            "pandas is required for DataFrame-returning queries; install with "
            "`pip install 'noxdb[analysis]'` or `pip install pandas`"
        ) from exc
    return pd


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #

def _fetch_dicts(cur) -> list[dict[str, Any]]:
    """Return ``cur.fetchall()`` as a list of column-keyed dicts."""
    rows = cur.fetchall()
    if not rows:
        return []
    columns = [d[0] for d in cur.description]
    return [dict(zip(columns, row)) for row in rows]


def _eav_rows_to_pivot(
    rows: list[dict[str, Any]],
    parent_col: str,
    *,
    keys: list[str] | None,
) -> dict[int, dict[str, Any]]:
    """Pivot raw EAV rows into ``{parent_id: {key_name: native_value}}``.

    Native-value coercion goes through :func:`metadata._row_to_value` so
    booleans come back as ``bool`` (the driver returns 0/1) and numeric
    values come back as ``Decimal``. Keys not in *keys* are dropped when
    *keys* is provided; ``None`` means keep them all.
    """
    keyset = set(keys) if keys is not None else None
    out: dict[int, dict[str, Any]] = {}
    for r in rows:
        k = r["key_name"]
        if keyset is not None and k not in keyset:
            continue
        parent_id = int(r[parent_col])
        out.setdefault(parent_id, {})[k] = _row_to_value(r)
    return out


# --------------------------------------------------------------------------- #
# Public API — sample-level queries
# --------------------------------------------------------------------------- #

def samples_for_project(
    cur,
    project_id: int,
    *,
    file_type: str | None = None,
    sample_type: str | None = None,
    has_files: bool | None = None,
    include_controls: bool = True,
) -> "pd.DataFrame":
    """Return one row per sample in a project, joined with parent IDs.

    Project membership lives entirely in ``project_samples``; plate
    controls (mockIP, anchor, NC) are linked there at import time, so
    they come back from the same junction scan as the real samples. The
    ``project_id`` column is therefore always the queried project.

    Output columns: ``project_id``, ``subject_id``, ``subject_code``,
    ``visit_id``, ``timepoint``, ``sample_id``, ``sample_name``,
    ``sample_type``, ``SQR``, ``SQRP``, ``library``, ``antibody_class``.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        project_id: Project to scan.
        file_type: Keep only samples that have at least one registered
            file of this type.
        sample_type: Keep only samples whose ``sample_type`` matches.
            When set, ``include_controls`` is ignored (the caller is
            being explicit about which type they want).
        has_files: If ``True``, keep only samples with ≥ 1 file. If
            ``False``, keep only samples with no files. If ``None``, no
            filter.
        include_controls: If ``True`` (default), keep the plate controls
            (mockIP, anchor, NC) linked to this project. If ``False``,
            exclude them. Ignored when ``sample_type`` is set.

    Returns:
        A ``pandas.DataFrame`` with one row per matching sample.

    Raises:
        ImportError: If pandas is not installed.
    """
    pd = _pd()

    # Step 1: every sample linked to this project, joined outward to its
    # lineage. project_samples is the sole membership source of truth.
    where = ["ps.project_id = ?"]
    params: list[Any] = [project_id]
    if sample_type is not None:
        where.append("sm.sample_type = ?")
        params.append(sample_type)
    elif not include_controls:
        where.append("sm.sample_type NOT IN ('mockIP', 'anchor', 'NC')")
    cur.execute(
        "SELECT sm.sample_id, sm.visit_id, sm.sample_name, sm.sample_type, "
        "sm.SQR, sm.SQRP, sm.library, sm.antibody_class, "
        "v.timepoint, sub.subject_id, sub.subject_code "
        "FROM project_samples ps "
        "JOIN samples sm   ON sm.sample_id   = ps.sample_id "
        "JOIN visits v     ON v.visit_id     = sm.visit_id "
        "JOIN subjects sub ON sub.subject_id = v.subject_id "
        f"WHERE {' AND '.join(where)} ORDER BY sm.sample_id",
        tuple(params),
    )
    sample_rows = _fetch_dicts(cur)
    if not sample_rows:
        return pd.DataFrame()

    # Step 2: optional file filters — one indexed lookup against sample_files.
    if has_files is not None or file_type is not None:
        sm_ids = [r["sample_id"] for r in sample_rows]
        f_ph = ",".join(["?"] * len(sm_ids))
        f_where = f"WHERE sample_id IN ({f_ph})"
        f_params: list[Any] = list(sm_ids)
        if file_type is not None:
            f_where += " AND file_type = ?"
            f_params.append(file_type)
        cur.execute(
            f"SELECT DISTINCT sample_id FROM sample_files {f_where}",
            tuple(f_params),
        )
        ids_with_files = {row[0] for row in cur.fetchall()}
        if has_files is True or file_type is not None:
            sample_rows = [r for r in sample_rows if r["sample_id"] in ids_with_files]
        else:  # has_files is False
            sample_rows = [r for r in sample_rows if r["sample_id"] not in ids_with_files]

    rows = [
        {
            "project_id": project_id,
            "subject_id": sr["subject_id"],
            "subject_code": sr["subject_code"],
            "visit_id": sr["visit_id"],
            "timepoint": sr["timepoint"],
            "sample_id": sr["sample_id"],
            "sample_name": sr["sample_name"],
            "sample_type": sr["sample_type"],
            "SQR": sr["SQR"],
            "SQRP": sr["SQRP"],
            "library": sr["library"],
            "antibody_class": sr["antibody_class"],
        }
        for sr in sample_rows
    ]
    return pd.DataFrame(rows)


def samples_with_metadata(
    cur,
    project_id: int,
    keys: list[str] | None = None,
    *,
    include_visit_metadata: bool = True,
) -> "pd.DataFrame":
    """Pivot EAV metadata into one row per sample (wide form).

    The base columns come from
    [`samples_for_project`][noxdb.queries.samples_for_project].
    Each metadata key becomes a column. Sample-level keys are taken from
    ``sample_metadata``; visit-level keys from ``visit_metadata`` are
    also added when ``include_visit_metadata`` is ``True``, prefixed
    with ``visit_`` on name collisions.

    Keys that do not match ``^[A-Za-z_][A-Za-z0-9_]*$`` are silently
    dropped to keep DataFrame column names well-formed.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        project_id: Project to pivot.
        keys: Restrict to this set of metadata keys. ``None`` keeps all
            keys present for the project.
        include_visit_metadata: Whether to also pivot visit-level keys.

    Returns:
        A ``pandas.DataFrame`` with one row per sample and one column
        per metadata key.

    Raises:
        ImportError: If pandas is not installed.
    """
    pd = _pd()
    base = samples_for_project(cur, project_id)
    if base.empty:
        return base

    sample_ids = base["sample_id"].astype(int).tolist()
    visit_ids = list({int(v) for v in base["visit_id"].tolist()})
    placeholders_s = ",".join(["?"] * len(sample_ids))

    cur.execute(
        "SELECT sample_id, key_name, value_int, value_numeric, value_bool, "
        "value_text, value_type FROM sample_metadata "
        f"WHERE sample_id IN ({placeholders_s})",
        tuple(sample_ids),
    )
    sm_rows = _fetch_dicts(cur)
    sm_pivot = _eav_rows_to_pivot(sm_rows, "sample_id", keys=keys)

    vm_pivot: dict[int, dict[str, Any]] = {}
    if include_visit_metadata and visit_ids:
        placeholders_v = ",".join(["?"] * len(visit_ids))
        cur.execute(
            "SELECT visit_id, key_name, value_int, value_numeric, value_bool, "
            "value_text, value_type FROM visit_metadata "
            f"WHERE visit_id IN ({placeholders_v})",
            tuple(visit_ids),
        )
        vm_rows = _fetch_dicts(cur)
        vm_pivot = _eav_rows_to_pivot(vm_rows, "visit_id", keys=keys)

    # Collect column names while preserving insertion order.
    sample_keys: list[str] = []
    seen_sample: set[str] = set()
    for d in sm_pivot.values():
        for k in d:
            if k not in seen_sample and _SAFE_KEY_RE.match(k):
                seen_sample.add(k)
                sample_keys.append(k)

    visit_keys: list[str] = []
    seen_visit: set[str] = set()
    for d in vm_pivot.values():
        for k in d:
            if k not in seen_visit and _SAFE_KEY_RE.match(k):
                seen_visit.add(k)
                visit_keys.append(k)

    # Resolve collisions between sample-level and visit-level keys by
    # prefixing the visit-level column with ``visit_``.
    rename_visit: dict[str, str] = {}
    for k in visit_keys:
        rename_visit[k] = f"visit_{k}" if k in seen_sample else k

    # Build columns dict to assign onto the base DataFrame in one pass.
    new_cols: dict[str, list[Any]] = {k: [] for k in sample_keys}
    for k in visit_keys:
        new_cols[rename_visit[k]] = []

    for _, row in base.iterrows():
        sid = int(row["sample_id"])
        vid = int(row["visit_id"])
        sm = sm_pivot.get(sid, {})
        vm = vm_pivot.get(vid, {})
        for k in sample_keys:
            new_cols[k].append(sm.get(k))
        for k in visit_keys:
            new_cols[rename_visit[k]].append(vm.get(k))

    for col, values in new_cols.items():
        base[col] = values
    return base


def controls_for_project(
    cur,
    project_id: int,
    *,
    sample_types: list[str] | None = None,
) -> "pd.DataFrame":
    """Return the control samples linked to a project.

    Controls (mockIP, anchor, NC) are linked into ``project_samples``
    at import time for every study project that shares their SQR+SQRP
    plate coordinates, so this is a plain junction scan filtered to the
    control ``sample_type``\\ s. A single physical control can be linked
    to several projects — this query handles that naturally.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        project_id: Study project whose plate controls you want.
        sample_types: Control types to include. Defaults to
            ``["mockIP", "anchor", "NC"]``. Pass e.g.
            ``["mockIP"]`` to retrieve only mocks.

    Returns:
        A ``pandas.DataFrame`` with columns: ``sample_id``, ``sample_name``,
        ``sample_type``, ``SQR``, ``SQRP``, ``library``, ``antibody_class``,
        ``visit_id``, ``timepoint``, ``subject_id``, ``subject_code``,
        ``project_id`` (the queried project's id).

    Raises:
        ImportError: If pandas is not installed.
    """
    pd = _pd()
    types = sample_types or ["mockIP", "anchor", "NC"]
    type_ph = ",".join(["?"] * len(types))
    cur.execute(
        "SELECT s.sample_id, s.sample_name, s.sample_type, s.SQR, s.SQRP, "
        "s.library, s.antibody_class, "
        "v.visit_id, v.timepoint, "
        "sub.subject_id, sub.subject_code, ps.project_id "
        "FROM project_samples ps "
        "JOIN samples s    ON s.sample_id    = ps.sample_id "
        "JOIN visits v     ON v.visit_id     = s.visit_id "
        "JOIN subjects sub ON sub.subject_id = v.subject_id "
        f"WHERE ps.project_id = ? AND s.sample_type IN ({type_ph}) "
        "ORDER BY s.sample_type, s.SQR, s.SQRP, s.sample_id",
        tuple([project_id] + list(types)),
    )
    return pd.DataFrame(_fetch_dicts(cur))


def list_inputs(cur) -> "pd.DataFrame":
    """Return all input DNA samples.

    Input samples are not associated with any study project. This is a
    plain select of every sample stored under the ``input`` project.

    Returns:
        A ``pandas.DataFrame`` with columns: ``sample_id``, ``sample_name``,
        ``sample_type``, ``SQR``, ``SQRP``, ``library``, ``antibody_class``,
        ``visit_id``, ``timepoint``, ``subject_id``, ``subject_code``,
        ``project_id``.

    Raises:
        ImportError: If pandas is not installed.
    """
    pd = _pd()
    cur.execute(
        "SELECT s.sample_id, s.sample_name, s.sample_type, s.SQR, s.SQRP, "
        "s.library, s.antibody_class, "
        "v.visit_id, v.timepoint, "
        "sub.subject_id, sub.subject_code, ps.project_id "
        "FROM project_samples ps "
        "JOIN projects p   ON p.project_id   = ps.project_id "
        "JOIN samples s    ON s.sample_id    = ps.sample_id "
        "JOIN visits v     ON v.visit_id     = s.visit_id "
        "JOIN subjects sub ON sub.subject_id = v.subject_id "
        "WHERE p.project_name = 'input' "
        "ORDER BY s.sample_id",
    )
    return pd.DataFrame(_fetch_dicts(cur))


def project_tidy_table(cur, project_id: int) -> "pd.DataFrame":
    """Full tidy table for a project — every sample × every metadata key.

    Convenience wrapper over
    [`samples_with_metadata`][noxdb.queries.samples_with_metadata]
    with both sample- and visit-level metadata included. The intended
    use is ``df.to_csv(...)`` / ``df.to_excel(...)`` for downstream
    analysis in pandas or R.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        project_id: Project to pivot.

    Returns:
        A ``pandas.DataFrame`` ready to write to CSV / XLSX.

    Raises:
        ImportError: If pandas is not installed.
    """
    return samples_with_metadata(cur, project_id, keys=None, include_visit_metadata=True)


# --------------------------------------------------------------------------- #
# Public API — file-level queries
# --------------------------------------------------------------------------- #

def files_for_project(
    cur,
    project_id: int,
    *,
    file_type: str | None = None,
    storage_tier: str | None = None,
) -> "pd.DataFrame":
    """Return one row per registered file in a project.

    Columns include the ``sample_files`` row plus parent identifiers
    (``sample_name``, ``subject_code``, ``timepoint``) so the result
    can be filtered/grouped without further joins.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        project_id: Project to scan.
        file_type: Optional ``file_type`` filter.
        storage_tier: Optional ``storage_tier`` filter.

    Returns:
        A ``pandas.DataFrame`` with one row per matching file.

    Raises:
        ImportError: If pandas is not installed.
    """
    pd = _pd()
    where = ["ps.project_id = ?"]
    params: list[Any] = [project_id]
    if file_type is not None:
        where.append("f.file_type = ?")
        params.append(file_type)
    if storage_tier is not None:
        where.append("f.storage_tier = ?")
        params.append(storage_tier)
    cur.execute(
        "SELECT f.file_id, f.sample_id, sm.sample_name, "
        "s.subject_code, v.timepoint, "
        "f.file_type, f.file_path, f.file_size_bytes, f.checksum_md5, "
        "f.storage_tier, f.created_at "
        "FROM project_samples ps "
        "JOIN sample_files f ON f.sample_id = ps.sample_id "
        "JOIN samples sm ON sm.sample_id = f.sample_id "
        "JOIN visits v ON v.visit_id = sm.visit_id "
        "JOIN subjects s ON s.subject_id = v.subject_id "
        f"WHERE {' AND '.join(where)} ORDER BY f.file_id",
        tuple(params),
    )
    return pd.DataFrame(_fetch_dicts(cur))


# --------------------------------------------------------------------------- #
# Public API — reports (dict-returning, no pandas dependency)
# --------------------------------------------------------------------------- #

def project_summary(cur, project_id: int) -> dict[str, Any]:
    """Return counts for a project: subjects, visits, samples, files.

    Does not raise if the project does not exist — the report just
    shows zeros. Has no pandas dependency.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        project_id: Project to summarize.

    Returns:
        ``{"project_id", "n_subjects", "n_visits", "n_samples",
        "n_files", "files_by_type"}`` where ``files_by_type`` is a
        ``dict[str, int]`` keyed by ``file_type``.
    """
    cur.execute(
        "SELECT COUNT(DISTINCT sub.subject_id) FROM project_samples ps "
        "JOIN samples sm   ON sm.sample_id   = ps.sample_id "
        "JOIN visits v     ON v.visit_id     = sm.visit_id "
        "JOIN subjects sub ON sub.subject_id = v.subject_id "
        "WHERE ps.project_id = ?",
        (project_id,),
    )
    n_subjects = int(cur.fetchone()[0])

    cur.execute(
        "SELECT COUNT(DISTINCT sm.visit_id) FROM project_samples ps "
        "JOIN samples sm ON sm.sample_id = ps.sample_id "
        "WHERE ps.project_id = ?",
        (project_id,),
    )
    n_visits = int(cur.fetchone()[0])

    cur.execute(
        "SELECT COUNT(*) FROM project_samples ps WHERE ps.project_id = ?",
        (project_id,),
    )
    n_samples = int(cur.fetchone()[0])

    cur.execute(
        "SELECT f.file_type, COUNT(*) FROM project_samples ps "
        "JOIN sample_files f ON f.sample_id = ps.sample_id "
        "WHERE ps.project_id = ? GROUP BY f.file_type",
        (project_id,),
    )
    files_by_type = {ft: int(n) for ft, n in cur.fetchall()}
    n_files = sum(files_by_type.values())

    # Controls are linked into project_samples at import time, so the
    # control counts are just the control-typed rows of this project.
    cur.execute(
        "SELECT sm.sample_type, COUNT(*) FROM project_samples ps "
        "JOIN samples sm ON sm.sample_id = ps.sample_id "
        "WHERE ps.project_id = ? "
        "AND sm.sample_type IN ('mockIP', 'anchor', 'NC') "
        "GROUP BY sm.sample_type",
        (project_id,),
    )
    controls_by_type = {st: int(n) for st, n in cur.fetchall()}

    return {
        "project_id": project_id,
        "n_subjects": n_subjects,
        "n_visits": n_visits,
        "n_samples": n_samples,
        "n_files": n_files,
        "files_by_type": files_by_type,
        "n_controls": sum(controls_by_type.values()),
        "controls_by_type": controls_by_type,
    }


def find_db_files_missing_on_disk(
    cur,
    *,
    project_id: int | None = None,
) -> "pd.DataFrame":
    """Return DB-registered files whose path does not exist on disk.

    Useful as a cron job to detect drift between the database and
    storage. ``os.path.exists`` follows symlinks, so a dangling link
    reads as missing.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        project_id: Limit the scan to a single project; ``None`` checks
            every row in ``sample_files``.

    Returns:
        A ``pandas.DataFrame`` with the same columns as
        [`files_for_project`][noxdb.queries.files_for_project],
        containing only rows whose path is missing.

    Raises:
        ImportError: If pandas is not installed.
    """
    pd = _pd()
    cols = (
        "SELECT f.file_id, f.sample_id, sm.sample_name, "
        "s.subject_code, v.timepoint, "
        "f.file_type, f.file_path, f.file_size_bytes, f.checksum_md5, "
        "f.storage_tier, f.created_at "
    )
    if project_id is not None:
        cur.execute(
            cols
            + "FROM project_samples ps "
            "JOIN sample_files f ON f.sample_id = ps.sample_id "
            "JOIN samples sm ON sm.sample_id = f.sample_id "
            "JOIN visits v ON v.visit_id = sm.visit_id "
            "JOIN subjects s ON s.subject_id = v.subject_id "
            "WHERE ps.project_id = ? ORDER BY f.file_id",
            (project_id,),
        )
    else:
        cur.execute(
            cols
            + "FROM sample_files f "
            "JOIN samples sm ON sm.sample_id = f.sample_id "
            "JOIN visits v ON v.visit_id = sm.visit_id "
            "JOIN subjects s ON s.subject_id = v.subject_id "
            "ORDER BY f.file_id",
        )
    rows = _fetch_dicts(cur)
    missing = [r for r in rows if not os.path.exists(r["file_path"])]
    return pd.DataFrame(missing)


def find_disk_files_missing_in_db(
    cur,
    *,
    roots: list[str] | None = None,
) -> "pd.DataFrame":
    """Return regular files under roots that are NOT in ``sample_files``.

    This is a full filesystem walk; on a real archive it can take
    minutes. Restrict via *roots* in interactive use. Roots that don't
    exist on the current host are silently skipped, which makes the
    function safe to call from a laptop with no LiSC mount.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        roots: List of directories to walk. ``None`` scans
            ``NOXDB_ARCHIVE_ROOT`` and ``NOXDB_WORK_ROOT`` (defaults
            ``/lisc/archive`` and ``/lisc/work``).

    Returns:
        A ``pandas.DataFrame`` with columns ``file_path``, ``root``,
        ``file_size_bytes``.

    Raises:
        ImportError: If pandas is not installed.
    """
    pd = _pd()
    if roots is None:
        roots = [_archive_root(), _work_root()]

    cur.execute("SELECT file_path FROM sample_files")
    known = {row[0] for row in cur.fetchall()}

    found: list[dict[str, Any]] = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        resolved_root = os.path.realpath(root)
        for dirpath, _dirnames, filenames in os.walk(resolved_root):
            for name in filenames:
                full = os.path.join(dirpath, name)
                if full in known:
                    continue
                try:
                    size = os.path.getsize(full)
                except OSError:
                    size = None
                found.append(
                    {"file_path": full, "root": resolved_root, "file_size_bytes": size}
                )
    return pd.DataFrame(found)


def integrity_check(cur, project_id: int) -> dict[str, Any]:
    """Run a battery of sanity checks for a project. Read-only.

    Does not touch the filesystem beyond ``realpath`` string operations
    on the configured roots.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        project_id: Project to check.

    Returns:
        A report dict with these keys:

        - ``samples_without_files``: list of ``{sample_id,
          sample_name}`` with zero registered files.
        - ``archive_files_missing_md5``: archive-tier files with NULL
          ``checksum_md5`` (archive convention: always store the
          checksum).
        - ``files_outside_tier_root``: files whose ``file_path`` does
          not live under the root configured for their
          ``storage_tier``. Includes ``scratch`` / ``external`` rows
          only when the path resolves outside both LABDB roots.
        - ``unknown_file_types``: ``sample_files`` rows whose
          ``file_type`` is not in the canonical set (defensive — the
          ENUM should prevent this).
    """
    report: dict[str, Any] = {
        "project_id": project_id,
        "samples_without_files": [],
        "archive_files_missing_md5": [],
        "files_outside_tier_root": [],
        "unknown_file_types": [],
    }

    cur.execute(
        "SELECT sm.sample_id, sm.sample_name FROM project_samples ps "
        "JOIN samples sm ON sm.sample_id = ps.sample_id "
        "LEFT JOIN sample_files f ON f.sample_id = sm.sample_id "
        "WHERE ps.project_id = ? AND f.file_id IS NULL "
        "ORDER BY sm.sample_id",
        (project_id,),
    )
    report["samples_without_files"] = [
        {"sample_id": int(sid), "sample_name": name}
        for sid, name in cur.fetchall()
    ]

    cur.execute(
        "SELECT f.file_id, f.file_path FROM project_samples ps "
        "JOIN sample_files f ON f.sample_id = ps.sample_id "
        "WHERE ps.project_id = ? AND f.storage_tier = 'archive' "
        "AND f.checksum_md5 IS NULL",
        (project_id,),
    )
    report["archive_files_missing_md5"] = [
        {"file_id": int(fid), "file_path": path}
        for fid, path in cur.fetchall()
    ]

    cur.execute(
        "SELECT f.file_id, f.file_type, f.file_path, f.storage_tier "
        "FROM project_samples ps "
        "JOIN sample_files f ON f.sample_id = ps.sample_id "
        "WHERE ps.project_id = ?",
        (project_id,),
    )
    # Build a list of acceptable prefixes per tier. Both the raw (as
    # configured) root and the realpath-resolved root are accepted, because
    # either side may be a symlink: DB rows are stored verbatim from the
    # caller (often using the raw mount path), while operator-facing tools
    # may resolve roots. Accepting both shapes prevents false positives in
    # either direction.
    archive_root_raw = os.path.normpath(_archive_root())
    work_root_raw = os.path.normpath(_work_root())
    archive_root_real = os.path.realpath(_archive_root())
    work_root_real = os.path.realpath(_work_root())
    archive_roots = sorted({archive_root_raw, archive_root_real})
    work_roots = sorted({work_root_raw, work_root_real})

    def _under_any(path: str, roots: list[str]) -> bool:
        """True iff *path* sits at-or-below any of *roots* by commonpath.

        ``commonpath`` is used instead of string ``startswith`` so a sibling
        with the same string prefix (e.g. ``/lisc/archive_old`` vs
        ``/lisc/archive``) is NOT classified as inside the root.
        """
        norm = os.path.normpath(path)
        for root in roots:
            try:
                if os.path.commonpath([norm, root]) == root:
                    return True
            except ValueError:
                # Mixed drive letters on Windows or non-absolute path.
                continue
        return False

    for fid, ftype, fpath, tier in cur.fetchall():
        if ftype not in _FILE_TYPES:
            report["unknown_file_types"].append(
                {"file_id": int(fid), "file_type": ftype, "file_path": fpath}
            )
            continue
        # Note: we deliberately do NOT realpath *fpath* itself — the file may
        # not exist on the current host (e.g. running this check from a
        # laptop without LiSC mounted), and realpath of a non-existent path
        # returns the path unchanged on POSIX, so resolving it adds no value.
        under_archive = _under_any(fpath, archive_roots)
        under_work = _under_any(fpath, work_roots)

        if tier == "archive" and not under_archive:
            report["files_outside_tier_root"].append(
                {"file_id": int(fid), "file_path": fpath, "tier": tier,
                 "expected_roots": archive_roots}
            )
        elif tier == "work" and not under_work:
            report["files_outside_tier_root"].append(
                {"file_id": int(fid), "file_path": fpath, "tier": tier,
                 "expected_roots": work_roots}
            )
        elif tier in {"archive", "work"}:
            # Cross-check: archive file_type stored as work, or vice versa.
            expected_tier = _expected_tier(ftype)
            if expected_tier != tier:
                report["files_outside_tier_root"].append(
                    {"file_id": int(fid), "file_path": fpath, "tier": tier,
                     "expected_tier": expected_tier}
                )
        elif tier in {"scratch", "external"}:
            # scratch/external have no enforced root. We still surface rows
            # whose path falls outside both LABDB roots so operators get an
            # audit-friendly list of files registered off the managed
            # storage entirely — useful when reconciling backups / DR.
            if not under_archive and not under_work:
                report["files_outside_tier_root"].append(
                    {"file_id": int(fid), "file_path": fpath, "tier": tier,
                     "checked_roots": archive_roots + work_roots,
                     "reason": "scratch/external path is outside both LABDB roots"}
                )
    return report
