"""Composite read-only queries and reports.

These helpers join the hierarchy (``projects → subjects → visits → samples``)
and pivot the EAV metadata tables into wide-form tables suitable for
notebook analysis. Everything here is **read-only**: nothing in this module
issues INSERT/UPDATE/DELETE.

All functions take a cursor as their first argument, so callers control
the transaction boundary just like with the CRUD modules:

    from dbmaria_utils import queries, transaction

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
from dbmaria_utils.metadata import _row_to_value

# Re-used to derive the expected tier for a file_type.
from dbmaria_utils.files import (
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
            "`pip install 'phiper-db[analysis]'` or `pip install pandas`"
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
) -> "pd.DataFrame":
    """Return one row per sample in *project_id*, joined with parent IDs.

    Columns: ``project_id``, ``subject_id``, ``subject_code``, ``visit_id``,
    ``timepoint``, ``sample_id``, ``sample_name``, ``sample_type``, ``SQR``,
    ``SQRP``, ``library``, ``antibody_class``.

    Optional filters:

    - *file_type*: keep only samples that have at least one registered file
      of this type.
    - *sample_type*: keep only samples with this ``sample_type``.
    - *has_files*: if True, keep only samples with ≥1 file; if False, keep
      only samples with no files; if None, no filter.
    """
    pd = _pd()
    # Params must be built in SQL textual order (JOIN ... WHERE ...) because
    # MariaDB binds placeholders positionally. The file_type filter lives in
    # a JOIN clause emitted before the WHERE, so its parameter has to come
    # first.
    join_params: list[Any] = []
    join_files = ""
    if file_type is not None:
        join_files = (
            " JOIN sample_files f ON f.sample_id = sm.sample_id "
            "AND f.file_type = ?"
        )
        join_params.append(file_type)
    where = ["s.project_id = ?"]
    where_params: list[Any] = [project_id]
    if sample_type is not None:
        where.append("sm.sample_type = ?")
        where_params.append(sample_type)
    params = join_params + where_params

    cur.execute(
        "SELECT s.project_id, s.subject_id, s.subject_code, "
        "v.visit_id, v.timepoint, "
        "sm.sample_id, sm.sample_name, sm.sample_type, "
        "sm.SQR, sm.SQRP, sm.library, sm.antibody_class "
        "FROM subjects s "
        "JOIN visits v ON v.subject_id = s.subject_id "
        "JOIN samples sm ON sm.visit_id = v.visit_id"
        + join_files
        + " WHERE " + " AND ".join(where)
        + " GROUP BY sm.sample_id "  # dedupe in case of file_type join multi-match
        + "ORDER BY sm.sample_id",
        tuple(params),
    )
    rows = _fetch_dicts(cur)

    if has_files is not None and rows:
        sample_ids = [r["sample_id"] for r in rows]
        placeholders = ",".join(["?"] * len(sample_ids))
        cur.execute(
            "SELECT sample_id, COUNT(*) AS n FROM sample_files "
            f"WHERE sample_id IN ({placeholders}) GROUP BY sample_id",
            tuple(sample_ids),
        )
        with_files = {int(r["sample_id"]) for r in _fetch_dicts(cur)}
        if has_files:
            rows = [r for r in rows if int(r["sample_id"]) in with_files]
        else:
            rows = [r for r in rows if int(r["sample_id"]) not in with_files]

    return pd.DataFrame(rows)


def samples_with_metadata(
    cur,
    project_id: int,
    keys: list[str] | None = None,
    *,
    include_visit_metadata: bool = True,
) -> "pd.DataFrame":
    """Pivot EAV metadata into one row per sample (wide form).

    The base columns come from :func:`samples_for_project`. Each metadata
    key becomes a column. Sample-level metadata keys are taken from
    ``sample_metadata``; when *include_visit_metadata* is True, visit-level
    keys from ``visit_metadata`` are also added (prefixed with ``visit_``
    if a name collision with a sample-level key occurs).

    *keys* restricts the set of metadata keys returned. When None, all
    keys present for the project are included. Keys that do not match
    ``^[A-Za-z_][A-Za-z0-9_]*$`` are silently dropped to keep DataFrame
    column names well-formed; mention these in user docs if you rely on
    exotic key names.
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


