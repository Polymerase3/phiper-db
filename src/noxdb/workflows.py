"""High-level composite operations built on the per-table CRUD modules.

Each workflow takes an *optional* cursor (``cur=None``). When the caller
provides a cursor, the workflow piggybacks on the caller's transaction —
all writes are part of the same atomic unit, and a later exception in
the caller's block rolls them back. When ``cur`` is ``None``, the
workflow opens its own :func:`noxdb.transaction` block, so the
multi-step operation is still atomic when called standalone (e.g. from a
notebook):

    # Notebook style — workflow owns the transaction:
    sid, vid = workflows.register_subject_with_visit(
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

from noxdb import files, metadata, samples, subjects, visits
from noxdb.connection import transaction


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
    subject_code: str,
    sex: str,
    origin: str | None = None,
    timepoint: str,
    group_test: str,
    age: int,
    visit_metadata: dict[str, Any] | None = None,
) -> tuple[int, int]:
    """Idempotently create a subject and its first visit. Atomic.

    Uses [`subjects.get_or_create`][noxdb.subjects.get_or_create]
    and [`visits.get_or_create`][noxdb.visits.get_or_create], so
    re-running with the same natural keys yields the same IDs without
    duplicating rows. Existing rows are NOT updated by this helper —
    call the CRUD ``update`` directly to change attributes of a row
    that already exists.

    Args:
        cur: Optional cursor from `transaction()`. When ``None`` this
            workflow opens its own transaction so the multi-step
            operation stays atomic when called standalone.
        subject_code: Globally unique subject code.
        sex: ``'M'`` or ``'F'``. Used only when inserting a new subject.
        origin: Used only when inserting a new subject.
        timepoint: Visit timepoint. Must be non-NULL — the underlying
            `visits.get_or_create` rejects NULL timepoints.
        group_test: Used only when inserting a new visit.
        age: Used only when inserting a new visit.
        visit_metadata: Optional ``{key: value}`` upserted via
            [`metadata.set_visit`][noxdb.metadata.set_visit].
            Values must be ``int`` / ``float`` / ``bool`` / ``str``;
            ``None`` values raise.

    Returns:
        ``(subject_id, visit_id)``.

    Raises:
        ValueError: If ``timepoint`` is ``None``, or if any
            ``visit_metadata`` value is ``None``.
    """
    with _cur_ctx(cur) as c:
        subject_id, _ = subjects.get_or_create(
            c, subject_code, sex, origin=origin,
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

    The sample is upserted via
    [`samples.get_or_create`][noxdb.samples.get_or_create] keyed
    on the globally-UNIQUE ``sample_name``; existing samples keep their
    original ``visit_id`` even when called with a different one.

    Any disk/path validation error from
    [`files`][noxdb.files] is raised inside the transaction,
    which rolls back the sample insert too — the unit is the
    sample-plus-files bundle.

    Args:
        cur: Optional cursor from `transaction()`. When ``None`` this
            workflow opens its own transaction.
        visit_id: Parent visit (used only when inserting a new sample).
        sample_name: Globally unique sample name.
        sample_type: See [`samples.create`][noxdb.samples.create]
            for allowed values. Used only on insert.
        sqr: Used only on insert.
        sqrp: Used only on insert.
        library: Used only on insert.
        antibody_class: Used only on insert.
        sample_metadata: Optional ``{key: value}`` upserted via
            [`metadata.set_sample`][noxdb.metadata.set_sample].
        files_spec: Optional list of dicts forwarded to
            [`files.get_or_register`][noxdb.files.get_or_register].
            Recognised keys per entry: ``file_path`` (required),
            ``file_type`` (required), ``storage_tier``,
            ``checksum_md5``, ``compute_md5`` (falls back to the
            workflow-level ``compute_md5`` when absent).
        compute_md5: Default for entries in ``files_spec`` that don't
            set their own.

    Returns:
        ``(sample_id, [file_id, ...])`` in the same order as
        ``files_spec``.
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
