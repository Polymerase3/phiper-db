"""Integration tests for dbmaria_utils.workflows (require a live MariaDB).

Workflows must be atomic — a failure partway through must roll back every
prior write in the same call. Tests cover both standalone usage (workflow
owns the transaction) and composed usage (caller-provided cursor).

``sample_files`` rows go through :func:`files.register` for the happy path,
so we create a real on-disk file under a temp directory and point
``LABDB_ARCHIVE_ROOT`` / ``LABDB_WORK_ROOT`` at it via monkeypatch.
"""

from __future__ import annotations

import mariadb
import pytest

from dbmaria_utils import (
    execute,
    files,
    metadata,
    projects,
    samples,
    subjects,
    transaction,
    visits,
    workflows,
)

from tests._helpers import wipe_all


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def project(_init_pool):
    """A clean project to attach subjects to."""
    with transaction() as cur:
        wipe_all(cur)
        pid = projects.create(cur, "WPROJ", pi_name="Dr. W")
    yield pid
    with transaction() as cur:
        wipe_all(cur)


@pytest.fixture
def fake_tier_roots(tmp_path, monkeypatch):
    """Point archive/work roots at temp dirs so files.register accepts paths."""
    archive = tmp_path / "archive"
    work = tmp_path / "work"
    archive.mkdir()
    work.mkdir()
    monkeypatch.setenv("LABDB_ARCHIVE_ROOT", str(archive))
    monkeypatch.setenv("LABDB_WORK_ROOT", str(work))
    return archive, work


# --------------------------------------------------------------------------- #
# register_subject_with_visit
# --------------------------------------------------------------------------- #

def test_register_subject_with_visit_creates_both(project):
    sid, vid = workflows.register_subject_with_visit(
        project_id=project,
        subject_code="S1", sex="F", origin="PL",
        timepoint="baseline", group_test="ctrl", age=30,
    )
    with transaction() as cur:
        srow = subjects.get(cur, sid)
        vrow = visits.get(cur, vid)
    assert srow["subject_code"] == "S1"
    assert srow["origin"] == "PL"
    assert vrow["timepoint"] == "baseline"
    assert vrow["group_test"] == "ctrl"
    assert vrow["age"] == 30


def test_register_subject_with_visit_writes_metadata(project):
    sid, vid = workflows.register_subject_with_visit(
        project_id=project,
        subject_code="SM", sex="M",
        timepoint="t0", group_test="tx", age=22,
        visit_metadata={"bmi": 24.1, "smoker": False, "stage": 3},
    )
    with transaction() as cur:
        meta = metadata.list_for_visit(cur, vid)
    assert meta["smoker"] is False
    assert meta["stage"] == 3
    # numeric is stored as DECIMAL and round-trips as Decimal — value-equal
    # comparison via float() avoids importing Decimal here.
    assert float(meta["bmi"]) == 24.1


def test_register_subject_with_visit_idempotent_on_rerun(project):
    sid1, vid1 = workflows.register_subject_with_visit(
        project_id=project,
        subject_code="IDEMP", sex="F",
        timepoint="t0", group_test="ctrl", age=30,
        visit_metadata={"bmi": 22.0},
    )
    sid2, vid2 = workflows.register_subject_with_visit(
        project_id=project,
        subject_code="IDEMP", sex="F",
        timepoint="t0", group_test="ctrl", age=30,
        visit_metadata={"bmi": 22.5},  # updated value
    )
    assert (sid1, vid1) == (sid2, vid2)
    with transaction() as cur:
        meta = metadata.list_for_visit(cur, vid1)
    assert float(meta["bmi"]) == 22.5  # metadata.set_visit upserts


def test_rollback_works_for_late_session_subjects_insert(project):
    """Regression test for the autocommit-after-pool-reset bug.

    Earlier versions of `get_connection()` set `conn.autocommit = False`
    via the Python attribute, which the driver elides when its cached
    flag already matches — leaving server-side autocommit stuck at 1
    after a pool reset. Inserts then auto-committed and rollback was a
    no-op. Verified by issuing a raw INSERT into a table with FK +
    AUTO_INCREMENT (subjects), raising, and checking the row is gone.
    """
    with pytest.raises(RuntimeError, match="rollback regression"):
        with transaction() as cur:
            cur.execute(
                "INSERT INTO subjects (project_id, subject_code, sex) "
                "VALUES (?, ?, ?)",
                (project, "DIAG_S", "F"),
            )
            raise RuntimeError("rollback regression")
    rows = execute(
        "SELECT COUNT(*) AS n FROM subjects "
        "WHERE project_id = ? AND subject_code = ?",
        (project, "DIAG_S"),
    )
    assert rows[0]["n"] == 0


def test_register_subject_with_visit_atomic_rollback_on_bad_metadata(project):
    """A bad metadata value (None) must roll back the subject+visit too."""
    with pytest.raises(ValueError):
        workflows.register_subject_with_visit(
            project_id=project,
            subject_code="ROLLBACK_S", sex="F",
            timepoint="t0", group_test="ctrl", age=30,
            visit_metadata={"bmi": None},  # ValueError from _eav_split
        )
    # Verify via execute() (one-shot connection) — mirrors the working
    # pattern in test_transaction_rolls_back_atomically. A nested
    # `with transaction()` for the readback can latch onto a snapshot
    # taken before the rollback fully propagated.
    rows = execute(
        "SELECT COUNT(*) AS n FROM subjects "
        "WHERE project_id = ? AND subject_code = ?",
        (project, "ROLLBACK_S"),
    )
    assert rows[0]["n"] == 0


