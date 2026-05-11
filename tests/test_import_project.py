"""Integration tests for the master import (require a live MariaDB).

Each test builds a project folder in ``tmp_path`` (YAML + 3 CSVs +
files/manifest.csv pointing at real files under a fake archive root)
and exercises :func:`import_project_from_dir`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dbmaria_utils import (
    execute,
    projects,
    transaction,
)
from dbmaria_utils._import import (
    ProjectImportError,
    import_project_from_dir,
)

from tests._helpers import wipe_all

# PyYAML is required to read project.yaml; skip the whole module when missing.
pytest.importorskip("yaml")


# --------------------------------------------------------------------------- #
# Helpers to build a project folder on disk
# --------------------------------------------------------------------------- #

PROJECT_YAML = """\
project_name: {name}
description: a test project
pi_name: Dr. Test
"""

SUBJECTS_CSV = """\
subject_code,sex,origin
S_A,F,PL
S_B,M,AT
"""

VISITS_CSV = """\
subject_code,timepoint,group_test,age,meta_bmi,meta_smoker
S_A,baseline,ctrl,30,22.5,false
S_A,m3,ctrl,31,22.8,false
S_B,baseline,tx,45,28.0,true
"""

SAMPLES_CSV = """\
sample_name,subject_code,timepoint,sample_type,sqr,sqrp,library,antibody_class,meta_well,meta_passed_qc
{prefix}_SA1,S_A,baseline,sample,Q1,Q1,libA,IgG,A01,true
{prefix}_SA2,S_A,m3,sample,Q2,Q2,libA,IgG,A02,true
{prefix}_SB1,S_B,baseline,input,Q1,Q1,libA,,B01,false
"""

MANIFEST_CSV = """\
sample_name,file_path,file_type,storage_tier
{prefix}_SA1,{archive}/{prefix}_SA1_R1.fastq.gz,fastq_r1,archive
{prefix}_SA1,{archive}/{prefix}_SA1.bam,bam,archive
{prefix}_SB1,{work}/{prefix}_SB1.beer.tsv,beer_norm,work
"""


def _build_project(tmp_path: Path, *, name: str, prefix: str) -> Path:
    """Materialize a project folder under tmp_path/<prefix>/.

    Returns the project root. Files referenced by the manifest are
    written to <tmp_path>/archive and <tmp_path>/work — these are NOT
    the real LABDB tier roots, but the importer does not enforce tier
    policy (that's enforced by :func:`files.register`; the importer uses
    raw INSERTs... actually it uses files.get_or_register which DOES
    validate). To avoid that, we point LABDB_ARCHIVE_ROOT / WORK_ROOT
    at our tmp directories via monkeypatch in each test.
    """
    archive = tmp_path / "archive"
    work = tmp_path / "work"
    archive.mkdir(exist_ok=True)
    work.mkdir(exist_ok=True)
    for filename in (f"{prefix}_SA1_R1.fastq.gz", f"{prefix}_SA1.bam"):
        (archive / filename).write_bytes(b"\x00")
    (work / f"{prefix}_SB1.beer.tsv").write_bytes(b"\x00")

    proj = tmp_path / prefix
    proj.mkdir()
    (proj / "project.yaml").write_text(PROJECT_YAML.format(name=name))
    (proj / "subjects.csv").write_text(SUBJECTS_CSV)
    (proj / "visits.csv").write_text(VISITS_CSV)
    (proj / "samples.csv").write_text(SAMPLES_CSV.format(prefix=prefix))
    (proj / "files").mkdir()
    (proj / "files" / "manifest.csv").write_text(
        MANIFEST_CSV.format(prefix=prefix, archive=archive, work=work)
    )
    return proj


@pytest.fixture
def fake_tier_roots(tmp_path, monkeypatch):
    """Point archive/work roots at tmp_path so files.register accepts paths."""
    archive = tmp_path / "archive"
    work = tmp_path / "work"
    archive.mkdir(exist_ok=True)
    work.mkdir(exist_ok=True)
    monkeypatch.setenv("LABDB_ARCHIVE_ROOT", str(archive))
    monkeypatch.setenv("LABDB_WORK_ROOT", str(work))
    return archive, work


@pytest.fixture
def clean_db(_init_pool):
    with transaction() as cur:
        wipe_all(cur)
    yield
    with transaction() as cur:
        wipe_all(cur)


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #

def test_happy_path_import(tmp_path, clean_db, fake_tier_roots):
    proj = _build_project(tmp_path, name="IMP_OK", prefix="OK")
    report = import_project_from_dir(proj, log_dir=tmp_path / "logs")
    assert report.project_name == "IMP_OK"
    assert report.project_id is not None
    assert report.counts["projects"]["inserted"] == 1
    assert report.counts["subjects"]["inserted"] == 2
    assert report.counts["visits"]["inserted"] == 3
    assert report.counts["samples"]["inserted"] == 3
    assert report.counts["files"]["inserted"] == 3
    # 3 visits × 2 keys (bmi, smoker) + 3 samples × 2 keys (well, passed_qc)
    # but SB1 visit baseline already has meta from SA1 visit, so total
    # visit_metadata inserts = 3 visits × 2 keys = 6, sample = 3×2 = 6 → 12.
    assert report.counts["metadata"]["inserted"] == 12

    rows = execute(
        "SELECT COUNT(*) AS n FROM subjects WHERE project_id = ?",
        (report.project_id,),
    )
    assert rows[0]["n"] == 2

    # Log file was written.
    logs = list((tmp_path / "logs").glob("*.log"))
    assert len(logs) == 1
    payload = json.loads(logs[0].read_text(encoding="utf-8"))
    assert payload["project_name"] == "IMP_OK"
    assert payload["counts"]["samples"]["inserted"] == 3


def test_dry_run_writes_nothing(tmp_path, clean_db, fake_tier_roots):
    proj = _build_project(tmp_path, name="IMP_DRY", prefix="DRY")
    report = import_project_from_dir(proj, dry_run=True, log_dir=tmp_path / "logs")
    assert report.dry_run is True
    assert report.project_id is None
    assert report.counts == {}
    rows = execute(
        "SELECT COUNT(*) AS n FROM projects WHERE project_name = ?",
        ("IMP_DRY",),
    )
    assert rows[0]["n"] == 0


def test_rerun_without_force_refuses(tmp_path, clean_db, fake_tier_roots):
    proj = _build_project(tmp_path, name="IMP_RERUN", prefix="RR")
    import_project_from_dir(proj, log_dir=tmp_path / "logs")
    with pytest.raises(ProjectImportError) as exc_info:
        import_project_from_dir(proj, log_dir=tmp_path / "logs")
    assert any("already exists" in e for e in exc_info.value.errors)


def test_rerun_with_force_is_idempotent(tmp_path, clean_db, fake_tier_roots):
    proj = _build_project(tmp_path, name="IMP_IDEMP", prefix="ID")
    first = import_project_from_dir(proj, log_dir=tmp_path / "logs")
    second = import_project_from_dir(
        proj, force=True, log_dir=tmp_path / "logs",
    )
    assert second.project_id == first.project_id
    # Re-run should not insert any new subjects/visits/samples/files.
    assert second.counts["subjects"]["inserted"] == 0
    assert second.counts["subjects"]["existing"] == 2
    assert second.counts["samples"]["inserted"] == 0
    assert second.counts["files"]["inserted"] == 0
    # Metadata values are identical → all "unchanged" upserts.
    assert second.counts["metadata"]["inserted"] == 0
    assert second.counts["metadata"]["unchanged"] == 12


# --------------------------------------------------------------------------- #
# Validation failures
# --------------------------------------------------------------------------- #

def test_bad_sex_value_collects_all_errors(tmp_path, clean_db, fake_tier_roots):
    proj = _build_project(tmp_path, name="IMP_BADSEX", prefix="BS")
    # Corrupt subjects.csv
    (proj / "subjects.csv").write_text(
        "subject_code,sex,origin\nS_A,Z,PL\nS_B,Q,AT\n"
    )
    with pytest.raises(ProjectImportError) as exc:
        import_project_from_dir(proj, log_dir=tmp_path / "logs")
    assert len(exc.value.errors) >= 2
    assert all("sex=" in e for e in exc.value.errors if "sex=" in e)
    rows = execute(
        "SELECT COUNT(*) AS n FROM projects WHERE project_name = ?",
        ("IMP_BADSEX",),
    )
    assert rows[0]["n"] == 0


def test_missing_subject_referenced_by_visit(tmp_path, clean_db, fake_tier_roots):
    proj = _build_project(tmp_path, name="IMP_REF", prefix="REF")
    # Reference a subject that doesn't exist in subjects.csv.
    (proj / "visits.csv").write_text(
        "subject_code,timepoint,group_test,age\n"
        "S_GHOST,baseline,ctrl,30\n"
    )
    with pytest.raises(ProjectImportError) as exc:
        import_project_from_dir(proj, log_dir=tmp_path / "logs")
    assert any("S_GHOST" in e for e in exc.value.errors)


def test_duplicate_sample_name_within_csv(tmp_path, clean_db, fake_tier_roots):
    proj = _build_project(tmp_path, name="IMP_DUP", prefix="DUP")
    (proj / "samples.csv").write_text(
        "sample_name,subject_code,timepoint,sample_type,sqr,sqrp,library\n"
        "DUP_X,S_A,baseline,sample,Q,Q,libA\n"
        "DUP_X,S_A,m3,sample,Q,Q,libA\n"
    )
    with pytest.raises(ProjectImportError) as exc:
        import_project_from_dir(proj, log_dir=tmp_path / "logs")
    assert any("duplicate sample_name" in e for e in exc.value.errors)


def test_missing_file_on_disk(tmp_path, clean_db, fake_tier_roots):
    proj = _build_project(tmp_path, name="IMP_NOFILE", prefix="NF")
    # Rewrite manifest with a non-existent path.
    (proj / "files" / "manifest.csv").write_text(
        "sample_name,file_path,file_type\n"
        "NF_SA1,/no/such/file.fastq.gz,fastq_r1\n"
    )
    with pytest.raises(ProjectImportError) as exc:
        import_project_from_dir(proj, log_dir=tmp_path / "logs")
    assert any("does not exist on disk" in e for e in exc.value.errors)


def test_skip_disk_check_lets_dry_run_succeed(
    tmp_path, clean_db, fake_tier_roots,
):
    proj = _build_project(tmp_path, name="IMP_SKIP", prefix="SK")
    (proj / "files" / "manifest.csv").write_text(
        "sample_name,file_path,file_type\n"
        "SK_SA1,/no/such/file.fastq.gz,fastq_r1\n"
    )
    # Disk check is skipped AND it's a dry run → no commit, no failure.
    report = import_project_from_dir(
        proj, dry_run=True, skip_disk_check=True, log_dir=tmp_path / "logs",
    )
    assert report.dry_run is True


def test_sample_name_collides_with_other_project(
    tmp_path, clean_db, fake_tier_roots,
):
    """Same sample_name in two different projects is blocked even with --force."""
    proj_a = _build_project(tmp_path, name="COLL_A", prefix="CA")
    import_project_from_dir(proj_a, log_dir=tmp_path / "logs")

    # Build a separate project that re-uses one of COLL_A's sample_names.
    proj_b = tmp_path / "coll_b"
    proj_b.mkdir()
    (proj_b / "project.yaml").write_text(PROJECT_YAML.format(name="COLL_B"))
    (proj_b / "subjects.csv").write_text(
        "subject_code,sex\nS_X,F\n"
    )
    (proj_b / "visits.csv").write_text(
        "subject_code,timepoint,group_test,age\nS_X,t0,ctrl,20\n"
    )
    (proj_b / "samples.csv").write_text(
        "sample_name,subject_code,timepoint,sample_type,sqr,sqrp,library\n"
        # CA_SA1 already lives in project COLL_A.
        "CA_SA1,S_X,t0,sample,Q,Q,libA\n"
    )
    (proj_b / "files").mkdir()
    (proj_b / "files" / "manifest.csv").write_text(
        "sample_name,file_path,file_type\n"
    )
    with pytest.raises(ProjectImportError) as exc:
        import_project_from_dir(
            proj_b, force=True, log_dir=tmp_path / "logs",
        )
    assert any("already belongs to project_id" in e for e in exc.value.errors)


def test_missing_required_file(tmp_path, clean_db):
    """project.yaml absent → FileNotFoundError (not ProjectImportError)."""
    proj = tmp_path / "broken"
    proj.mkdir()
    (proj / "subjects.csv").write_text("subject_code,sex\nS,F\n")
    with pytest.raises(FileNotFoundError):
        import_project_from_dir(proj, log_dir=tmp_path / "logs")


def test_missing_required_column(tmp_path, clean_db, fake_tier_roots):
    proj = _build_project(tmp_path, name="IMP_BADCOL", prefix="BC")
    (proj / "subjects.csv").write_text("subject_code\nS_A\n")
    with pytest.raises(ValueError) as exc:
        import_project_from_dir(proj, log_dir=tmp_path / "logs")
    assert "missing required columns" in str(exc.value)
