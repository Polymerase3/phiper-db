#!/usr/bin/env python
"""Prepare bulk_import master CSVs from the legacy migration folder.

Usage:
    python scripts/prepare_migration.py <migration_dir> <output_dir> [--lisc-root PATH]

Inputs (inside <migration_dir>):
    Overview_SQRs(Project info).csv   project descriptions / PI names
    meta/*_metadata.csv               per-sample metadata (17 projects)

Outputs (inside <output_dir>):
    projects.csv
    subjects.csv
    visits.csv          (includes meta_* columns for extra phenotypic fields)
    samples.csv
    files/manifest.csv
    _warnings.txt       rows that needed special handling

LISC file paths are constructed as:
    <lisc_root>/counts/{SampleName}.count.gz   → file_type=counts
    <lisc_root>/zigp/{SampleName}.csv          → file_type=zigp_norm
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

DEFAULT_LISC_ROOT = "/lisc/data/work/ccr"
STORAGE_TIER = "work"

# Tokens at the end of a SampleName that encode the library composition.
KNOWN_LIBS: frozenset[str] = frozenset({"A", "T", "C2", "C1", "v0", "v1", "s"})

# Base columns present in every meta file (not written as meta_* fields).
BASE_COLS = frozenset({
    "SampleName", "group_test", "Sex", "Age",
    "origin", "project", "SQR", "SQRP", "timepoint",
})

# Projects with a timepoint column — require per-project subject extraction.
LONGITUDINAL = frozenset({"FAB-HCC", "PD-NED", "PIC_MUW"})

# Regex for longitudinal subject extraction.
_FAB_RE  = re.compile(r"^(.+?)(BL\d*|W\d+|FU\d*)$", re.IGNORECASE)
_PDNED_RE = re.compile(r"^(P\d+)(BL|FU)$", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _extract_library(sample_name: str) -> str:
    """Collect trailing library-code tokens (A, T, C2, ...) from a SampleName."""
    tokens = sample_name.split("_")
    lib_tokens: list[str] = []
    for tok in reversed(tokens):
        if tok in KNOWN_LIBS:
            lib_tokens.append(tok)
        else:
            break
    return "_".join(reversed(lib_tokens))


def _extract_subject(project: str, sample_name: str) -> str:
    """Return the subject_code for a given sample.

    Cross-sectional projects: subject_code = full SampleName (1 sample = 1 subject).
    Longitudinal projects: per-project regex on token at index 2.
    """
    if project not in LONGITUDINAL:
        return sample_name

    tokens = sample_name.split("_")
    if len(tokens) < 3:
        return sample_name

    identifier = tokens[2]

    if project == "FAB-HCC":
        m = _FAB_RE.match(identifier)
        return m.group(1) if m else identifier

    if project == "PD-NED":
        m = _PDNED_RE.match(identifier)
        return m.group(1) if m else identifier

    if project == "PIC_MUW":
        return identifier  # e.g. PIC1, PIC2

    return sample_name


def _parse_age(raw: str, label: str, warnings: list[str]) -> str | None:
    """Parse age to nearest integer, handling comma-decimal notation."""
    s = raw.strip().replace(",", ".")
    if not s or s.upper() in ("NA", "N/A", ""):
        warnings.append(f"{label}: age is {raw!r}, will fail DB validation — fix manually")
        return raw.strip() or "0"
    try:
        return str(round(float(s)))
    except ValueError:
        warnings.append(f"{label}: cannot parse age {raw!r} — kept as-is")
        return raw.strip()


def _sanitize_key(col: str) -> str:
    """Turn a column name into a safe meta key (lowercase, no spaces/hyphens)."""
    return re.sub(r"[^a-z0-9_]", "_", col.strip().lower())


def _read_overview(path: Path) -> dict[str, tuple[str | None, str | None]]:
    """Return {project_name: (description, pi_name)} from the overview CSV."""
    result: dict[str, tuple[str | None, str | None]] = {}
    if not path.exists():
        return result
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh, delimiter=";")
        for row in reader:
            name = (row.get("Project") or "").strip()
            if not name:
                continue
            desc = (row.get("Project description") or "").strip() or None
            pi   = (row.get("Main contact person") or "").strip() or None
            result[name] = (desc, pi)
    return result


# --------------------------------------------------------------------------- #
# Core processing
# --------------------------------------------------------------------------- #

def process_meta_files(
    meta_dir: Path,
    overview: dict[str, tuple[str | None, str | None]],
    lisc_root: str,
    warnings: list[str],
    drop_na: bool = False,
) -> tuple[
    list[dict],   # projects rows
    list[dict],   # subjects rows (deduplicated)
    list[dict],   # visits rows   (deduplicated)
    list[dict],   # samples rows
    list[dict],   # manifest rows
    list[str],    # extra meta key names (sanitized, union across all projects)
]:
    projects_rows: list[dict] = []
    subjects_rows: list[dict] = []
    visits_rows:   list[dict] = []
    samples_rows:  list[dict] = []
    manifest_rows: list[dict] = []

    # Track extra (phenotypic) meta columns across all projects.
    all_meta_keys: list[str] = []
    seen_meta_keys: set[str] = set()

    # Deduplication sets.
    seen_projects:  set[str] = set()
    seen_subjects:  set[tuple[str, str]] = set()   # (project, subject_code)
    seen_visits:    set[tuple[str, str, str]] = set()  # (project, subject_code, timepoint)
    seen_samples:   set[str] = set()  # sample_name (globally unique)

    for meta_file in sorted(meta_dir.glob("*_metadata.csv")):
        with meta_file.open(encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh, delimiter=";")
            fieldnames = list(reader.fieldnames or [])

            # Detect extra columns for this file → meta_* keys.
            file_meta_keys: list[str] = []
            for col in fieldnames:
                if col in BASE_COLS:
                    continue
                key = _sanitize_key(col)
                if not key:
                    continue
                file_meta_keys.append((col, key))
                if key not in seen_meta_keys:
                    seen_meta_keys.add(key)
                    all_meta_keys.append(key)

            has_timepoint = "timepoint" in fieldnames

            for row_num, row in enumerate(reader, start=2):
                sample_name = (row.get("SampleName") or "").strip()
                project     = (row.get("project")    or "").strip()

                if not sample_name or not project:
                    warnings.append(
                        f"{meta_file.name} row {row_num}: empty SampleName or project — skipped"
                    )
                    continue

                # ── PROJECT ──────────────────────────────────────────────
                if project not in seen_projects:
                    seen_projects.add(project)
                    desc, pi = overview.get(project, (None, None))
                    projects_rows.append({
                        "project_name": project,
                        "description":  desc or "",
                        "pi_name":       pi  or "",
                    })

                # ── SUBJECT ──────────────────────────────────────────────
                subject_code = _extract_subject(project, sample_name)
                sex    = (row.get("Sex")    or "").strip()
                origin = (row.get("origin") or "").strip() or ""
                age_raw = (row.get("Age") or "").strip()

                bad_sex = sex not in ("M", "F")
                bad_age = age_raw.upper() in ("NA", "N/A", "")

                if bad_sex or bad_age:
                    label = f"{meta_file.name} row {row_num} ({sample_name!r})"
                    reasons = []
                    if bad_sex:
                        reasons.append(f"Sex={sex!r}")
                    if bad_age:
                        reasons.append(f"Age={age_raw!r}")
                    msg = f"{label}: {', '.join(reasons)} — "
                    if drop_na:
                        warnings.append(msg + "dropped (--drop-na)")
                        continue
                    else:
                        warnings.append(msg + "kept, will fail DB validation")

                subj_key = (project, subject_code)
                if subj_key not in seen_subjects:
                    seen_subjects.add(subj_key)
                    subjects_rows.append({
                        "project_name":  project,
                        "subject_code":  subject_code,
                        "sex":           sex,
                        "origin":        origin,
                    })

                # ── VISIT ────────────────────────────────────────────────
                timepoint  = (row.get("timepoint") or "").strip() if has_timepoint else "baseline"
                group_test = (row.get("group_test") or "").strip()
                age        = _parse_age(
                    age_raw,
                    f"{meta_file.name} row {row_num}",
                    warnings,
                )

                # Extra phenotypic columns → meta_* values for this visit row.
                visit_meta: dict[str, Any] = {}
                for col, key in file_meta_keys:
                    val = (row.get(col) or "").strip()
                    if val and val.upper() not in ("NA", "N/A"):
                        visit_meta[key] = val

                visit_key = (project, subject_code, timepoint)
                if visit_key not in seen_visits:
                    seen_visits.add(visit_key)
                    visit_row: dict[str, Any] = {
                        "project_name":  project,
                        "subject_code":  subject_code,
                        "timepoint":     timepoint,
                        "group_test":    group_test,
                        "age":           age,
                    }
                    visit_row.update({f"meta_{k}": visit_meta.get(k, "") for k in seen_meta_keys})
                    visits_rows.append(visit_row)

                # ── SAMPLE ───────────────────────────────────────────────
                if sample_name in seen_samples:
                    warnings.append(
                        f"{meta_file.name} row {row_num}: duplicate sample_name "
                        f"{sample_name!r} — skipped"
                    )
                    continue
                seen_samples.add(sample_name)

                sqr   = (row.get("SQR")   or "").strip()
                sqrp  = (row.get("SQRP")  or "").strip()
                lib   = _extract_library(sample_name)

                samples_rows.append({
                    "project_name":   project,
                    "sample_name":    sample_name,
                    "subject_code":   subject_code,
                    "timepoint":      timepoint,
                    "sample_type":    "sample",
                    "sqr":            sqr,
                    "sqrp":           sqrp,
                    "library":        lib,
                    "antibody_class": "",
                })

                # ── MANIFEST (counts + zigp) ──────────────────────────────
                manifest_rows.append({
                    "project_name": project,
                    "sample_name":  sample_name,
                    "file_path":    f"{lisc_root}/counts/{sample_name}.count.gz",
                    "file_type":    "counts",
                    "storage_tier": STORAGE_TIER,
                    "checksum_md5": "",
                })
                manifest_rows.append({
                    "project_name": project,
                    "sample_name":  sample_name,
                    "file_path":    f"{lisc_root}/zigp/{sample_name}.csv",
                    "file_type":    "zigp_norm",
                    "storage_tier": STORAGE_TIER,
                    "checksum_md5": "",
                })

    # Back-fill missing meta_* columns in visit rows written before a new key
    # was discovered (keys are added to all_meta_keys as files are processed).
    for vr in visits_rows:
        for k in all_meta_keys:
            col = f"meta_{k}"
            if col not in vr:
                vr[col] = ""

    return projects_rows, subjects_rows, visits_rows, samples_rows, manifest_rows, all_meta_keys


# --------------------------------------------------------------------------- #
# Writers
# --------------------------------------------------------------------------- #

def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="prepare_migration",
        description="Convert legacy migration data to bulk_import master CSVs.",
    )
    p.add_argument("migration_dir", type=Path,
                   help="Root of the migrations/ folder.")
    p.add_argument("output_dir", type=Path,
                   help="Destination folder for master CSVs (created if absent).")
    p.add_argument("--lisc-root", default=DEFAULT_LISC_ROOT,
                   help=f"Base path on LISC (default: {DEFAULT_LISC_ROOT}).")
    p.add_argument("--drop-na", action="store_true",
                   help="Drop rows where Sex or Age is NA instead of keeping them.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    migration_dir: Path = args.migration_dir
    output_dir:    Path = args.output_dir
    lisc_root:     str  = args.lisc_root.rstrip("/")

    meta_dir     = migration_dir / "meta"
    overview_csv = migration_dir / "Overview_SQRs(Project info).csv"

    for path in (migration_dir, meta_dir):
        if not path.is_dir():
            print(f"ERROR: directory not found: {path}", file=sys.stderr)
            return 2

    print(f"Reading overview: {overview_csv.name}")
    overview = _read_overview(overview_csv)

    print(f"Processing {len(list(meta_dir.glob('*_metadata.csv')))} meta files...")
    warnings: list[str] = []
    (
        projects_rows,
        subjects_rows,
        visits_rows,
        samples_rows,
        manifest_rows,
        meta_keys,
    ) = process_meta_files(meta_dir, overview, lisc_root, warnings, drop_na=args.drop_na)

    # ── Write CSVs ────────────────────────────────────────────────────────────

    _write_csv(
        output_dir / "projects.csv",
        ["project_name", "description", "pi_name"],
        projects_rows,
    )

    _write_csv(
        output_dir / "subjects.csv",
        ["project_name", "subject_code", "sex", "origin"],
        subjects_rows,
    )

    visit_fields = (
        ["project_name", "subject_code", "timepoint", "group_test", "age"]
        + [f"meta_{k}" for k in meta_keys]
    )
    _write_csv(output_dir / "visits.csv", visit_fields, visits_rows)

    _write_csv(
        output_dir / "samples.csv",
        ["project_name", "sample_name", "subject_code", "timepoint",
         "sample_type", "sqr", "sqrp", "library", "antibody_class"],
        samples_rows,
    )

    _write_csv(
        output_dir / "files" / "manifest.csv",
        ["project_name", "sample_name", "file_path", "file_type",
         "storage_tier", "checksum_md5"],
        manifest_rows,
    )

    # ── Warnings file ─────────────────────────────────────────────────────────
    warn_path = output_dir / "_warnings.txt"
    if warnings:
        warn_path.write_text("\n".join(warnings) + "\n", encoding="utf-8")
        print(f"  {len(warnings)} warning(s) written to {warn_path}")
    else:
        warn_path.unlink(missing_ok=True)

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\nOutput: {output_dir}/")
    print(f"  projects.csv   {len(projects_rows):>5} rows")
    print(f"  subjects.csv   {len(subjects_rows):>5} rows")
    print(f"  visits.csv     {len(visits_rows):>5} rows  ({len(meta_keys)} meta_* columns)")
    print(f"  samples.csv    {len(samples_rows):>5} rows")
    print(f"  manifest.csv   {len(manifest_rows):>5} rows  ({len(manifest_rows)//2} samples × 2 file types)")
    print(f"  warnings       {len(warnings):>5}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
