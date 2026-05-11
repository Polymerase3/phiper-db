"""High-level composite operations built on the per-table CRUD modules.

Each workflow takes an *optional* cursor (``cur=None``). When the caller
provides a cursor, the workflow piggybacks on the caller's transaction —
all writes are part of the same atomic unit, and a later exception in
the caller's block rolls them back. When ``cur`` is ``None``, the
workflow opens its own :func:`dbmaria_utils.transaction` block, so the
multi-step operation is still atomic when called standalone (e.g. from a
notebook):

    # Notebook style — workflow owns the transaction:
    sid, vid = workflows.register_subject_with_visit(
        project_id=pid,
        subject_code="S1", sex="F",
        timepoint="baseline", group_test="ctrl", age=42,
        visit_metadata={"bmi": 24.1, "smoker": False},
    )

    # Composed inside a larger transaction:
    with transaction() as cur:
        sid, vid = workflows.register_subject_with_visit(cur, ...)
        for spec in sample_specs:
            workflows.register_sample_with_files(cur, visit_id=vid, **spec)
"""

from __future__ import annotations

from contextlib import nullcontext
from typing import Any

from dbmaria_utils import files, metadata, samples, subjects, visits
from dbmaria_utils.connection import transaction


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #

def _cur_ctx(cur):
    """Return a context manager yielding a usable cursor.

    When *cur* is None, open a fresh :func:`transaction`. Otherwise wrap
    the caller-provided cursor in :class:`contextlib.nullcontext` so the
    same ``with`` block works in both cases without us double-closing
    the caller's transaction.
    """
    if cur is None:
        return transaction()
    return nullcontext(cur)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def register_subject_with_visit(
    cur=None,
    *,
    project_id: int,
    subject_code: str,
    sex: str,
    origin: str | None = None,
    timepoint: str,
    group_test: str,
    age: int,
    visit_metadata: dict[str, Any] | None = None,
) -> tuple[int, int]:
    """Idempotently create a subject and its first visit. Atomic.

    Returns ``(subject_id, visit_id)``. Uses
    :func:`subjects.get_or_create` and :func:`visits.get_or_create`, so
    re-running with the same natural keys (``(project_id, subject_code)``
    and ``(subject_id, timepoint)``) yields the same IDs without
    duplicating rows. Existing rows are NOT updated by this helper — to
    change ``sex``/``origin``/``group_test``/``age`` on a row that
    already exists, call the CRUD ``update`` directly.

    *visit_metadata* (optional) is upserted via
    :func:`metadata.set_visit`; values must be one of int/float/bool/str.
    A ``None`` value raises ``ValueError`` (per metadata module
    contract) — pass an empty dict to skip metadata altogether.

    *timepoint* must be non-NULL because the underlying
    :func:`visits.get_or_create` rejects NULL timepoints (the UNIQUE on
    ``visits`` does not deduplicate NULLs).
    """
    with _cur_ctx(cur) as c:
        subject_id, _ = subjects.get_or_create(
            c, project_id, subject_code, sex, origin=origin,
        )
        visit_id, _ = visits.get_or_create(
            c, subject_id, timepoint, group_test, age,
        )
        if visit_metadata:
            for k, v in visit_metadata.items():
                metadata.set_visit(c, visit_id, k, v)
    return subject_id, visit_id


def register_sample_with_files(
    cur=None,
    *,
    visit_id: int,
    sample_name: str,
    sample_type: str,
    sqr: str,
    sqrp: str,
    library: str,
    antibody_class: str | None = None,
    sample_metadata: dict[str, Any] | None = None,
    files_spec: list[dict[str, Any]] | None = None,
    compute_md5: bool = False,
) -> tuple[int, list[int]]:
    """Idempotently create a sample, its metadata, and its files. Atomic.

    Returns ``(sample_id, [file_id, ...])``. The sample is upserted via
    :func:`samples.get_or_create` keyed on the globally-UNIQUE
    ``sample_name``; existing samples keep their original ``visit_id``
    even when called with a different one.

    *sample_metadata* (optional) is upserted via :func:`metadata.set_sample`.

    *files_spec* (optional) is a list of dicts; each dict is forwarded
    to :func:`files.get_or_register` with keys:

        - ``file_path`` (required)
        - ``file_type`` (required)
        - ``storage_tier`` (optional)
        - ``checksum_md5`` (optional)
        - ``compute_md5`` (optional; falls back to the workflow-level
          *compute_md5* when absent)

    Any disk/path validation error from :mod:`files` is raised inside
    the transaction, which rolls back the sample insert too — the unit
    is the sample-plus-files bundle.
    """
    with _cur_ctx(cur) as c:
        sample_id, _ = samples.get_or_create(
            c, visit_id, sample_name, sample_type, sqr, sqrp, library,
            antibody_class=antibody_class,
        )
        if sample_metadata:
            for k, v in sample_metadata.items():
                metadata.set_sample(c, sample_id, k, v)
        file_ids: list[int] = []
        for spec in files_spec or []:
            spec_md5 = spec.get("compute_md5", compute_md5)
            file_id, _ = files.get_or_register(
                c,
                sample_id,
                spec["file_path"],
                spec["file_type"],
                compute_md5=spec_md5,
                checksum_md5=spec.get("checksum_md5"),
                storage_tier=spec.get("storage_tier"),
            )
            file_ids.append(file_id)
    return sample_id, file_ids
