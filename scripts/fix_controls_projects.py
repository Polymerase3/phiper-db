#!/usr/bin/env python
"""One-off DB cleanup: reassign control subjects to their dedicated projects.

Before this fix, add_controls.py assigned mockIP/anchor/NC subjects to real
study projects based on a flawed SQR→project lookup.  Controls should live
in their own projects ("mockIP", "anchor", "NC") so that reverse lookup via
SQR+SQRP (queries.controls_for_project) works correctly.

This script:
  1. Creates the "mockIP", "anchor", "NC" projects if they don't exist.
  2. Finds all subjects in real study projects whose samples are exclusively
     of a control type (mockIP / anchor / NC).
  3. Reassigns those subjects to their correct control project.

Dry-run by default.  Pass --commit to write to the database.

Usage:
    .venv/bin/python3 scripts/fix_controls_projects.py
    .venv/bin/python3 scripts/fix_controls_projects.py --commit
"""

from __future__ import annotations

import argparse
import sys

sys.path.insert(0, "src")

from dbmaria_utils import init_pool, close_pool, transaction
from dbmaria_utils import projects as projects_mod

CONTROL_TYPES = ("mockIP", "anchor", "NC")

CONTROL_DESCRIPTIONS = {
    "mockIP": "Mock IP control samples",
    "anchor": "Anchor control samples",
    "NC":     "Negative control (NC) samples",
}

# "input" is intentionally excluded — it was already handled correctly.
# "sample" is a real sample type and must never appear here.


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Reassign misplaced control subjects.")
    p.add_argument("--commit", action="store_true",
                   help="Write changes to the database (default: dry-run).")
    args = p.parse_args(argv)
    dry_run = not args.commit

    if dry_run:
        print("DRY-RUN — no changes will be written.  Pass --commit to apply.\n")

    try:
        init_pool()
    except Exception as exc:
        print(f"ERROR: could not connect: {exc}", file=sys.stderr)
        return 3

    try:
        with transaction() as cur:
            # ── 1. Ensure control projects exist ──────────────────────────────
            ctrl_project_ids: dict[str, int] = {}
            for ctype, desc in CONTROL_DESCRIPTIONS.items():
                pid, created = projects_mod.get_or_create(
                    cur, ctype, description=desc
                )
                ctrl_project_ids[ctype] = pid
                status = "created" if created else "already exists"
                print(f"  project {ctype!r}: project_id={pid} ({status})")

            print()

            # ── 2. Find misplaced control subjects ────────────────────────────
            # A subject is misplaced if:
            #   - its project is NOT one of the control projects
            #   - ALL of its samples have a control sample_type
            ctrl_pid_list = list(ctrl_project_ids.values())
            ctrl_pid_ph   = ",".join(["?"] * len(ctrl_pid_list))
            ctype_ph      = ",".join(["?"] * len(CONTROL_TYPES))

            cur.execute(
                f"""
                SELECT sub.subject_id, sub.project_id, s.sample_type
                FROM subjects sub
                JOIN visits v ON v.subject_id = sub.subject_id
                JOIN samples s ON s.visit_id = v.visit_id
                WHERE sub.project_id NOT IN ({ctrl_pid_ph})
                  AND s.sample_type IN ({ctype_ph})
                GROUP BY sub.subject_id, sub.project_id, s.sample_type
                HAVING COUNT(DISTINCT s.sample_type) = 1
                ORDER BY s.sample_type, sub.subject_id
                """,
                tuple(ctrl_pid_list + list(CONTROL_TYPES)),
            )
            rows = cur.fetchall()

            if not rows:
                print("Nothing to fix — no misplaced control subjects found.")
                return 0

            # Group by (current_project_id, target_sample_type)
            by_type: dict[str, list[int]] = {t: [] for t in CONTROL_TYPES}
            for subject_id, _old_pid, sample_type in rows:
                by_type[sample_type].append(subject_id)

            total = sum(len(v) for v in by_type.values())
            print(f"Found {total} misplaced control subject(s):")
            for ctype, ids in by_type.items():
                if ids:
                    print(f"  {ctype}: {len(ids)} subject(s) → project_id={ctrl_project_ids[ctype]}")

            if dry_run:
                print("\nDry-run complete.  Re-run with --commit to apply.")
                return 0

            # ── 3. Reassign ───────────────────────────────────────────────────
            print()
            total_updated = 0
            for ctype, subject_ids in by_type.items():
                if not subject_ids:
                    continue
                new_pid = ctrl_project_ids[ctype]
                id_ph = ",".join(["?"] * len(subject_ids))
                cur.execute(
                    f"UPDATE subjects SET project_id = ? WHERE subject_id IN ({id_ph})",
                    tuple([new_pid] + subject_ids),
                )
                updated = cur.rowcount
                total_updated += updated
                print(f"  {ctype}: updated {updated} subject(s) → project_id={new_pid}")

            print(f"\nDone.  {total_updated} subject(s) reassigned.")

    finally:
        close_pool()

    return 0


if __name__ == "__main__":
    sys.exit(main())
