#!/usr/bin/env python
"""Bulk import: one flat folder with per-table master CSVs → multiple projects.

Usage:
    python scripts/bulk_import.py /path/to/migration [--dry-run] [--force]
                                   [--compute-md5] [--skip-disk-check]
                                   [--log-dir PATH]

Master CSV layout
-----------------
  projects.csv        project_name, [description], [pi_name]
  subjects.csv        project_name, subject_code, sex, [origin]
  visits.csv          project_name, subject_code, timepoint, group_test, age,
                      [meta_*]
  samples.csv         project_name, sample_name, subject_code, timepoint,
                      sample_type, sqr, sqrp, library, [antibody_class], [meta_*]
  files/manifest.csv  project_name, sample_name, file_path, file_type,
                      [storage_tier], [checksum_md5]   (optional file)

The project_name column in each CSV ties rows to a project declared in
projects.csv. Rows referencing an undeclared project_name are skipped with a
warning. Each project goes through the standard validation + atomic commit
pipeline; a failure in one project does not abort the others.

Exit codes:
  0  all projects imported (or dry-run passed)
  2  one or more projects failed validation / import
  3  unexpected runtime error
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from dbmaria_utils._import import schema as _schema
from dbmaria_utils._import.loader import (
    FileRow,
    ProjectBundle,
    ProjectMeta,
    SampleRow,
    SubjectRow,
    VisitRow,
)
from dbmaria_utils._import.runner import (
    ImportReport,
    _commit,
    _validate_db_collisions,
    _validate_disk,
    _validate_referential,
    _validate_schema,
    _write_log,
)
from dbmaria_utils import projects as projects_mod
from dbmaria_utils.connection import close_pool, init_pool, transaction


# --------------------------------------------------------------------------- #
# CSV helpers
# --------------------------------------------------------------------------- #

def _open_csv(path: Path) -> tuple[csv.DictReader, Any]:
    if not path.exists():
        raise FileNotFoundError(f"missing required file: {path}")
    fh = path.open(encoding="utf-8", newline="")
    reader = csv.DictReader(fh)
    if reader.fieldnames is None:
        fh.close()
        raise ValueError(f"{path.name}: empty file or no header row")
    return reader, fh


def _require_cols(header: list[str], required: tuple[str, ...], path: Path) -> None:
    missing = [c for c in required if c not in header]
    if missing:
        raise ValueError(f"{path.name}: missing required column(s): {missing}")


def _meta_keys(header: list[str]) -> list[str]:
    prefix = _schema.META_PREFIX
    return [col[len(prefix):] for col in header if col.startswith(prefix) and col[len(prefix):]]


def _meta_dict(row: dict[str, str], meta_keys: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in meta_keys:
        v = _schema.coerce_metadata_value(row.get(_schema.META_PREFIX + k))
        if v is not None:
            out[k] = v
    return out


# --------------------------------------------------------------------------- #
# Master CSV readers — each returns a dict keyed by project_name
# --------------------------------------------------------------------------- #

def _read_projects(path: Path) -> dict[str, ProjectMeta]:
    reader, fh = _open_csv(path)
    try:
        _require_cols(list(reader.fieldnames or []), ("project_name",), path)
        result: dict[str, ProjectMeta] = {}
        for row in reader:
            name = row["project_name"].strip()
            if not name:
                continue
            result[name] = ProjectMeta(
                project_name=name,
                description=(row.get("description") or "").strip() or None,
                pi_name=(row.get("pi_name") or "").strip() or None,
            )
        return result
    finally:
        fh.close()


def _read_subjects(path: Path) -> dict[str, list[SubjectRow]]:
    reader, fh = _open_csv(path)
    try:
        header = list(reader.fieldnames or [])
        _require_cols(header, ("project_name", "subject_code", "sex"), path)
        by: dict[str, list[SubjectRow]] = defaultdict(list)
        for i, row in enumerate(reader, start=2):
            pname = row["project_name"].strip()
            if not pname:
                continue
            by[pname].append(SubjectRow(
                subject_code=row["subject_code"].strip(),
                sex=row["sex"].strip(),
                origin=(row.get("origin") or "").strip() or None,
                row_num=i,
            ))
        return dict(by)
    finally:
        fh.close()


def _read_visits(path: Path) -> dict[str, list[VisitRow]]:
    reader, fh = _open_csv(path)
    try:
        header = list(reader.fieldnames or [])
        _require_cols(
            header,
            ("project_name", "subject_code", "timepoint", "group_test", "age"),
            path,
        )
        mkeys = _meta_keys(header)
        by: dict[str, list[VisitRow]] = defaultdict(list)
        for i, row in enumerate(reader, start=2):
            pname = row["project_name"].strip()
            if not pname:
                continue
            by[pname].append(VisitRow(
                subject_code=row["subject_code"].strip(),
                timepoint=row["timepoint"].strip(),
                group_test=row["group_test"].strip(),
                age=row["age"].strip(),
                metadata=_meta_dict(row, mkeys),
                row_num=i,
            ))
        return dict(by)
    finally:
        fh.close()


def _read_samples(path: Path) -> dict[str, list[SampleRow]]:
    reader, fh = _open_csv(path)
    try:
        header = list(reader.fieldnames or [])
        _require_cols(
            header,
            ("project_name", "sample_name", "subject_code", "timepoint",
             "sample_type", "sqr", "sqrp", "library"),
            path,
        )
        mkeys = _meta_keys(header)
        by: dict[str, list[SampleRow]] = defaultdict(list)
        for i, row in enumerate(reader, start=2):
            pname = row["project_name"].strip()
            if not pname:
                continue
            by[pname].append(SampleRow(
                sample_name=row["sample_name"].strip(),
                subject_code=row["subject_code"].strip(),
                timepoint=row["timepoint"].strip(),
                sample_type=row["sample_type"].strip(),
                sqr=row["sqr"].strip(),
                sqrp=row["sqrp"].strip(),
                library=row["library"].strip(),
                antibody_class=(row.get("antibody_class") or "").strip() or None,
                metadata=_meta_dict(row, mkeys),
                row_num=i,
            ))
        return dict(by)
    finally:
        fh.close()


def _read_manifest(path: Path) -> dict[str, list[FileRow]]:
    if not path.exists():
        return {}
    reader, fh = _open_csv(path)
    try:
        header = list(reader.fieldnames or [])
        _require_cols(
            header,
            ("project_name", "sample_name", "file_path", "file_type"),
            path,
        )
        by: dict[str, list[FileRow]] = defaultdict(list)
        for i, row in enumerate(reader, start=2):
            pname = row["project_name"].strip()
            if not pname:
                continue
            by[pname].append(FileRow(
                sample_name=row["sample_name"].strip(),
                file_path=row["file_path"].strip(),
                file_type=row["file_type"].strip(),
                storage_tier=(row.get("storage_tier") or "").strip() or None,
                checksum_md5=(row.get("checksum_md5") or "").strip() or None,
                row_num=i,
            ))
        return dict(by)
    finally:
        fh.close()


# --------------------------------------------------------------------------- #
# Bundle assembly
# --------------------------------------------------------------------------- #

def load_bundles(root: Path) -> list[ProjectBundle]:
    """Read all master CSVs and return one ProjectBundle per project."""
    projects_meta = _read_projects(root / "projects.csv")
    subjects_by   = _read_subjects(root / "subjects.csv")
    visits_by     = _read_visits(root / "visits.csv")
    samples_by    = _read_samples(root / "samples.csv")
    files_by      = _read_manifest(root / "files" / "manifest.csv")

    declared = set(projects_meta)
    for label, by in [
        ("subjects.csv", subjects_by),
        ("visits.csv",   visits_by),
        ("samples.csv",  samples_by),
        ("files/manifest.csv", files_by),
    ]:
        for pname in by:
            if pname not in declared:
                print(
                    f"WARNING: {label} contains rows for project_name {pname!r} "
                    "which is not declared in projects.csv — rows skipped.",
                    file=sys.stderr,
                )

    return [
        ProjectBundle(
            root=root,
            project=pmeta,
            subjects=subjects_by.get(pname, []),
            visits=visits_by.get(pname, []),
            samples=samples_by.get(pname, []),
            files=files_by.get(pname, []),
        )
        for pname, pmeta in projects_meta.items()
    ]


# --------------------------------------------------------------------------- #
# Per-bundle import (mirrors runner.import_project_from_dir, no raise on error)
# --------------------------------------------------------------------------- #

def _import_bundle(
    bundle: ProjectBundle,
    *,
    dry_run: bool,
    force: bool,
    compute_md5: bool,
    skip_disk_check: bool,
    log_dir: Path | None,
) -> ImportReport:
    start = time.monotonic()
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

    with transaction() as cur:
        existing = projects_mod.get_by_name(cur, bundle.project.project_name)
        if existing is not None and not force:
            errors.append(
                f"project {bundle.project.project_name!r} already exists "
                f"(project_id={existing['project_id']}); rerun with --force to append."
            )
        errors.extend(_validate_db_collisions(cur, bundle))

    if errors:
        report.errors = errors
        report.duration_seconds = time.monotonic() - start
        _write_log(log_dir, report)
        return report

    if dry_run:
        report.duration_seconds = time.monotonic() - start
        _write_log(log_dir, report)
        return report

    with transaction() as cur:
        counts, pid = _commit(cur, bundle, compute_md5=compute_md5, skip_disk_check=skip_disk_check)
    report.counts = counts
    report.project_id = pid
    report.duration_seconds = time.monotonic() - start
    _write_log(log_dir, report)
    return report


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="bulk_import",
        description="Bulk-import multiple projects from a flat folder of master CSVs.",
    )
    p.add_argument("migration_dir", type=Path,
                   help="Folder containing projects.csv, subjects.csv, etc.")
    p.add_argument("--dry-run", action="store_true",
                   help="Validate everything; do not write to the database.")
    p.add_argument("--force", action="store_true",
                   help="Allow re-import of projects that already exist (appends / updates).")
    p.add_argument("--compute-md5", action="store_true",
                   help="Compute MD5 for manifest files that have no checksum_md5.")
    p.add_argument("--skip-disk-check", action="store_true",
                   help="Skip on-disk existence check for manifest file paths.")
    p.add_argument("--log-dir", type=Path, default=None,
                   help="Directory for per-project JSON report logs (default: ~/.labdb/imports/).")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    try:
        bundles = load_bundles(args.migration_dir)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR reading migration folder: {exc}", file=sys.stderr)
        return 2

    if not bundles:
        print("No projects found in projects.csv.", file=sys.stderr)
        return 2

    print(f"Found {len(bundles)} project(s). "
          f"{'Dry-run — no writes.' if args.dry_run else 'Importing...'}\n")

    try:
        init_pool()
    except Exception as exc:
        print(f"ERROR: could not connect to database: {exc}", file=sys.stderr)
        return 3

    reports: list[ImportReport] = []
    try:
        for bundle in bundles:
            print(f"  [{bundle.project.project_name}] ...", end=" ", flush=True)
            try:
                report = _import_bundle(
                    bundle,
                    dry_run=args.dry_run,
                    force=args.force,
                    compute_md5=args.compute_md5,
                    skip_disk_check=args.skip_disk_check,
                    log_dir=args.log_dir,
                )
            except Exception as exc:
                print(f"UNEXPECTED ERROR: {exc!r}", file=sys.stderr)
                return 3
            reports.append(report)
            if report.errors:
                print(f"FAILED ({len(report.errors)} error(s))")
            elif args.dry_run:
                print("DRY-RUN OK")
            else:
                counts = report.counts
                n_sub  = counts.get("subjects", {}).get("inserted", 0)
                n_vis  = counts.get("visits",   {}).get("inserted", 0)
                n_sam  = counts.get("samples",  {}).get("inserted", 0)
                n_fil  = counts.get("files",    {}).get("inserted", 0)
                print(f"OK  "
                      f"subjects={n_sub} visits={n_vis} samples={n_sam} files={n_fil}")
    finally:
        close_pool()

    failed  = [r for r in reports if r.errors]
    ok      = len(reports) - len(failed)

    print(f"\n{'─' * 60}")
    print(f"  {len(reports)} project(s) total   {ok} OK   {len(failed)} failed")

    if failed:
        print()
        for r in failed:
            print(f"  {r.project_name}:")
            for e in r.errors:
                print(f"    - {e}")
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
