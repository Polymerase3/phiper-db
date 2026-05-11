"""Validate a :class:`ProjectBundle` and (optionally) commit it.

The runner is split into two distinct phases so import errors never
leave the database in a half-written state:

1. **Validation** — schema, referential, duplicate, on-disk path, and
   project-existence checks. Errors are collected exhaustively (not
   short-circuit) so the user sees every problem in one pass.
2. **Commit** — a single :func:`transaction` block calling the
   existing CRUD wrappers in hierarchical order. An exception anywhere
   rolls back the entire import; partial states are impossible.

The split also gives ``--dry-run`` for free: skip phase 2.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dbmaria_utils import files as files_mod
from dbmaria_utils import metadata, projects, samples, subjects, visits
from dbmaria_utils._import import loader, schema
from dbmaria_utils.connection import transaction


class ProjectImportError(RuntimeError):
    """Raised when validation fails or the project already exists without --force."""

    def __init__(self, message: str, errors: list[str] | None = None) -> None:
        super().__init__(message)
        self.errors = list(errors or [])


@dataclass
class ImportReport:
    project_name: str
    project_id: int | None = None
    dry_run: bool = False
    force: bool = False
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    counts: dict[str, dict[str, int]] = field(default_factory=dict)
    duration_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_name": self.project_name,
            "project_id": self.project_id,
            "dry_run": self.dry_run,
            "force": self.force,
            "warnings": self.warnings,
            "errors": self.errors,
            "counts": self.counts,
            "duration_seconds": round(self.duration_seconds, 3),
        }


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def _validate_schema(bundle: loader.ProjectBundle) -> list[str]:
    """Schema-level checks: enum membership and parseable numerics.

    Doesn't look at the database, just at the row contents.
    """
    errs: list[str] = []
    for r in bundle.subjects:
        if r.sex not in schema.ALLOWED_SEX:
            errs.append(
                f"subjects.csv row {r.row_num}: sex={r.sex!r} not in "
                f"{sorted(schema.ALLOWED_SEX)}"
            )
        if not r.subject_code:
            errs.append(f"subjects.csv row {r.row_num}: subject_code is empty")

    for r in bundle.visits:
        if not r.timepoint:
            # The schema permits NULL timepoints, but get_or_create rejects
            # them (UNIQUE doesn't dedupe NULL) — so the importer does too.
            errs.append(
                f"visits.csv row {r.row_num}: timepoint is empty; "
                "non-null timepoints are required for idempotent import"
            )
        try:
            age = schema.coerce_int(r.age, field=f"visits.csv row {r.row_num}.age")
            if age < 0:
                errs.append(
                    f"visits.csv row {r.row_num}: age must be >= 0, got {age}"
                )
        except ValueError as exc:
            errs.append(str(exc))

    for r in bundle.samples:
        if r.sample_type not in schema.ALLOWED_SAMPLE_TYPE:
            errs.append(
                f"samples.csv row {r.row_num}: sample_type={r.sample_type!r} "
                f"not in {sorted(schema.ALLOWED_SAMPLE_TYPE)}"
            )

    for r in bundle.files:
        if r.file_type not in schema.ALLOWED_FILE_TYPE:
            errs.append(
                f"manifest.csv row {r.row_num}: file_type={r.file_type!r} "
                f"not in {sorted(schema.ALLOWED_FILE_TYPE)}"
            )
        if r.storage_tier is not None and r.storage_tier not in schema.ALLOWED_STORAGE_TIER:
            errs.append(
                f"manifest.csv row {r.row_num}: storage_tier={r.storage_tier!r} "
                f"not in {sorted(schema.ALLOWED_STORAGE_TIER)}"
            )

    return errs


def _validate_referential(bundle: loader.ProjectBundle) -> list[str]:
    """Cross-file references and duplicates within the bundle."""
    errs: list[str] = []

    # subjects.csv → unique subject_code
    seen_subjects: set[str] = set()
    for r in bundle.subjects:
        if r.subject_code in seen_subjects:
            errs.append(
                f"subjects.csv row {r.row_num}: duplicate subject_code "
                f"{r.subject_code!r}"
            )
        seen_subjects.add(r.subject_code)

    # visits.csv → unique (subject_code, timepoint); subject must exist
    seen_visits: set[tuple[str, str]] = set()
    for r in bundle.visits:
        if r.subject_code not in seen_subjects:
            errs.append(
                f"visits.csv row {r.row_num}: subject_code "
                f"{r.subject_code!r} not in subjects.csv"
            )
        key = (r.subject_code, r.timepoint)
        if key in seen_visits:
            errs.append(
                f"visits.csv row {r.row_num}: duplicate (subject_code, "
                f"timepoint)={key!r}"
            )
        seen_visits.add(key)

    # samples.csv → unique sample_name; (subject_code, timepoint) must exist
    seen_samples: set[str] = set()
    for r in bundle.samples:
        if (r.subject_code, r.timepoint) not in seen_visits:
            errs.append(
                f"samples.csv row {r.row_num}: (subject_code, timepoint)="
                f"({r.subject_code!r}, {r.timepoint!r}) not in visits.csv"
            )
        if r.sample_name in seen_samples:
            errs.append(
                f"samples.csv row {r.row_num}: duplicate sample_name "
                f"{r.sample_name!r}"
            )
        seen_samples.add(r.sample_name)

    # manifest.csv → unique file_path; sample must exist in samples.csv
    seen_paths: set[str] = set()
    for r in bundle.files:
        if r.sample_name not in seen_samples:
            errs.append(
                f"manifest.csv row {r.row_num}: sample_name {r.sample_name!r} "
                "not in samples.csv"
            )
        if r.file_path in seen_paths:
            errs.append(
                f"manifest.csv row {r.row_num}: duplicate file_path "
                f"{r.file_path!r}"
            )
        seen_paths.add(r.file_path)

    return errs


def _validate_disk(bundle: loader.ProjectBundle) -> list[str]:
    """Confirm every manifest path resolves on the local filesystem."""
    return [
        f"manifest.csv row {r.row_num}: file does not exist on disk: {r.file_path}"
        for r in bundle.files
        if not os.path.exists(r.file_path)
    ]


def _validate_db_collisions(cur, bundle: loader.ProjectBundle) -> list[str]:
    """Block on UNIQUE keys owned by OTHER projects.

    The hierarchy has two cross-project UNIQUEs that can't be resolved by
    ``get_or_create``: ``samples.sample_name`` and ``sample_files.file_path``.
    If either collides with a row in a different project we refuse the
    import even with ``--force``.
    """
    errs: list[str] = []
    project_name = bundle.project.project_name
    project_row = projects.get_by_name(cur, project_name)
    project_id = project_row["project_id"] if project_row else None

    for r in bundle.samples:
        existing = samples.get_by_name(cur, r.sample_name)
        if existing is None:
            continue
        # Walk up to project_id.
        cur.execute(
            "SELECT s.project_id FROM samples sm "
            "JOIN visits v ON v.visit_id = sm.visit_id "
            "JOIN subjects s ON s.subject_id = v.subject_id "
            "WHERE sm.sample_id = ?",
            (existing["sample_id"],),
        )
        existing_pid = cur.fetchone()[0]
        if existing_pid != project_id:
            errs.append(
                f"samples.csv row {r.row_num}: sample_name {r.sample_name!r} "
                f"already belongs to project_id={existing_pid}"
            )

    for r in bundle.files:
        existing = files_mod.get_by_path(cur, r.file_path)
        if existing is None:
            continue
        cur.execute(
            "SELECT s.project_id FROM sample_files f "
            "JOIN samples sm ON sm.sample_id = f.sample_id "
            "JOIN visits v ON v.visit_id = sm.visit_id "
            "JOIN subjects s ON s.subject_id = v.subject_id "
            "WHERE f.file_id = ?",
            (existing["file_id"],),
        )
        existing_pid = cur.fetchone()[0]
        if existing_pid != project_id:
            errs.append(
                f"manifest.csv row {r.row_num}: file_path {r.file_path!r} "
                f"already belongs to project_id={existing_pid}"
            )

    return errs


# --------------------------------------------------------------------------- #
# Commit
# --------------------------------------------------------------------------- #

def _commit(
    cur, bundle: loader.ProjectBundle, *, compute_md5: bool,
) -> tuple[dict[str, dict[str, int]], int]:
    """Write the bundle to the database. Caller owns the transaction."""
    counts = {
        "projects":   {"inserted": 0, "existing": 0},
        "subjects":   {"inserted": 0, "existing": 0},
        "visits":     {"inserted": 0, "existing": 0},
        "samples":    {"inserted": 0, "existing": 0},
        "files":      {"inserted": 0, "existing": 0},
        "metadata":   {"inserted": 0, "updated": 0, "unchanged": 0},
    }

    pid, created = projects.get_or_create(
        cur, bundle.project.project_name,
        description=bundle.project.description,
        pi_name=bundle.project.pi_name,
    )
    counts["projects"]["inserted" if created else "existing"] += 1

    subject_ids: dict[str, int] = {}
    for s in bundle.subjects:
        sid, created = subjects.get_or_create(
            cur, pid, s.subject_code, s.sex, origin=s.origin,
        )
        subject_ids[s.subject_code] = sid
        counts["subjects"]["inserted" if created else "existing"] += 1

    visit_ids: dict[tuple[str, str], int] = {}
    for v in bundle.visits:
        age = schema.coerce_int(v.age, field=f"visits.csv row {v.row_num}.age")
        vid, created = visits.get_or_create(
            cur, subject_ids[v.subject_code], v.timepoint, v.group_test, age,
        )
        visit_ids[(v.subject_code, v.timepoint)] = vid
        counts["visits"]["inserted" if created else "existing"] += 1
        for key, val in v.metadata.items():
            result = metadata.set_visit(cur, vid, key, val)
            counts["metadata"][result] += 1

    sample_ids: dict[str, int] = {}
    for sm in bundle.samples:
        vid = visit_ids[(sm.subject_code, sm.timepoint)]
        sid, created = samples.get_or_create(
            cur, vid, sm.sample_name, sm.sample_type, sm.sqr, sm.sqrp,
            sm.library, antibody_class=sm.antibody_class,
        )
        sample_ids[sm.sample_name] = sid
        counts["samples"]["inserted" if created else "existing"] += 1
        for key, val in sm.metadata.items():
            result = metadata.set_sample(cur, sid, key, val)
            counts["metadata"][result] += 1

    for f in bundle.files:
        fid, created = files_mod.get_or_register(
            cur,
            sample_ids[f.sample_name],
            f.file_path,
            f.file_type,
            compute_md5=compute_md5 and f.checksum_md5 is None,
            checksum_md5=f.checksum_md5,
            storage_tier=f.storage_tier,
        )
        counts["files"]["inserted" if created else "existing"] += 1

    return counts, pid


# --------------------------------------------------------------------------- #
# Public entrypoint
# --------------------------------------------------------------------------- #

def import_project_from_dir(
    root: str | Path,
    *,
    dry_run: bool = False,
    force: bool = False,
    compute_md5: bool = False,
    skip_disk_check: bool = False,
    log_dir: str | Path | None = None,
) -> ImportReport:
    """Validate and (unless *dry_run*) import the project under *root*.

    Raises :class:`ProjectImportError` with the collected error list when
    validation fails, or when the project already exists and *force* is
    False. On success returns an :class:`ImportReport` with row counts
    per table.

    Idempotency: with *force* a re-run on the same folder re-uses
    existing rows via the ``get_or_create`` / ``get_or_register`` /
    ``set_*`` semantics of the CRUD layer; the report distinguishes
    ``inserted`` from ``existing`` (or, for metadata, ``inserted``
    vs ``updated`` vs ``unchanged``).
    """
    start = time.monotonic()
    bundle = loader.load_project_dir(root)
    report = ImportReport(
        project_name=bundle.project.project_name,
        dry_run=dry_run,
        force=force,
        warnings=list(bundle.warnings),
    )

    errors: list[str] = []
    errors.extend(_validate_schema(bundle))
    errors.extend(_validate_referential(bundle))
    if not skip_disk_check:
        errors.extend(_validate_disk(bundle))

    # The collision check needs a cursor; run it in its own short read
    # transaction so we can present all validation errors before deciding
    # whether to commit or refuse.
    with transaction() as cur:
        existing_project = projects.get_by_name(cur, bundle.project.project_name)
        if existing_project is not None and not force:
            errors.append(
                f"project {bundle.project.project_name!r} already exists "
                f"(project_id={existing_project['project_id']}); rerun with "
                "force=True to append."
            )
        errors.extend(_validate_db_collisions(cur, bundle))

    if errors:
        report.errors = errors
        report.duration_seconds = time.monotonic() - start
        _write_log(log_dir, report)
        raise ProjectImportError(
            f"import refused: {len(errors)} validation error(s)", errors,
        )

    if dry_run:
        report.duration_seconds = time.monotonic() - start
        _write_log(log_dir, report)
        return report

    with transaction() as cur:
        counts, pid = _commit(cur, bundle, compute_md5=compute_md5)
    report.counts = counts
    report.project_id = pid
    report.duration_seconds = time.monotonic() - start
    _write_log(log_dir, report)
    return report


def _write_log(log_dir: str | Path | None, report: ImportReport) -> None:
    """Append the JSON report to ``<log_dir>/<ts>_<project>.log``."""
    target = Path(log_dir).expanduser() if log_dir else (
        Path.home() / ".labdb" / "imports"
    )
    target.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%dT%H%M%S")
    safe = "".join(c if c.isalnum() or c in "._-" else "_"
                   for c in report.project_name)
    path = target / f"{ts}_{safe}.log"
    path.write_text(
        json.dumps(report.to_dict(), indent=2, default=str),
        encoding="utf-8",
    )
