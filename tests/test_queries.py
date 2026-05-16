"""Integration tests for noxdb.queries (require a live MariaDB).

Tests that build a DataFrame call ``pytest.importorskip("pandas")``; the
report-only helpers (``project_summary``, ``integrity_check``) run without
pandas. Test data is wired through the regular CRUD modules; ``sample_files``
rows are inserted via raw SQL because :func:`files.register` validates the
path on disk and we don't want to create real files for every test.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from noxdb import (
    metadata,
    projects,
    queries,
    samples,
    subjects,
    transaction,
    visits,
)

from tests._helpers import wipe_all


# --------------------------------------------------------------------------- #
# Fixture: one fully populated project + a second small project for isolation
# --------------------------------------------------------------------------- #

@pytest.fixture
def populated_project(_init_pool):
    """Seed a project with 2 subjects, 3 visits, 4 samples, mixed metadata,
    and a few raw-SQL file rows. Yields a dict of useful IDs.

    Layout:
        Project QPROJ
          Subject SUBJ_A (M)
            Visit baseline   (group=ctrl, age=30)
              Sample SAMP_A1 (sample, IgG)   files: fastq_r1, fastq_r2
              Sample SAMP_A2 (input)         no files
            Visit m3         (group=ctrl, age=31)
              Sample SAMP_A3 (sample, IgM)   files: bam
          Subject SUBJ_B (F)
            Visit baseline   (group=tx, age=45)
              Sample SAMP_B1 (sample, IgG)   files: beer_norm (work tier)

    Sample metadata: well_position (text), passed_qc (bool), dilution (float)
    Visit metadata: bmi (numeric), smoker (bool)
    """
    with transaction() as cur:
        wipe_all(cur)
        # Second project to make sure queries scope correctly.
        other_pid = projects.create(cur, "OTHER")
        other_sid = subjects.create(cur, "OS1", "M")
        other_vid = visits.create(cur, other_sid, "ctrl", 20, timepoint="t0")
        other_smp = samples.create(
            cur, other_vid, "OTHER_S1", "sample", "X", "X", "libX",
            antibody_class="IgG",
        )
        samples.link_to_project(cur, other_pid, other_smp)

        pid = projects.create(cur, "QPROJ", pi_name="Dr. Q")

        sa = subjects.create(cur, "SUBJ_A", "M", origin="PL")
        sb = subjects.create(cur, "SUBJ_B", "F", origin="AT")

        va_b = visits.create(cur, sa, "ctrl", 30, timepoint="baseline")
        va_m3 = visits.create(cur, sa, "ctrl", 31, timepoint="m3")
        vb_b = visits.create(cur, sb, "tx",  45, timepoint="baseline")

        metadata.set_visit(cur, va_b, "bmi", 22.7)
        metadata.set_visit(cur, va_b, "smoker", False)
        metadata.set_visit(cur, va_m3, "bmi", 23.1)
        metadata.set_visit(cur, vb_b, "bmi", 28.4)
        metadata.set_visit(cur, vb_b, "smoker", True)

        a1 = samples.create(cur, va_b, "SAMP_A1", "sample", "Q1", "Q1", "libA",
                            antibody_class="IgG")
        a2 = samples.create(cur, va_b, "SAMP_A2", "input",  "Q1", "Q1", "libA")
        a3 = samples.create(cur, va_m3, "SAMP_A3", "sample", "Q2", "Q2", "libA",
                            antibody_class="IgM")
        b1 = samples.create(cur, vb_b, "SAMP_B1", "sample", "Q3", "Q3", "libB",
                            antibody_class="IgG")

        for _smp in (a1, a2, a3, b1):
            samples.link_to_project(cur, pid, _smp)

        metadata.set_sample(cur, a1, "well_position", "A01")
        metadata.set_sample(cur, a1, "passed_qc", True)
        metadata.set_sample(cur, a1, "dilution", 1.5)
        metadata.set_sample(cur, a2, "well_position", "A02")
        metadata.set_sample(cur, a2, "passed_qc", False)
        metadata.set_sample(cur, a3, "well_position", "B01")
        metadata.set_sample(cur, b1, "well_position", "C01")
        metadata.set_sample(cur, b1, "passed_qc", True)

        # Raw SQL for files — bypasses files.register() disk validation.
        # All paths are under the default tier roots so integrity_check stays clean.
        for sid, ftype, path, md5, tier in [
            (a1, "fastq_r1",  "/lisc/archive/qproj/SAMP_A1_R1.fastq.gz",
             "0" * 32, "archive"),
            (a1, "fastq_r2",  "/lisc/archive/qproj/SAMP_A1_R2.fastq.gz",
             "1" * 32, "archive"),
            (a3, "bam",       "/lisc/archive/qproj/SAMP_A3.bam",
             "2" * 32, "archive"),
            (b1, "beer_norm", "/lisc/work/qproj/SAMP_B1.beer.tsv",
             None, "work"),
        ]:
            cur.execute(
                "INSERT INTO sample_files "
                "(sample_id, file_type, file_path, checksum_md5, storage_tier) "
                "VALUES (?, ?, ?, ?, ?)",
                (sid, ftype, path, md5, tier),
            )

    yield {
        "project_id": pid,
        "other_project_id": other_pid,
        "subjects": {"A": sa, "B": sb},
        "visits": {"A_baseline": va_b, "A_m3": va_m3, "B_baseline": vb_b},
        "samples": {"A1": a1, "A2": a2, "A3": a3, "B1": b1},
    }

    with transaction() as cur:
        wipe_all(cur)


# --------------------------------------------------------------------------- #
# project_summary / integrity_check — no pandas
# --------------------------------------------------------------------------- #

def test_project_summary_counts(populated_project):
    pid = populated_project["project_id"]
    with transaction() as cur:
        report = queries.project_summary(cur, pid)
    assert report["project_id"] == pid
    assert report["n_subjects"] == 2
    assert report["n_visits"] == 3
    assert report["n_samples"] == 4
    assert report["n_files"] == 4
    assert report["files_by_type"] == {
        "fastq_r1": 1, "fastq_r2": 1, "bam": 1, "beer_norm": 1,
    }


def test_project_summary_other_project_isolated(populated_project):
    other = populated_project["other_project_id"]
    with transaction() as cur:
        report = queries.project_summary(cur, other)
    assert report["n_subjects"] == 1
    assert report["n_samples"] == 1
    assert report["n_files"] == 0


def test_project_summary_unknown_project_zero(populated_project):
    with transaction() as cur:
        report = queries.project_summary(cur, 9_999_999)
    assert report["n_samples"] == 0
    assert report["files_by_type"] == {}


def test_integrity_check_clean_project(populated_project):
    pid = populated_project["project_id"]
    with transaction() as cur:
        report = queries.integrity_check(cur, pid)
    # Only SAMP_A2 has no files in the fixture.
    sample_names = {r["sample_name"] for r in report["samples_without_files"]}
    assert sample_names == {"SAMP_A2"}
    # SAMP_B1's beer_norm has NULL md5 but it's work tier — not flagged.
    # All three archive files in the fixture have md5 set.
    assert report["archive_files_missing_md5"] == []
    assert report["files_outside_tier_root"] == []
    assert report["unknown_file_types"] == []


def test_integrity_check_flags_archive_without_md5(populated_project):
    pid = populated_project["project_id"]
    with transaction() as cur:
        cur.execute(
            "UPDATE sample_files SET checksum_md5 = NULL "
            "WHERE file_path = ?",
            ("/lisc/archive/qproj/SAMP_A3.bam",),
        )
        report = queries.integrity_check(cur, pid)
    paths = [r["file_path"] for r in report["archive_files_missing_md5"]]
    assert paths == ["/lisc/archive/qproj/SAMP_A3.bam"]


def test_integrity_check_flags_scratch_outside_both_roots(populated_project):
    """scratch/external tiers have no enforced root, but a path that lives
    outside *both* LABDB roots is surfaced for audit."""
    pid = populated_project["project_id"]
    with transaction() as cur:
        cur.execute(
            "UPDATE sample_files SET file_path = ?, storage_tier = 'scratch' "
            "WHERE file_path = ?",
            ("/tmp/random/scratch.tsv", "/lisc/work/qproj/SAMP_B1.beer.tsv"),
        )
        report = queries.integrity_check(cur, pid)
    scratch_rows = [
        r for r in report["files_outside_tier_root"] if r.get("tier") == "scratch"
    ]
    assert len(scratch_rows) == 1
    assert scratch_rows[0]["file_path"] == "/tmp/random/scratch.tsv"
    assert "checked_roots" in scratch_rows[0]


def test_integrity_check_does_not_flag_scratch_under_root(populated_project):
    """A scratch path that happens to land under a LABDB root is intentional
    (the row was opted into the escape hatch) and is NOT flagged."""
    pid = populated_project["project_id"]
    with transaction() as cur:
        cur.execute(
            "UPDATE sample_files SET storage_tier = 'scratch' WHERE file_path = ?",
            ("/lisc/work/qproj/SAMP_B1.beer.tsv",),
        )
        report = queries.integrity_check(cur, pid)
    assert all(r.get("tier") != "scratch" for r in report["files_outside_tier_root"])


def test_integrity_check_flags_path_outside_root(populated_project):
    pid = populated_project["project_id"]
    with transaction() as cur:
        cur.execute(
            "UPDATE sample_files SET file_path = ? WHERE file_path = ?",
            ("/elsewhere/file.bam", "/lisc/archive/qproj/SAMP_A3.bam"),
        )
        report = queries.integrity_check(cur, pid)
    bad_paths = [r["file_path"] for r in report["files_outside_tier_root"]]
    assert bad_paths == ["/elsewhere/file.bam"]


# --------------------------------------------------------------------------- #
# samples_for_project — DataFrame helpers
# --------------------------------------------------------------------------- #

def test_samples_for_project_returns_all_samples(populated_project):
    pytest.importorskip("pandas")
    pid = populated_project["project_id"]
    with transaction() as cur:
        df = queries.samples_for_project(cur, pid)
    assert sorted(df["sample_name"].tolist()) == [
        "SAMP_A1", "SAMP_A2", "SAMP_A3", "SAMP_B1",
    ]
    assert set(df.columns) >= {
        "project_id", "subject_id", "subject_code", "visit_id",
        "timepoint", "sample_id", "sample_name", "sample_type",
        "SQR", "SQRP", "library", "antibody_class",
    }


def test_samples_for_project_filters_by_sample_type(populated_project):
    pytest.importorskip("pandas")
    pid = populated_project["project_id"]
    with transaction() as cur:
        df = queries.samples_for_project(cur, pid, sample_type="input")
    assert df["sample_name"].tolist() == ["SAMP_A2"]


def test_samples_for_project_filters_by_file_type(populated_project):
    pytest.importorskip("pandas")
    pid = populated_project["project_id"]
    with transaction() as cur:
        df = queries.samples_for_project(cur, pid, file_type="fastq_r1")
    assert df["sample_name"].tolist() == ["SAMP_A1"]


def test_samples_for_project_has_files_true(populated_project):
    pytest.importorskip("pandas")
    pid = populated_project["project_id"]
    with transaction() as cur:
        df = queries.samples_for_project(cur, pid, has_files=True)
    assert set(df["sample_name"]) == {"SAMP_A1", "SAMP_A3", "SAMP_B1"}


def test_samples_for_project_has_files_false(populated_project):
    pytest.importorskip("pandas")
    pid = populated_project["project_id"]
    with transaction() as cur:
        df = queries.samples_for_project(cur, pid, has_files=False)
    assert df["sample_name"].tolist() == ["SAMP_A2"]


def test_samples_for_project_empty_for_unknown(populated_project):
    pytest.importorskip("pandas")
    with transaction() as cur:
        df = queries.samples_for_project(cur, 9_999_999)
    assert df.empty


# --------------------------------------------------------------------------- #
# samples_with_metadata — EAV pivot
# --------------------------------------------------------------------------- #

def test_samples_with_metadata_pivots_sample_keys(populated_project):
    pd = pytest.importorskip("pandas")
    pid = populated_project["project_id"]
    with transaction() as cur:
        df = queries.samples_with_metadata(cur, pid, include_visit_metadata=False)
    df = df.set_index("sample_name")
    assert df.loc["SAMP_A1", "well_position"] == "A01"
    assert df.loc["SAMP_A1", "passed_qc"] is True
    assert df.loc["SAMP_A1", "dilution"] == Decimal("1.500000")
    # SAMP_A3 has no dilution/passed_qc set → missing in object column → None/NaN.
    assert df.loc["SAMP_A3", "well_position"] == "B01"
    assert pd.isna(df.loc["SAMP_A3", "dilution"])
    assert pd.isna(df.loc["SAMP_A3", "passed_qc"])


def test_samples_with_metadata_includes_visit_keys(populated_project):
    pytest.importorskip("pandas")
    pid = populated_project["project_id"]
    with transaction() as cur:
        df = queries.samples_with_metadata(cur, pid)
    df = df.set_index("sample_name")
    # SAMP_A1, SAMP_A2 share visit baseline (bmi 22.7, smoker False)
    assert df.loc["SAMP_A1", "bmi"] == Decimal("22.700000")
    assert df.loc["SAMP_A2", "bmi"] == Decimal("22.700000")
    assert df.loc["SAMP_A1", "smoker"] is False
    # SAMP_B1 → bmi 28.4, smoker True
    assert df.loc["SAMP_B1", "bmi"] == Decimal("28.400000")
    assert df.loc["SAMP_B1", "smoker"] is True


def test_samples_with_metadata_keys_filter(populated_project):
    pytest.importorskip("pandas")
    pid = populated_project["project_id"]
    with transaction() as cur:
        df = queries.samples_with_metadata(
            cur, pid, keys=["well_position"], include_visit_metadata=False
        )
    assert "well_position" in df.columns
    assert "passed_qc" not in df.columns
    assert "dilution" not in df.columns


def test_samples_with_metadata_collision_prefixed_visit(_init_pool):
    """When a key exists in both sample_metadata and visit_metadata, the
    visit-side column is renamed with a ``visit_`` prefix to avoid collision."""
    pytest.importorskip("pandas")
    with transaction() as cur:
        wipe_all(cur)
        pid = projects.create(cur, "CPROJ")
        sa = subjects.create(cur, "S", "F")
        vid = visits.create(cur, sa, "ctrl", 20, timepoint="t")
        sid = samples.create(cur, vid, "SS", "sample", "X", "X", "libX")
        samples.link_to_project(cur, pid, sid)
        metadata.set_visit(cur, vid, "shared", "from_visit")
        metadata.set_sample(cur, sid, "shared", "from_sample")
        df = queries.samples_with_metadata(cur, pid)
    assert df.iloc[0]["shared"] == "from_sample"
    assert df.iloc[0]["visit_shared"] == "from_visit"
    with transaction() as cur:
        wipe_all(cur)


def test_samples_with_metadata_empty_for_unknown(populated_project):
    pytest.importorskip("pandas")
    with transaction() as cur:
        df = queries.samples_with_metadata(cur, 9_999_999)
    assert df.empty


# --------------------------------------------------------------------------- #
# files_for_project
# --------------------------------------------------------------------------- #

def test_files_for_project_returns_all(populated_project):
    pytest.importorskip("pandas")
    pid = populated_project["project_id"]
    with transaction() as cur:
        df = queries.files_for_project(cur, pid)
    assert len(df) == 4
    assert set(df["file_type"]) == {"fastq_r1", "fastq_r2", "bam", "beer_norm"}


def test_files_for_project_filters_by_file_type(populated_project):
    pytest.importorskip("pandas")
    pid = populated_project["project_id"]
    with transaction() as cur:
        df = queries.files_for_project(cur, pid, file_type="bam")
    assert df["file_path"].tolist() == ["/lisc/archive/qproj/SAMP_A3.bam"]


def test_files_for_project_filters_by_storage_tier(populated_project):
    pytest.importorskip("pandas")
    pid = populated_project["project_id"]
    with transaction() as cur:
        df = queries.files_for_project(cur, pid, storage_tier="work")
    assert df["file_type"].tolist() == ["beer_norm"]


# --------------------------------------------------------------------------- #
# find_db_files_missing_on_disk / find_disk_files_missing_in_db
# --------------------------------------------------------------------------- #

def test_find_db_files_missing_on_disk_all_missing(populated_project):
    """Fixture file paths are fake (/lisc/archive/...) so they're all missing."""
    pytest.importorskip("pandas")
    pid = populated_project["project_id"]
    with transaction() as cur:
        df = queries.find_db_files_missing_on_disk(cur, project_id=pid)
    assert len(df) == 4


