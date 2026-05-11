#!/usr/bin/env python
"""CLI entry point: import a project folder into dbmaria_project.

    python scripts/import_project.py /path/to/project_X [--dry-run] [--force]

Exit codes:
    0  success (or successful dry-run)
    2  validation failed / project conflict
    3  unexpected runtime error
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dbmaria_utils._import import ProjectImportError, import_project_from_dir


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="import_project",
        description="Import a project folder (project.yaml + CSVs) into dbmaria_project.",
    )
    p.add_argument("project_dir", type=Path, help="Path to the project folder.")
    p.add_argument(
        "--dry-run", action="store_true",
        help="Validate everything and report; do not write to the database.",
    )
    p.add_argument(
        "--force", action="store_true",
        help="Permit appending into an existing project. Without this flag, "
             "the import refuses when the project_name already exists.",
    )
    p.add_argument(
        "--compute-md5", action="store_true",
        help="Compute MD5 checksums for files whose manifest entry has no "
             "checksum_md5. Slow on large files but recommended for archive tier.",
    )
    p.add_argument(
        "--skip-disk-check", action="store_true",
        help="Skip 'file exists on disk' validation. Useful for dry-runs from "
             "a host where the storage isn't mounted.",
    )
    p.add_argument(
        "--log-dir", type=Path, default=None,
        help="Directory to write the JSON report log (default: ~/.labdb/imports).",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        report = import_project_from_dir(
            args.project_dir,
            dry_run=args.dry_run,
            force=args.force,
            compute_md5=args.compute_md5,
            skip_disk_check=args.skip_disk_check,
            log_dir=args.log_dir,
        )
    except ProjectImportError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        for e in exc.errors:
            print(f"  - {e}", file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - last-resort safety net
        print(f"UNEXPECTED ERROR: {exc!r}", file=sys.stderr)
        return 3
    json.dump(report.to_dict(), sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
