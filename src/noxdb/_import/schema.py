"""CSV / YAML schema declarations and value coercion helpers.

Each CSV file has a fixed set of required columns plus optional ones; any
column that starts with ``meta_`` is treated as a typed metadata key (the
prefix is stripped to produce the stored ``key_name``). Type inference
order: int → float → bool ('true'/'false'/'1'/'0') → str. Empty cells
are treated as "no metadata for this row" (no INSERT).
"""

from __future__ import annotations

from typing import Any

# Reuse the single plate-id canonicalization chokepoint so the value the
# importer validates is exactly what samples.create will store.
from noxdb.samples import canonical_plate_id

# DB column width for samples.SQR / samples.SQRP (VARCHAR(10)).
_PLATE_MAX_LEN = 10

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
ALLOWED_SAMPLE_TYPE = frozenset({"sample", "mockIP", "input", "anchor", "NC"})
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
    """Coerce a raw CSV cell to an ``int`` / ``float`` / ``bool`` / ``str``.

    Type inference order: bool (``'true'`` / ``'false'``) → int → float
    → str. ``'1'`` / ``'0'`` are NOT treated as bools (they'd otherwise
    parse as int and lose their boolean intent at write time).

    Args:
        raw: The raw cell value.

    Returns:
        The coerced value, or ``None`` for empty / whitespace-only
        cells (meaning "no metadata entry" — the caller should skip
        the row rather than insert NULL, because
        [`metadata.set_visit`][noxdb.metadata.set_visit] /
        [`metadata.set_sample`][noxdb.metadata.set_sample]
        reject ``None``).
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
    """Parse an int with a labelled error on failure.

    Args:
        raw: The raw cell value.
        field: Human-readable identifier (e.g. ``"visits.csv row 4.age"``)
            embedded in the error message so the user can locate the
            bad cell.

    Returns:
        The parsed integer.

    Raises:
        ValueError: If ``raw`` is not parseable as an int.
    """
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}: expected int, got {raw!r}") from exc


def validate_plate_id(raw: str | None, *, field: str) -> tuple[str, str | None]:
    """Validate + canonicalize an SQR / SQRP cell for import.

    Uses the same canonicalization as
    [`samples.create`][noxdb.samples.create], so what the importer
    accepts here is byte-identical to what gets stored — SQR+SQRP
    plate matching can't drift between the two.

    Args:
        raw: The raw cell value.
        field: Human-readable identifier (e.g.
            ``"samples.csv row 4.sqr"``) embedded in messages so the
            user can locate the cell.

    Returns:
        ``(canonical, warning)`` — *canonical* is the value that will
        be stored; *warning* is a human-readable string when
        canonicalization changed the input (whitespace stripped or an
        ``NA``/empty sentinel collapsed), else ``None``.

    Raises:
        ValueError: If the canonical value exceeds the
            ``samples.SQR`` / ``samples.SQRP`` column width (10),
            which would otherwise fail with a cryptic driver error
            mid-commit.
    """
    canon = canonical_plate_id(raw)
    if len(canon) > _PLATE_MAX_LEN:
        raise ValueError(
            f"{field}: {raw!r} is {len(canon)} chars after normalization; "
            f"max is {_PLATE_MAX_LEN}"
        )
    warning = None
    if canon != (raw or "").strip():
        warning = f"{field}: {raw!r} normalized to {canon!r}"
    return canon, warning


def split_columns(
    header: list[str], required: tuple[str, ...], optional: tuple[str, ...],
) -> tuple[list[str], list[str]]:
    """Split a CSV header into known, metadata, and extra columns.

    Args:
        header: List of column names from the CSV header row.
        required: Column names that must be present.
        optional: Column names allowed but not required.

    Returns:
        ``(non_meta_extra, meta_keys)``:

        - ``non_meta_extra`` — columns not in required/optional and
          not prefixed with ``meta_``. These are silently ignored by
          the loader but reported to the user as warnings (so a typo
          like ``smaple_name`` doesn't silently drop data).
        - ``meta_keys`` — the bare metadata key names with the
          ``meta_`` prefix stripped.
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
