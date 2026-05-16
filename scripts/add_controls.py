#!/usr/bin/env python
"""Append control samples from Overview_SQRs.csv to migration_import/ CSVs.

Redesigned for the **schema 003 cross-project samples** model.

Controls (mockIP, anchor, NC) have **no project of their own** — schema
003 deleted the dedicated control projects. A control belongs to every
study project whose real samples share its plate, expressed through the
``project_samples`` junction. The importer (``noxdb._import.runner``)
auto-links any control already in the DB to a bundle when their SQR+SQRP
match, and the 003 migration backfilled the same relationship for
historical data.

So instead of inventing control projects, this script:

  * builds a ``(SQR, SQRP) -> {study project_name}`` map from the real
    (``sample_type == 'sample'``) rows already in ``samples.csv``;
  * for each mockIP/anchor/NC control, emits its subject/visit/sample/
    file rows **once per study project that shares its plate**, so each
    of those project bundles creates the control and links it via
    ``project_samples``. A control whose plate matches no study project
    is reported as an orphan and skipped (it would otherwise be a
    sample attached to no project — invisible to every query).
  * ``input`` samples still go to the dedicated ``input`` project, which
    schema 003 keeps (``queries.list_inputs`` depends on it).

SQR/SQRP are taken verbatim (stripped, no zero-padding) to stay
byte-identical with the values prepare_migration.py wrote for the study
samples, otherwise the importer's SQR+SQRP plate match would miss.

Safe to re-run: already-present sample_names are silently skipped.

Usage:
    python scripts/add_controls.py <migration_dir> <import_dir> [--lisc-root PATH]
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

DEFAULT_LISC_ROOT = "/lisc/data/work/ccr"
STORAGE_TIER = "work"

# The only control project schema 003 keeps. mockIP/anchor/NC are linked
# to study projects via project_samples instead of owning a project.
INPUT_PROJECT = "input"
INPUT_PROJECT_DESCRIPTION = "Control samples (input DNA)"

KNOWN_LIBS: frozenset[str] = frozenset({"A", "T", "C2", "C1", "v0", "v1", "s"})


def _extract_library(sample_name: str) -> str:
    tokens = sample_name.split("_")
    lib_tokens: list[str] = []
    for tok in reversed(tokens):
        if tok in KNOWN_LIBS:
            lib_tokens.append(tok)
        else:
            break
    return "_".join(reversed(lib_tokens))


def _detect_sample_type(sample_name: str) -> str:
    lower = sample_name.lower()
    if "anchor" in lower:
        return "anchor"
    if "mock" in lower:
        return "mockIP"
    if "NC" in sample_name:
        return "NC"
    if "input" in lower:
        return "input"
    return "sample"


def _strip_sample_prefix(name: str) -> str:
    return name[7:] if name.startswith("Sample_") else name


def _plate(sqr: str, sqrp: str) -> tuple[str, str]:
    """Normalised plate key. Verbatim-stripped to match study samples."""
    return (sqr.strip(), sqrp.strip())


def _read_csv_set(path: Path, *cols: str) -> set[tuple]:
    if not path.exists():
        return set()
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        return {tuple(row[c] for c in cols) for row in reader if all(c in row for c in cols)}


def _build_plate_to_projects(samples_csv: Path) -> dict[tuple[str, str], set[str]]:
    """Map ``(SQR, SQRP) -> {study project_name}`` from real samples.

    Only ``sample_type == 'sample'`` rows define plate ownership — this
    mirrors the 003 migration's Backfill 3, which joined controls to
    study samples on ``study_sm.sample_type = 'sample'``.
    """
    out: dict[tuple[str, str], set[str]] = {}
    if not samples_csv.exists():
        return out
    with samples_csv.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            if (row.get("sample_type") or "").strip() != "sample":
                continue
            key = _plate(row.get("sqr", ""), row.get("sqrp", ""))
            out.setdefault(key, set()).add((row.get("project_name") or "").strip())
    return out


def _read_visits_header(path: Path) -> list[str]:
    if not path.exists():
        return ["project_name", "subject_code", "timepoint", "group_test", "age"]
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        return list(reader.fieldnames or [])


def _append_rows(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    if not rows:
        return
    with path.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="add_controls",
        description="Append control samples from Overview_SQRs.csv to import CSVs.",
    )
    p.add_argument("migration_dir", type=Path,
                   help="Root of migrations/ (contains Overview_SQRs.csv).")
    p.add_argument("import_dir", type=Path,
                   help="Destination folder with existing master CSVs (migration_import/).")
    p.add_argument("--lisc-root", default=DEFAULT_LISC_ROOT)
    args = p.parse_args(argv)

    migration_dir: Path = args.migration_dir
    import_dir: Path    = args.import_dir
    lisc_root: str      = args.lisc_root.rstrip("/")

    overview_path = migration_dir / "Overview_SQRs.csv"

    if not overview_path.exists():
        print(f"ERROR: not found: {overview_path}", file=sys.stderr)
        return 2

    samples_csv = import_dir / "samples.csv"
    plate_to_projects = _build_plate_to_projects(samples_csv)
    if not plate_to_projects:
        print(
            f"ERROR: no real ('sample') rows in {samples_csv} — run "
            "prepare_migration first so controls can be matched to a plate.",
            file=sys.stderr,
        )
        return 2

    # Read existing state for deduplication.
    existing_samples  = {t[0] for t in _read_csv_set(samples_csv, "sample_name")}
    existing_subjects = _read_csv_set(import_dir / "subjects.csv", "project_name", "subject_code")
    existing_visits   = _read_csv_set(import_dir / "visits.csv",   "project_name", "subject_code", "timepoint")
    existing_projects = {t[0] for t in _read_csv_set(import_dir / "projects.csv", "project_name")}
    visits_header     = _read_visits_header(import_dir / "visits.csv")

    new_projects: list[dict] = []
    new_subjects: list[dict] = []
    new_visits:   list[dict] = []
    new_samples:  list[dict] = []
    new_files:    list[dict] = []

    seen_projects: set[str]                  = set()
    seen_subjects: set[tuple[str, str]]      = set()
    seen_visits:   set[tuple[str, str, str]] = set()
    seen_samples:  set[str]                  = set()

    n_skipped = 0
    orphans: list[tuple[str, str, str]] = []  # (sample_name, sqr, sqrp)

    print(f"Scanning {overview_path.name}...")

    with overview_path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh, delimiter=";")
        next(reader, None)  # row 1: section header  (;;;Barcodes…)
        next(reader, None)  # row 2: column names    (SQR#;SQRP#;SampleName…)

        for row in reader:
            if len(row) < 3:
                continue
            sqr      = row[0].strip()
            sqrp     = row[1].strip()
            raw_name = row[2].strip()
            if not raw_name:
                continue

            sample_name = _strip_sample_prefix(raw_name)
            sample_type = _detect_sample_type(sample_name)
            if sample_type == "sample":
                continue

            if sample_name in existing_samples or sample_name in seen_samples:
                n_skipped += 1
                continue

            # ── Target project(s) ─────────────────────────────────────────
            # input → the dedicated 'input' project (kept by schema 003).
            # mockIP/anchor/NC → every study project sharing this plate;
            # the importer links the one physical sample to each via
            # project_samples.
            if sample_type == "input":
                target_projects = [INPUT_PROJECT]
            else:
                target_projects = sorted(
                    pn for pn in plate_to_projects.get(_plate(sqr, sqrp), set())
                    if pn
                )
                if not target_projects:
                    orphans.append((sample_name, sqr, sqrp))
                    continue

            seen_samples.add(sample_name)

            subject_code = sample_name
            timepoint    = "baseline"
            lib          = _extract_library(sample_name)

            for project in target_projects:
                # ── Project (only the 'input' umbrella is ever created) ───
                if (
                    sample_type == "input"
                    and project not in existing_projects
                    and project not in seen_projects
                ):
                    seen_projects.add(project)
                    new_projects.append({
                        "project_name": project,
                        "description":  INPUT_PROJECT_DESCRIPTION,
                        "pi_name":      "",
                    })

                # ── Subject ───────────────────────────────────────────────
                subj_key = (project, subject_code)
                if subj_key not in existing_subjects and subj_key not in seen_subjects:
                    seen_subjects.add(subj_key)
                    new_subjects.append({
                        "project_name": project,
                        "subject_code": subject_code,
                        "sex":          "",
                        "origin":       "",
                    })

                # ── Visit ─────────────────────────────────────────────────
                visit_key = (project, subject_code, timepoint)
                if visit_key not in existing_visits and visit_key not in seen_visits:
                    seen_visits.add(visit_key)
                    visit_row = {k: "" for k in visits_header}
                    visit_row.update({
                        "project_name": project,
                        "subject_code": subject_code,
                        "timepoint":    timepoint,
                        "group_test":   "control",
                        "age":          "",
                    })
                    new_visits.append(visit_row)

                # ── Sample ────────────────────────────────────────────────
                new_samples.append({
                    "project_name":   project,
                    "sample_name":    sample_name,
                    "subject_code":   subject_code,
                    "timepoint":      timepoint,
                    "sample_type":    sample_type,
                    "sqr":            sqr,
                    "sqrp":           sqrp,
                    "library":        lib,
                    "antibody_class": "",
                })

                # ── Files ─────────────────────────────────────────────────
                new_files.append({
                    "project_name": project,
                    "sample_name":  sample_name,
                    "file_path":    f"{lisc_root}/counts/{sample_name}.count.gz",
                    "file_type":    "counts",
                    "storage_tier": STORAGE_TIER,
                    "checksum_md5": "",
                })
                new_files.append({
                    "project_name": project,
                    "sample_name":  sample_name,
                    "file_path":    f"{lisc_root}/zigp/{sample_name}.csv",
                    "file_type":    "zigp_norm",
                    "storage_tier": STORAGE_TIER,
                    "checksum_md5": "",
                })

    # ── Write ─────────────────────────────────────────────────────────────────
    _append_rows(import_dir / "projects.csv",
                 ["project_name", "description", "pi_name"],
                 new_projects)
    _append_rows(import_dir / "subjects.csv",
                 ["project_name", "subject_code", "sex", "origin"],
                 new_subjects)
    _append_rows(import_dir / "visits.csv", visits_header, new_visits)
    _append_rows(samples_csv,
                 ["project_name", "sample_name", "subject_code", "timepoint",
                  "sample_type", "sqr", "sqrp", "library", "antibody_class"],
                 new_samples)
    (import_dir / "files").mkdir(exist_ok=True)
    _append_rows(import_dir / "files" / "manifest.csv",
                 ["project_name", "sample_name", "file_path", "file_type",
                  "storage_tier", "checksum_md5"],
                 new_files)

    print(f"\nDone.")
    print(f"  projects appended : {len(new_projects)}  (input umbrella only)")
    print(f"  subjects appended : {len(new_subjects)}")
    print(f"  visits   appended : {len(new_visits)}")
    print(f"  samples  appended : {len(new_samples)}  (control rows, "
          "duplicated per plate-sharing project)")
    print(f"  files    appended : {len(new_files)}")
    if n_skipped:
        print(f"  skipped (already exist) : {n_skipped}")
    if orphans:
        print(f"  ORPHAN controls (no study project shares the plate) : {len(orphans)}")
        for name, sqr, sqrp in orphans[:20]:
            print(f"    - {name} (SQR={sqr!r} SQRP={sqrp!r})")
        if len(orphans) > 20:
            print(f"    … and {len(orphans) - 20} more")
        print("  These were NOT written. Re-run prepare_migration or check "
              "SQR/SQRP formatting if this is unexpected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
