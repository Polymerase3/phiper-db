"""CSV / YAML schema declarations and value coercion helpers.

Each CSV file has a fixed set of required columns plus optional ones; any
column that starts with ``meta_`` is treated as a typed metadata key (the
prefix is stripped to produce the stored ``key_name``). Type inference
order: int → float → bool ('true'/'false'/'1'/'0') → str. Empty cells
are treated as "no metadata for this row" (no INSERT).
"""

from __future__ import annotations

from typing import Any

# Required + optional columns per CSV (excluding meta_* keys).
SUBJECTS_REQUIRED = ("subject_code", "sex")
SUBJECTS_OPTIONAL = ("origin",)

VISITS_REQUIRED = ("subject_code", "timepoint", "group_test", "age")
VISITS_OPTIONAL: tuple[str, ...] = ()

SAMPLES_REQUIRED = (
    "sample_name", "subject_code", "timepoint",
    "sample_type", "sqr", "sqrp", "library",
)
SAMPLES_OPTIONAL = ("antibody_class",)

MANIFEST_REQUIRED = ("sample_name", "file_path", "file_type")
MANIFEST_OPTIONAL = ("storage_tier", "checksum_md5")

# Schema enums (mirror the DB ENUM/CHECK constraints).
ALLOWED_SEX = frozenset({"M", "F"})
ALLOWED_SAMPLE_TYPE = frozenset({"sample", "mockIP", "input", "anchor"})
ALLOWED_FILE_TYPE = frozenset({
    "fastq_r1", "fastq_r2", "fastq_single", "bam", "counts",
    "beer_norm", "zigp_norm", "edger_norm",
})
ALLOWED_STORAGE_TIER = frozenset({"work", "archive", "scratch", "external"})

PROJECT_YAML_REQUIRED = ("project_name",)
PROJECT_YAML_OPTIONAL = ("description", "pi_name")

META_PREFIX = "meta_"


# --------------------------------------------------------------------------- #
# Value coercion
# --------------------------------------------------------------------------- #

def coerce_metadata_value(raw: str) -> Any | None:
    """Coerce a raw CSV cell to an int / float / bool / str.

    Empty / whitespace-only cells return ``None``, meaning "no metadata
    entry" — the caller should skip the row rather than insert NULL,
    because :func:`metadata.set_*` rejects None.
    """
    if raw is None:
        return None
    s = raw.strip()
    if s == "":
        return None
    # bool BEFORE int: '1' and '0' would otherwise parse as int.
    low = s.lower()
    if low in ("true", "false"):
        return low == "true"
    # int
    try:
        return int(s)
    except ValueError:
        pass
    # float
    try:
        return float(s)
    except ValueError:
        pass
    return s


def coerce_int(raw: str, *, field: str) -> int:
    """Parse an int, raising :class:`ValueError` with the field name on failure."""
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}: expected int, got {raw!r}") from exc


def split_columns(
    header: list[str], required: tuple[str, ...], optional: tuple[str, ...],
) -> tuple[list[str], list[str]]:
    """Return ``(non_meta_extra, meta_keys)`` given a CSV header.

    *non_meta_extra* — columns not in required/optional and not prefixed
    with ``meta_``. These are silently ignored by the loader but reported
    to the user as warnings (so a typo like ``smaple_name`` doesn't
    silently drop data).

    *meta_keys* — the bare key names with ``meta_`` stripped.
    """
    known = set(required) | set(optional)
    extra: list[str] = []
    meta: list[str] = []
    for col in header:
        if col in known:
            continue
        if col.startswith(META_PREFIX):
            key = col[len(META_PREFIX):]
            if key:
                meta.append(key)
        else:
            extra.append(col)
    return extra, meta
