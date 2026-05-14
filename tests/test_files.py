"""Tests for dbmaria_utils.files.

Two tiers, like ``test_metadata.py``:

- Pure-function unit tests live in the ``Test*`` classes and run without
  a DB.
- The rest are integration tests that need ``_init_pool`` plus a real
  filesystem under ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import os

import mariadb
import pytest

from dbmaria_utils import files, projects, samples, subjects, transaction, visits

from tests._helpers import wipe_all
from dbmaria_utils.files import (
    _compute_md5,
    _expected_tier,
    _resolve_tier,
    _validate_extension,
    _validate_md5,
    _validate_tier_path,
)


# =========================================================================== #
# Unit tier — pure helpers, no DB / no env mutation required
# =========================================================================== #

class TestExpectedTier:
    @pytest.mark.parametrize(
        "ft", ["fastq_r1", "fastq_r2", "fastq_single", "bam", "counts"],
    )
    def test_archive_types(self, ft):
        assert _expected_tier(ft) == "archive"

    @pytest.mark.parametrize("ft", ["beer_norm", "zigp_norm", "edger_norm"])
    def test_work_types(self, ft):
        assert _expected_tier(ft) == "work"

    def test_unknown_type_raises(self):
        with pytest.raises(ValueError):
            _expected_tier("nope")


class TestValidateExtension:
    @pytest.mark.parametrize(
        "path",
        ["/x/y.fastq", "/x/y.fastq.gz", "/x/y.fq", "/x/y.fq.gz",
         "/x/Y.FASTQ.GZ"],
    )
    def test_fastq_accepts(self, path):
        _validate_extension(path, "fastq_r1")
        _validate_extension(path, "fastq_r2")
        _validate_extension(path, "fastq_single")

    def test_fastq_rejects_wrong_extension(self):
        with pytest.raises(ValueError):
            _validate_extension("/x/y.bam", "fastq_r1")

    def test_bam_accepts(self):
        _validate_extension("/x/y.bam", "bam")
        _validate_extension("/x/Y.BAM", "bam")

    def test_bam_rejects_wrong_extension(self):
        with pytest.raises(ValueError):
            _validate_extension("/x/y.fastq", "bam")

    def test_counts_does_not_validate(self):
        # No raise even with weird extensions
        _validate_extension("/x/y.tsv", "counts")
        _validate_extension("/x/y.h5", "counts")
        _validate_extension("/x/y.weird", "counts")

    def test_norm_does_not_validate(self):
        _validate_extension("/x/y.tsv", "beer_norm")
        _validate_extension("/x/y.rds", "zigp_norm")
        _validate_extension("/x/y", "edger_norm")


class TestValidateTierPath:
    def test_archive_path_under_root(self):
        _validate_tier_path(
            "/lisc/archive/foo/x.bam", "archive",
            archive_root="/lisc/archive", work_root="/lisc/work",
        )

    def test_archive_sibling_prefix_rejected(self):
        # /lisc/archive_old must NOT be accepted as /lisc/archive.
        with pytest.raises(ValueError):
            _validate_tier_path(
                "/lisc/archive_old/x.bam", "archive",
                archive_root="/lisc/archive", work_root="/lisc/work",
            )

    def test_work_path_under_archive_rejected(self):
        with pytest.raises(ValueError):
            _validate_tier_path(
                "/lisc/work/x.tsv", "archive",
                archive_root="/lisc/archive", work_root="/lisc/work",
            )

    def test_work_path_under_root(self):
        _validate_tier_path(
            "/lisc/work/foo/x.tsv", "work",
            archive_root="/lisc/archive", work_root="/lisc/work",
        )

    def test_scratch_skips_check(self):
        _validate_tier_path(
            "/anywhere/x.bam", "scratch",
            archive_root="/lisc/archive", work_root="/lisc/work",
        )

    def test_external_skips_check(self):
        _validate_tier_path(
            "/anywhere/x.bam", "external",
            archive_root="/lisc/archive", work_root="/lisc/work",
        )


class TestResolveTier:
    def test_default_archive(self):
        assert _resolve_tier("bam", None) == "archive"

    def test_default_work(self):
        assert _resolve_tier("beer_norm", None) == "work"

    def test_override_to_scratch(self):
        assert _resolve_tier("bam", "scratch") == "scratch"

    def test_override_to_external(self):
        assert _resolve_tier("beer_norm", "external") == "external"

    def test_override_archive_to_work_allowed(self):
        assert _resolve_tier("bam", "work") == "work"

    def test_override_work_to_archive_allowed(self):
        assert _resolve_tier("beer_norm", "archive") == "archive"

    def test_unknown_tier_rejected(self):
        with pytest.raises(ValueError):
            _resolve_tier("bam", "garbage")


class TestValidateMd5:
    def test_valid_lowercase_hex(self):
        _validate_md5("0123456789abcdef" * 2)

    def test_uppercase_hex_rejected(self):
        # Schema CHECK is lowercase only.
        with pytest.raises(ValueError):
            _validate_md5("0123456789ABCDEF" * 2)

    def test_too_short_rejected(self):
        with pytest.raises(ValueError):
            _validate_md5("a" * 31)

    def test_non_hex_rejected(self):
        with pytest.raises(ValueError):
            _validate_md5("g" * 32)


class TestComputeMd5:
    def test_matches_hashlib(self, tmp_path):
        content = b"hello world\n" * 100
        p = tmp_path / "x.bin"
        p.write_bytes(content)
        expected = hashlib.md5(content).hexdigest()
        assert _compute_md5(str(p)) == expected

    def test_empty_file(self, tmp_path):
        p = tmp_path / "empty.bin"
        p.write_bytes(b"")
        assert _compute_md5(str(p)) == hashlib.md5(b"").hexdigest()


# =========================================================================== #
# Integration tier — live MariaDB + tmp filesystem
# =========================================================================== #

@pytest.fixture
def roots(monkeypatch, tmp_path):
    """Point the archive/work roots at tmp directories."""
    arc = tmp_path / "archive"
    wrk = tmp_path / "work"
    arc.mkdir()
    wrk.mkdir()
    monkeypatch.setenv("LABDB_ARCHIVE_ROOT", str(arc))
    monkeypatch.setenv("LABDB_WORK_ROOT", str(wrk))
    return arc, wrk


@pytest.fixture
def parent_ids(_init_pool):
    """One project / subject / visit / sample for sample_files to attach to."""
    with transaction() as cur:
        wipe_all(cur)
        pid = projects.create(cur, "FPROJ")
        sid = subjects.create(cur, pid, "S1", "F")
        vid = visits.create(cur, sid, "control", 30, timepoint="baseline")
        smp = samples.create(cur, vid, "SMP1", "sample", "SQR1", "SQRP1", "libA")
    yield smp
    with transaction() as cur:
        wipe_all(cur)


def _make_file(p, content: bytes = b"hello") -> str:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return str(p)


# --------------------------------------------------------------------------- #
# register: happy paths
# --------------------------------------------------------------------------- #

def test_register_bam_under_archive(parent_ids, roots):
    arc, _ = roots
    path = _make_file(arc / "x.bam", b"BAM" * 1000)
    with transaction() as cur:
        fid = files.register(cur, parent_ids, path, "bam")
    with transaction() as cur:
        row = files.get(cur, fid)
    assert row["sample_id"] == parent_ids
    assert row["file_type"] == "bam"
    assert row["file_path"] == path
    assert row["file_size_bytes"] == os.path.getsize(path)
    assert row["checksum_md5"] is None
    assert row["storage_tier"] == "archive"


def test_register_norm_under_work(parent_ids, roots):
    _, wrk = roots
    path = _make_file(wrk / "x.tsv", b"col1\tcol2\n")
    with transaction() as cur:
        fid = files.register(cur, parent_ids, path, "beer_norm")
    with transaction() as cur:
        row = files.get(cur, fid)
    assert row["storage_tier"] == "work"
    assert row["file_type"] == "beer_norm"


def test_register_with_compute_md5(parent_ids, roots):
    arc, _ = roots
    content = b"hello md5\n"
    path = _make_file(arc / "y.bam", content)
    with transaction() as cur:
        fid = files.register(cur, parent_ids, path, "bam", compute_md5=True)
    with transaction() as cur:
        row = files.get(cur, fid)
    assert row["checksum_md5"] == hashlib.md5(content).hexdigest()


def test_register_with_explicit_md5(parent_ids, roots):
    arc, _ = roots
    path = _make_file(arc / "z.bam")
    explicit = "a" * 32
    with transaction() as cur:
        fid = files.register(cur, parent_ids, path, "bam", checksum_md5=explicit)
    with transaction() as cur:
        assert files.get(cur, fid)["checksum_md5"] == explicit


# --------------------------------------------------------------------------- #
# register: error paths
# --------------------------------------------------------------------------- #

def test_register_compute_md5_and_explicit_md5_rejected(parent_ids, roots):
    arc, _ = roots
    path = _make_file(arc / "x.bam")
    with transaction() as cur:
        with pytest.raises(ValueError):
            files.register(
                cur, parent_ids, path, "bam",
                compute_md5=True, checksum_md5="a" * 32,
            )


def test_register_invalid_md5_rejected(parent_ids, roots):
    arc, _ = roots
    path = _make_file(arc / "x.bam")
    with transaction() as cur:
        with pytest.raises(ValueError):
            files.register(
                cur, parent_ids, path, "bam", checksum_md5="not-hex",
            )


def test_register_missing_path_raises(parent_ids, roots):
    arc, _ = roots
    missing = str(arc / "ghost.bam")  # not created
    with transaction() as cur:
        with pytest.raises(FileNotFoundError):
            files.register(cur, parent_ids, missing, "bam")


def test_register_directory_raises(parent_ids, roots):
    arc, _ = roots
    d = arc / "subdir.bam"  # name still ends with .bam to pass extension check
    d.mkdir()
    with transaction() as cur:
        with pytest.raises(IsADirectoryError):
            files.register(cur, parent_ids, str(d), "bam")


def test_register_relative_path_raises(parent_ids, roots):
    with transaction() as cur:
        with pytest.raises(ValueError):
            files.register(cur, parent_ids, "relative/x.bam", "bam")


def test_register_extension_mismatch_raises(parent_ids, roots):
    arc, _ = roots
    path = _make_file(arc / "x.txt")
    with transaction() as cur:
        with pytest.raises(ValueError):
            files.register(cur, parent_ids, path, "fastq_r1")


def test_register_archive_type_under_work_root_raises(parent_ids, roots):
    _, wrk = roots
    path = _make_file(wrk / "x.bam")
    with transaction() as cur:
        with pytest.raises(ValueError):
            files.register(cur, parent_ids, path, "bam")


def test_register_work_type_under_archive_root_raises(parent_ids, roots):
    arc, _ = roots
    path = _make_file(arc / "x.tsv")
    with transaction() as cur:
        with pytest.raises(ValueError):
            files.register(cur, parent_ids, path, "beer_norm")


def test_register_scratch_outside_roots_succeeds(parent_ids, roots, tmp_path):
    other = tmp_path / "elsewhere"
    path = _make_file(other / "x.bam")
    with transaction() as cur:
        fid = files.register(
            cur, parent_ids, path, "bam", storage_tier="scratch",
        )
    with transaction() as cur:
        assert files.get(cur, fid)["storage_tier"] == "scratch"


def test_register_override_archive_to_work_rejected(parent_ids, roots):
    _, wrk = roots
    path = _make_file(wrk / "x.tsv")
    with transaction() as cur:
        with pytest.raises(ValueError):
            files.register(
                cur, parent_ids, path, "beer_norm", storage_tier="archive",
            )


def test_register_duplicate_path_raises(parent_ids, roots):
    arc, _ = roots
    path = _make_file(arc / "dup.bam")
    with transaction() as cur:
        files.register(cur, parent_ids, path, "bam")
    with pytest.raises(mariadb.IntegrityError):
        with transaction() as cur:
            files.register(cur, parent_ids, path, "bam")


def test_register_unknown_sample_id_raises(roots):
    arc, _ = roots
    path = _make_file(arc / "orphan.bam")
    with pytest.raises(mariadb.IntegrityError):
        with transaction() as cur:
            files.register(cur, 9_999_999, path, "bam")


# --------------------------------------------------------------------------- #
# get_or_register
# --------------------------------------------------------------------------- #

def test_get_or_register_inserts_then_returns_existing(parent_ids, roots):
    arc, _ = roots
    path = _make_file(arc / "goc.bam")
    with transaction() as cur:
        fid1, registered1 = files.get_or_register(cur, parent_ids, path, "bam")
    assert registered1 is True
    with transaction() as cur:
        fid2, registered2 = files.get_or_register(cur, parent_ids, path, "bam")
    assert registered2 is False
    assert fid2 == fid1


def test_get_or_register_existing_does_not_restat(parent_ids, roots):
    """Once registered, removing the file on disk does not block the
    second get_or_register call."""
    arc, _ = roots
    path = _make_file(arc / "goc2.bam")
    with transaction() as cur:
        fid1, _ = files.get_or_register(cur, parent_ids, path, "bam")
    os.remove(path)
    with transaction() as cur:
        fid2, registered = files.get_or_register(cur, parent_ids, path, "bam")
    assert (fid2, registered) == (fid1, False)


# --------------------------------------------------------------------------- #
# get / get_by_path / list / count
# --------------------------------------------------------------------------- #

def test_get_missing_returns_none(parent_ids, roots):
    with transaction() as cur:
        assert files.get(cur, 9_999_999) is None


def test_get_by_path_missing_returns_none(parent_ids, roots):
    with transaction() as cur:
        assert files.get_by_path(cur, "/does/not/exist") is None


def test_list_for_sample_orders_by_file_id(parent_ids, roots):
    arc, _ = roots
    p1 = _make_file(arc / "a.bam", b"a")
    p2 = _make_file(arc / "b.bam", b"b")
    with transaction() as cur:
        a = files.register(cur, parent_ids, p1, "bam")
        b = files.register(cur, parent_ids, p2, "bam")
    with transaction() as cur:
        rows = files.list_for_sample(cur, parent_ids)
    assert [r["file_id"] for r in rows] == [a, b]


def test_list_for_sample_rejects_unknown_order_by(parent_ids, roots):
    with transaction() as cur:
        with pytest.raises(ValueError):
            files.list_for_sample(
                cur, parent_ids, order_by="; DROP TABLE sample_files",
            )


def test_count_for_sample(parent_ids, roots):
    arc, _ = roots
    with transaction() as cur:
        files.register(cur, parent_ids, _make_file(arc / "a.bam", b"a"), "bam")
        files.register(cur, parent_ids, _make_file(arc / "b.bam", b"b"), "bam")
    with transaction() as cur:
        assert files.count_for_sample(cur, parent_ids) == 2


# --------------------------------------------------------------------------- #
# update / restat
# --------------------------------------------------------------------------- #

def test_update_partial(parent_ids, roots):
    arc, _ = roots
    path = _make_file(arc / "u.bam")
    with transaction() as cur:
        fid = files.register(cur, parent_ids, path, "bam")
    with transaction() as cur:
        changed = files.update(cur, fid, storage_tier="scratch")
    assert changed is True
    with transaction() as cur:
        assert files.get(cur, fid)["storage_tier"] == "scratch"


def test_update_with_all_none_is_noop(parent_ids, roots):
    arc, _ = roots
    path = _make_file(arc / "u2.bam")
    with transaction() as cur:
        fid = files.register(cur, parent_ids, path, "bam")
    with transaction() as cur:
        assert files.update(cur, fid) is False


def test_update_invalid_md5_raises(parent_ids, roots):
    arc, _ = roots
    path = _make_file(arc / "u3.bam")
    with transaction() as cur:
        fid = files.register(cur, parent_ids, path, "bam")
        with pytest.raises(ValueError):
            files.update(cur, fid, checksum_md5="ZZZ")


def test_restat_picks_up_size_change(parent_ids, roots):
    arc, _ = roots
    path = _make_file(arc / "r.bam", b"x" * 10)
    with transaction() as cur:
        fid = files.register(cur, parent_ids, path, "bam")
    # Grow the file on disk
    with open(path, "ab") as f:
        f.write(b"y" * 100)
    with transaction() as cur:
        files.restat(cur, fid)
    with transaction() as cur:
        assert files.get(cur, fid)["file_size_bytes"] == 110


def test_restat_with_compute_md5(parent_ids, roots):
    arc, _ = roots
    content = b"AAA"
    path = _make_file(arc / "rmd5.bam", content)
    with transaction() as cur:
        fid = files.register(cur, parent_ids, path, "bam")
    new_content = content + b"BBB"
    with open(path, "wb") as f:
        f.write(new_content)
    with transaction() as cur:
        files.restat(cur, fid, compute_md5=True)
    with transaction() as cur:
        row = files.get(cur, fid)
    assert row["checksum_md5"] == hashlib.md5(new_content).hexdigest()
    assert row["file_size_bytes"] == len(new_content)


def test_restat_missing_file_raises(parent_ids, roots):
    arc, _ = roots
    path = _make_file(arc / "gone.bam")
    with transaction() as cur:
        fid = files.register(cur, parent_ids, path, "bam")
    os.remove(path)
    with pytest.raises(FileNotFoundError):
        with transaction() as cur:
            files.restat(cur, fid)


# --------------------------------------------------------------------------- #
# delete / exists
# --------------------------------------------------------------------------- #

def test_delete_returns_true_when_row_removed(parent_ids, roots):
    arc, _ = roots
    path = _make_file(arc / "d.bam")
    with transaction() as cur:
        fid = files.register(cur, parent_ids, path, "bam")
    with transaction() as cur:
        assert files.delete(cur, fid) is True
        assert files.get(cur, fid) is None


def test_delete_unknown_id_returns_false(parent_ids):
    with transaction() as cur:
        assert files.delete(cur, 9_999_999) is False


def test_sample_delete_blocked_while_files_exist(parent_ids, roots):
    """Mirror of test_samples test: sample delete is RESTRICTed by FK."""
    arc, _ = roots
    path = _make_file(arc / "rest.bam")
    with transaction() as cur:
        files.register(cur, parent_ids, path, "bam")
    with pytest.raises(mariadb.IntegrityError):
        with transaction() as cur:
            samples.delete(cur, parent_ids)


def test_exists_by_id(parent_ids, roots):
    arc, _ = roots
    path = _make_file(arc / "e.bam")
    with transaction() as cur:
        fid = files.register(cur, parent_ids, path, "bam")
    with transaction() as cur:
        assert files.exists(cur, fid) is True
        assert files.exists(cur, 9_999_999) is False


def test_exists_by_path(parent_ids, roots):
    arc, _ = roots
    path = _make_file(arc / "e2.bam")
    with transaction() as cur:
        files.register(cur, parent_ids, path, "bam")
    with transaction() as cur:
        assert files.exists(cur, path=path) is True
        assert files.exists(cur, path="/nope.bam") is False


def test_exists_requires_exactly_one_arg(parent_ids):
    with transaction() as cur:
        with pytest.raises(ValueError):
            files.exists(cur)
        with pytest.raises(ValueError):
            files.exists(cur, 1, path="/x")
