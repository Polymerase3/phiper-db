"""Shared helpers for the test suite."""

from __future__ import annotations


def wipe_all(cur) -> None:
    """Truncate every domain table in dependency-safe order.

    Since schema 003 subjects no longer FK to projects — the
    subject→visit→sample lineage is independent of project membership
    (which lives in ``project_samples``). So deleting projects alone
    leaves orphan subjects behind, and because ``subject_code`` is now
    globally UNIQUE that would break the next test's inserts.

    Order:
      1. ``sample_files`` (``ON DELETE RESTRICT`` — must go first).
      2. ``subjects`` — cascades visits → samples → *_metadata and the
         ``project_samples`` rows keyed on ``sample_id``.
      3. ``projects`` — cascades any remaining ``project_samples`` rows.
    """
    cur.execute("DELETE FROM sample_files")
    cur.execute("DELETE FROM subjects")
    cur.execute("DELETE FROM projects")
