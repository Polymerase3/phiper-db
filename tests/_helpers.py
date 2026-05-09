"""Shared helpers for the test suite."""

from __future__ import annotations


def wipe_all(cur) -> None:
    """Truncate every domain table in dependency-safe order.

    ``sample_files`` uses ``ON DELETE RESTRICT``, so a bare
    ``DELETE FROM projects`` fails whenever any file row exists. Always
    clear ``sample_files`` first, then let the project cascade do the rest.
    """
    cur.execute("DELETE FROM sample_files")
    cur.execute("DELETE FROM projects")