def project_tidy_table(cur, project_id: int) -> "pd.DataFrame":
    """Full tidy table for *project_id* — every sample × every metadata key.

    Convenience wrapper over :func:`samples_with_metadata` with both
    sample- and visit-level metadata included. The intended use is
    ``df.to_csv(...)`` / ``df.to_excel(...)`` for downstream analysis in
    pandas or R.
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
    """Return one row per registered file in *project_id*.

    Columns include the ``sample_files`` row plus parent identifiers
    (``sample_name``, ``subject_code``, ``timepoint``) so the result can
    be filtered/grouped without further joins.
    """
    pd = _pd()
    where = ["s.project_id = ?"]
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
        "FROM sample_files f "
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
    """Return counts for *project_id*: subjects, visits, samples, files.

    The ``files_by_type`` sub-dict gives a per-``file_type`` breakdown.
    Returns ``{"project_id": project_id, ...}`` with zero counts if the
    project has no rows; raises nothing if the project does not exist
    (the report just shows zeros).
    """
    cur.execute(
        "SELECT COUNT(*) FROM subjects WHERE project_id = ?", (project_id,)
    )
    n_subjects = int(cur.fetchone()[0])

    cur.execute(
        "SELECT COUNT(*) FROM visits v "
        "JOIN subjects s ON s.subject_id = v.subject_id "
        "WHERE s.project_id = ?",
        (project_id,),
    )
    n_visits = int(cur.fetchone()[0])

    cur.execute(
        "SELECT COUNT(*) FROM samples sm "
        "JOIN visits v ON v.visit_id = sm.visit_id "
        "JOIN subjects s ON s.subject_id = v.subject_id "
        "WHERE s.project_id = ?",
        (project_id,),
    )
    n_samples = int(cur.fetchone()[0])

    cur.execute(
        "SELECT f.file_type, COUNT(*) FROM sample_files f "
        "JOIN samples sm ON sm.sample_id = f.sample_id "
        "JOIN visits v ON v.visit_id = sm.visit_id "
        "JOIN subjects s ON s.subject_id = v.subject_id "
        "WHERE s.project_id = ? GROUP BY f.file_type",
        (project_id,),
    )
    files_by_type = {ft: int(n) for ft, n in cur.fetchall()}
    n_files = sum(files_by_type.values())

    return {
        "project_id": project_id,
        "n_subjects": n_subjects,
        "n_visits": n_visits,
        "n_samples": n_samples,
        "n_files": n_files,
        "files_by_type": files_by_type,
    }


def find_db_files_missing_on_disk(
    cur,
    *,
    project_id: int | None = None,
) -> "pd.DataFrame":
    """Return DB-registered files whose ``file_path`` does not exist on disk.

    Useful as a cron job to detect drift between the database and storage.
    When *project_id* is given, the scan is limited to that project; when
    None, every file in ``sample_files`` is checked. ``os.path.exists``
    follows symlinks, so a dangling link reads as missing.

    Columns: same as :func:`files_for_project`.
    """
    pd = _pd()
    where = []
    params: list[Any] = []
    if project_id is not None:
        where.append("s.project_id = ?")
        params.append(project_id)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    cur.execute(
        "SELECT f.file_id, f.sample_id, sm.sample_name, "
        "s.subject_code, v.timepoint, "
        "f.file_type, f.file_path, f.file_size_bytes, f.checksum_md5, "
        "f.storage_tier, f.created_at "
        "FROM sample_files f "
        "JOIN samples sm ON sm.sample_id = f.sample_id "
        "JOIN visits v ON v.visit_id = sm.visit_id "
        "JOIN subjects s ON s.subject_id = v.subject_id"
        + where_sql
        + " ORDER BY f.file_id",
        tuple(params),
    )
    rows = _fetch_dicts(cur)
    missing = [r for r in rows if not os.path.exists(r["file_path"])]
    return pd.DataFrame(missing)


def find_disk_files_missing_in_db(
    cur,
    *,
    roots: list[str] | None = None,
) -> "pd.DataFrame":
    """Return regular files under *roots* that are NOT in ``sample_files``.

    When *roots* is None, scans both ``LABDB_ARCHIVE_ROOT`` and
    ``LABDB_WORK_ROOT`` (defaults ``/lisc/archive`` and ``/lisc/work``).
    Roots that don't exist on the current host are silently skipped, which
    makes the function safe to call from a laptop with no LiSC mount.

    Columns: ``file_path``, ``root``, ``file_size_bytes``.

    Note: this is a full filesystem walk; on a real archive it can take
    minutes. Restrict via *roots* in interactive use.
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
    """Run a battery of sanity checks for *project_id*. Read-only.

    Returns a report:

    - ``samples_without_files``: list of ``{sample_id, sample_name}`` with
      zero registered files.
    - ``archive_files_missing_md5``: archive-tier files with NULL
      ``checksum_md5`` (archive convention: always store the checksum).
    - ``files_outside_tier_root``: files whose ``file_path`` does not live
      under the root configured for their ``storage_tier``. Includes
      ``scratch`` / ``external`` rows only when the path resolves outside
      both LABDB roots.
    - ``unknown_file_types``: ``sample_files`` rows whose ``file_type`` is
      not in the canonical set (defensive — the ENUM should prevent this).

    The check itself does not touch the filesystem beyond ``realpath``
    string operations on the configured roots.
    """
    report: dict[str, Any] = {
        "project_id": project_id,
        "samples_without_files": [],
        "archive_files_missing_md5": [],
        "files_outside_tier_root": [],
        "unknown_file_types": [],
    }

    cur.execute(
        "SELECT sm.sample_id, sm.sample_name FROM samples sm "
        "JOIN visits v ON v.visit_id = sm.visit_id "
        "JOIN subjects s ON s.subject_id = v.subject_id "
        "LEFT JOIN sample_files f ON f.sample_id = sm.sample_id "
        "WHERE s.project_id = ? AND f.file_id IS NULL "
        "ORDER BY sm.sample_id",
        (project_id,),
    )
    report["samples_without_files"] = [
        {"sample_id": int(sid), "sample_name": name}
        for sid, name in cur.fetchall()
    ]

    cur.execute(
        "SELECT f.file_id, f.file_path FROM sample_files f "
        "JOIN samples sm ON sm.sample_id = f.sample_id "
        "JOIN visits v ON v.visit_id = sm.visit_id "
        "JOIN subjects s ON s.subject_id = v.subject_id "
        "WHERE s.project_id = ? AND f.storage_tier = 'archive' "
        "AND f.checksum_md5 IS NULL",
        (project_id,),
    )
    report["archive_files_missing_md5"] = [
        {"file_id": int(fid), "file_path": path}
        for fid, path in cur.fetchall()
    ]

    cur.execute(
        "SELECT f.file_id, f.file_type, f.file_path, f.storage_tier "
        "FROM sample_files f "
        "JOIN samples sm ON sm.sample_id = f.sample_id "
        "JOIN visits v ON v.visit_id = sm.visit_id "
        "JOIN subjects s ON s.subject_id = v.subject_id "
        "WHERE s.project_id = ?",
        (project_id,),
    )
    archive_root = os.path.realpath(_archive_root())
    work_root = os.path.realpath(_work_root())
    for fid, ftype, fpath, tier in cur.fetchall():
        if ftype not in _FILE_TYPES:
            report["unknown_file_types"].append(
                {"file_id": int(fid), "file_type": ftype, "file_path": fpath}
            )
            continue
        # Compare against the *string* prefix of the configured root rather
        # than calling realpath on fpath (the file may not exist locally,
        # e.g. when running this check from a laptop without LiSC mount).
        if tier == "archive" and not fpath.startswith(archive_root + os.sep):
            report["files_outside_tier_root"].append(
                {"file_id": int(fid), "file_path": fpath, "tier": tier,
                 "expected_root": archive_root}
            )
        elif tier == "work" and not fpath.startswith(work_root + os.sep):
            report["files_outside_tier_root"].append(
                {"file_id": int(fid), "file_path": fpath, "tier": tier,
                 "expected_root": work_root}
            )
        elif tier in {"archive", "work"}:
            # Cross-check: archive file_type stored as work, or vice versa.
            expected_tier = _expected_tier(ftype)
            if expected_tier != tier:
                report["files_outside_tier_root"].append(
                    {"file_id": int(fid), "file_path": fpath, "tier": tier,
                     "expected_tier": expected_tier}
                )
    return report