def test_find_db_files_missing_on_disk_finds_real_file(
    populated_project, tmp_path,
):
    """A real on-disk file should NOT appear in the missing list."""
    pytest.importorskip("pandas")
    real = tmp_path / "real.bam"
    real.write_bytes(b"\x00")
    pid = populated_project["project_id"]
    with transaction() as cur:
        cur.execute(
            "UPDATE sample_files SET file_path = ? WHERE file_path = ?",
            (str(real), "/lisc/archive/qproj/SAMP_A3.bam"),
        )
        df = queries.find_db_files_missing_on_disk(cur, project_id=pid)
    paths = set(df["file_path"])
    assert str(real) not in paths
    assert "/lisc/archive/qproj/SAMP_A1_R1.fastq.gz" in paths


def test_find_disk_files_missing_in_db_with_temp_root(
    populated_project, tmp_path,
):
    pytest.importorskip("pandas")
    # Two on-disk files; one is in the DB, one isn't.
    known = tmp_path / "known.bam"
    unknown = tmp_path / "unknown.bam"
    known.write_bytes(b"\x00")
    unknown.write_bytes(b"\x00")
    with transaction() as cur:
        cur.execute(
            "UPDATE sample_files SET file_path = ? WHERE file_path = ?",
            (str(known), "/lisc/archive/qproj/SAMP_A3.bam"),
        )
        df = queries.find_disk_files_missing_in_db(cur, roots=[str(tmp_path)])
    paths = set(df["file_path"])
    assert str(unknown) in paths
    assert str(known) not in paths