def test_register_subject_with_visit_uses_provided_cursor(project):
    """When a cursor is provided, the workflow joins the caller's
    transaction and an outer exception rolls back its writes."""
    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom):
        with transaction() as cur:
            workflows.register_subject_with_visit(
                cur,
                project_id=project,
                subject_code="OUTER_ROLLBACK", sex="F",
                timepoint="t0", group_test="ctrl", age=30,
            )
            raise Boom()

    rows = execute(
        "SELECT COUNT(*) AS n FROM subjects "
        "WHERE project_id = ? AND subject_code = ?",
        (project, "OUTER_ROLLBACK"),
    )
    assert rows[0]["n"] == 0


# --------------------------------------------------------------------------- #
# register_sample_with_files
# --------------------------------------------------------------------------- #

def test_register_sample_with_files_happy_path(project, fake_tier_roots):
    archive, work = fake_tier_roots
    fq = archive / "x_R1.fastq.gz"
    fq.write_bytes(b"\x00")
    norm = work / "x.beer.tsv"
    norm.write_bytes(b"\x00")

    _sid, vid = workflows.register_subject_with_visit(
        project_id=project,
        subject_code="SAMP_SUBJ", sex="F",
        timepoint="t0", group_test="ctrl", age=30,
    )
    sample_id, file_ids = workflows.register_sample_with_files(
        visit_id=vid,
        sample_name="SAMP_X",
        sample_type="sample", sqr="Q1", sqrp="Q1", library="libA",
        antibody_class="IgG",
        sample_metadata={"well_position": "A01", "passed_qc": True},
        files_spec=[
            {"file_path": str(fq), "file_type": "fastq_r1"},
            {"file_path": str(norm), "file_type": "beer_norm"},
        ],
    )
    assert len(file_ids) == 2
    with transaction() as cur:
        assert samples.get(cur, sample_id)["sample_name"] == "SAMP_X"
        meta = metadata.list_for_sample(cur, sample_id)
        assert meta == {"well_position": "A01", "passed_qc": True}
        assert files.count_for_sample(cur, sample_id) == 2


def test_register_sample_with_files_atomic_on_bad_file(project, fake_tier_roots):
    """First file registers, second is bad → entire sample (+metadata) rolls back."""
    archive, _work = fake_tier_roots
    good = archive / "good_R1.fastq.gz"
    good.write_bytes(b"\x00")

    _sid, vid = workflows.register_subject_with_visit(
        project_id=project,
        subject_code="RB_SUBJ", sex="F",
        timepoint="t0", group_test="ctrl", age=30,
    )
    with pytest.raises(FileNotFoundError):
        workflows.register_sample_with_files(
            visit_id=vid,
            sample_name="ROLLBACK_SAMP",
            sample_type="sample", sqr="Q1", sqrp="Q1", library="libA",
            sample_metadata={"well_position": "A01"},
            files_spec=[
                {"file_path": str(good), "file_type": "fastq_r1"},
                {"file_path": str(archive / "missing.fastq.gz"),
                 "file_type": "fastq_r2"},
            ],
        )
    with transaction() as cur:
        assert samples.get_by_name(cur, "ROLLBACK_SAMP") is None
        # Also no orphan file row left behind.
        assert files.get_by_path(cur, str(good)) is None


def test_register_sample_with_files_idempotent(project, fake_tier_roots):
    archive, _work = fake_tier_roots
    fq = archive / "idemp_R1.fastq.gz"
    fq.write_bytes(b"\x00")
    _sid, vid = workflows.register_subject_with_visit(
        project_id=project,
        subject_code="IDEMP_SUBJ", sex="F",
        timepoint="t0", group_test="ctrl", age=30,
    )
    sid1, fids1 = workflows.register_sample_with_files(
        visit_id=vid,
        sample_name="IDEMP_SAMP",
        sample_type="sample", sqr="Q1", sqrp="Q1", library="libA",
        files_spec=[{"file_path": str(fq), "file_type": "fastq_r1"}],
    )
    sid2, fids2 = workflows.register_sample_with_files(
        visit_id=vid,
        sample_name="IDEMP_SAMP",
        sample_type="sample", sqr="Q1", sqrp="Q1", library="libA",
        files_spec=[{"file_path": str(fq), "file_type": "fastq_r1"}],
    )
    assert sid1 == sid2
    assert fids1 == fids2


def test_register_sample_with_files_unknown_visit_raises(project, fake_tier_roots):
    """No visit → FK violation, no orphan row left behind."""
    with pytest.raises(mariadb.IntegrityError):
        workflows.register_sample_with_files(
            visit_id=9_999_999,
            sample_name="NO_VISIT",
            sample_type="sample", sqr="Q1", sqrp="Q1", library="libA",
        )
    with transaction() as cur:
        assert samples.get_by_name(cur, "NO_VISIT") is None


def test_register_sample_with_files_composes_in_outer_transaction(
    project, fake_tier_roots,
):
    """One outer transaction wraps subject+visit+sample+files in atomic unit."""
    archive, _work = fake_tier_roots
    fq = archive / "compose_R1.fastq.gz"
    fq.write_bytes(b"\x00")
    with transaction() as cur:
        _sid, vid = workflows.register_subject_with_visit(
            cur,
            project_id=project,
            subject_code="COMPOSE_SUBJ", sex="F",
            timepoint="t0", group_test="ctrl", age=30,
        )
        sample_id, _file_ids = workflows.register_sample_with_files(
            cur,
            visit_id=vid,
            sample_name="COMPOSE_SAMP",
            sample_type="sample", sqr="Q1", sqrp="Q1", library="libA",
            files_spec=[{"file_path": str(fq), "file_type": "fastq_r1"}],
        )
    with transaction() as cur:
        assert samples.get(cur, sample_id)["sample_name"] == "COMPOSE_SAMP"
