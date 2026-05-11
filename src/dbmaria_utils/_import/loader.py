"""Read a project folder into typed in-memory records.

The runner consumes the structures produced here and performs validation
+ commit. Loading itself is forgiving — it does not check enum values or
referential integrity, only structural things (required columns present,
file readable, YAML parseable).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dbmaria_utils._import import schema


@dataclass
class ProjectMeta:
    project_name: str
    description: str | None = None
    pi_name: str | None = None


@dataclass
class SubjectRow:
    subject_code: str
    sex: str
    origin: str | None
    row_num: int  # 1-based row in subjects.csv (for error reporting)


@dataclass
class VisitRow:
    subject_code: str
    timepoint: str
    group_test: str
    age: str  # kept as raw str; coerced during validation so errors report cleanly
    metadata: dict[str, Any]
    row_num: int


@dataclass
class SampleRow:
    sample_name: str
    subject_code: str
    timepoint: str
    sample_type: str
    sqr: str
    sqrp: str
    library: str
    antibody_class: str | None
    metadata: dict[str, Any]
    row_num: int


@dataclass
class FileRow:
    sample_name: str
    file_path: str
    file_type: str
    storage_tier: str | None
    checksum_md5: str | None
    row_num: int


@dataclass
class ProjectBundle:
    """Everything read from a project folder, pre-validation."""
    root: Path
    project: ProjectMeta
    subjects: list[SubjectRow] = field(default_factory=list)
    visits: list[VisitRow] = field(default_factory=list)
    samples: list[SampleRow] = field(default_factory=list)
    files: list[FileRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #

def load_project_dir(root: str | Path) -> ProjectBundle:
    """Read all required files from *root* and return a :class:`ProjectBundle`.

    Raises :class:`FileNotFoundError` for missing required files and
    :class:`ValueError` for missing required columns. Unknown columns
    produce warnings stored on the bundle.
    """
    root_path = Path(root)
    if not root_path.is_dir():
        raise FileNotFoundError(f"project directory not found: {root_path}")

    bundle = ProjectBundle(
        root=root_path,
        project=_load_project_yaml(root_path / "project.yaml"),
    )
    bundle.subjects, sub_warn = _load_subjects(root_path / "subjects.csv")
    bundle.visits, vis_warn = _load_visits(root_path / "visits.csv")
    bundle.samples, sam_warn = _load_samples(root_path / "samples.csv")
    bundle.files, fil_warn = _load_manifest(root_path / "files" / "manifest.csv")
    bundle.warnings = sub_warn + vis_warn + sam_warn + fil_warn
    return bundle


def _load_project_yaml(path: Path) -> ProjectMeta:
    if not path.exists():
        raise FileNotFoundError(f"missing required file: {path}")
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - import guard
        raise ImportError(
            "PyYAML is required to read project.yaml; install with "
            "`pip install pyyaml`"
        ) from exc
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: expected a mapping at top level")
    missing = [k for k in schema.PROJECT_YAML_REQUIRED if k not in data]
    if missing:
        raise ValueError(
            f"{path.name}: missing required keys: {missing}"
        )
    return ProjectMeta(
        project_name=str(data["project_name"]).strip(),
        description=data.get("description"),
        pi_name=data.get("pi_name"),
    )


def _open_csv(path: Path) -> tuple[csv.DictReader, Any]:
    """Open a CSV for DictReader iteration. Caller closes the file."""
    if not path.exists():
        raise FileNotFoundError(f"missing required file: {path}")
    fh = path.open(encoding="utf-8", newline="")
    reader = csv.DictReader(fh)
    if reader.fieldnames is None:
        fh.close()
        raise ValueError(f"{path.name}: empty file or no header row")
    return reader, fh


def _check_required(header: list[str], required: tuple[str, ...], path: Path) -> None:
    missing = [c for c in required if c not in header]
    if missing:
        raise ValueError(f"{path.name}: missing required columns: {missing}")


def _meta_dict(row: dict[str, str], meta_keys: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in meta_keys:
        v = schema.coerce_metadata_value(row.get(schema.META_PREFIX + k))
        if v is not None:
            out[k] = v
    return out


def _warn_extras(extras: list[str], filename: str) -> list[str]:
    if not extras:
        return []
    return [
        f"{filename}: unknown column(s) ignored: {extras}; "
        "did you mean meta_<key>?"
    ]


def _load_subjects(path: Path) -> tuple[list[SubjectRow], list[str]]:
    reader, fh = _open_csv(path)
    try:
        _check_required(list(reader.fieldnames or []), schema.SUBJECTS_REQUIRED, path)
        extras, _ = schema.split_columns(
            list(reader.fieldnames or []),
            schema.SUBJECTS_REQUIRED,
            schema.SUBJECTS_OPTIONAL,
        )
        rows: list[SubjectRow] = []
        for i, r in enumerate(reader, start=2):
            rows.append(
                SubjectRow(
                    subject_code=r["subject_code"].strip(),
                    sex=r["sex"].strip(),
                    origin=(r.get("origin") or "").strip() or None,
                    row_num=i,
                )
            )
        return rows, _warn_extras(extras, path.name)
    finally:
        fh.close()


def _load_visits(path: Path) -> tuple[list[VisitRow], list[str]]:
    reader, fh = _open_csv(path)
    try:
        header = list(reader.fieldnames or [])
        _check_required(header, schema.VISITS_REQUIRED, path)
        extras, meta_keys = schema.split_columns(
            header, schema.VISITS_REQUIRED, schema.VISITS_OPTIONAL,
        )
        rows: list[VisitRow] = []
        for i, r in enumerate(reader, start=2):
            rows.append(
                VisitRow(
                    subject_code=r["subject_code"].strip(),
                    timepoint=r["timepoint"].strip(),
                    group_test=r["group_test"].strip(),
                    age=r["age"].strip(),
                    metadata=_meta_dict(r, meta_keys),
                    row_num=i,
                )
            )
        return rows, _warn_extras(extras, path.name)
    finally:
        fh.close()


def _load_samples(path: Path) -> tuple[list[SampleRow], list[str]]:
    reader, fh = _open_csv(path)
    try:
        header = list(reader.fieldnames or [])
        _check_required(header, schema.SAMPLES_REQUIRED, path)
        extras, meta_keys = schema.split_columns(
            header, schema.SAMPLES_REQUIRED, schema.SAMPLES_OPTIONAL,
        )
        rows: list[SampleRow] = []
        for i, r in enumerate(reader, start=2):
            rows.append(
                SampleRow(
                    sample_name=r["sample_name"].strip(),
                    subject_code=r["subject_code"].strip(),
                    timepoint=r["timepoint"].strip(),
                    sample_type=r["sample_type"].strip(),
                    sqr=r["sqr"].strip(),
                    sqrp=r["sqrp"].strip(),
                    library=r["library"].strip(),
                    antibody_class=(r.get("antibody_class") or "").strip() or None,
                    metadata=_meta_dict(r, meta_keys),
                    row_num=i,
                )
            )
        return rows, _warn_extras(extras, path.name)
    finally:
        fh.close()


def _load_manifest(path: Path) -> tuple[list[FileRow], list[str]]:
    if not path.exists():
        # Empty manifest is allowed; the project may register files later.
        return [], []
    reader, fh = _open_csv(path)
    try:
        header = list(reader.fieldnames or [])
        _check_required(header, schema.MANIFEST_REQUIRED, path)
        extras, _ = schema.split_columns(
            header, schema.MANIFEST_REQUIRED, schema.MANIFEST_OPTIONAL,
        )
        rows: list[FileRow] = []
        for i, r in enumerate(reader, start=2):
            rows.append(
                FileRow(
                    sample_name=r["sample_name"].strip(),
                    file_path=r["file_path"].strip(),
                    file_type=r["file_type"].strip(),
                    storage_tier=(r.get("storage_tier") or "").strip() or None,
                    checksum_md5=(r.get("checksum_md5") or "").strip() or None,
                    row_num=i,
                )
            )
        return rows, _warn_extras(extras, path.name)
    finally:
        fh.close()
