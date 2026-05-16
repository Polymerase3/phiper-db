"""Integration tests for noxdb.samples (require a live MariaDB)."""

from __future__ import annotations

import mariadb
import pytest

from noxdb import projects, samples, subjects, transaction, visits

from tests._helpers import wipe_all


@pytest.fixture
def two_visits(_init_pool):
    """Two visits under one subject. Cleanup wipes everything via cascade."""
    with transaction() as cur:
        wipe_all(cur)
        projects.create(cur, "SPROJ")
        sid = subjects.create(cur, "S1", "F")
        v1 = visits.create(cur, sid, "control", 30, timepoint="baseline")
        v2 = visits.create(cur, sid, "control", 31, timepoint="m3")
    yield v1, v2
    with transaction() as cur:
        wipe_all(cur)


# --------------------------------------------------------------------------- #
# create / get / get_by_name
# --------------------------------------------------------------------------- #

def test_create_returns_new_id_and_persists(two_visits):
    v1, _ = two_visits
    with transaction() as cur:
        sid = samples.create(
            cur, v1, "SAMP_A", "sample", "SQR1", "SQRP1", "libA",
            antibody_class="IgG",
        )
        assert isinstance(sid, int) and sid > 0
        row = samples.get(cur, sid)
    assert row["visit_id"] == v1
    assert row["sample_name"] == "SAMP_A"
    assert row["sample_type"] == "sample"
    assert row["SQR"] == "SQR1"
    assert row["SQRP"] == "SQRP1"
    assert row["library"] == "libA"
    assert row["antibody_class"] == "IgG"


def test_create_canonicalizes_sqr_sqrp(two_visits):
    """Whitespace is stripped; NA/empty SQRP collapses to '' so
    SQR+SQRP plate matching never drifts. Padding is preserved."""
    v1, _ = two_visits
    with transaction() as cur:
        sid = samples.create(
            cur, v1, "CANON1", "sample", "  01 ", "NA", "libA",
        )
        row = samples.get(cur, sid)
    assert row["SQR"] == "01"
    assert row["SQRP"] == ""


def test_create_canonicalizes_na_sqr(two_visits):
    v1, _ = two_visits
    with transaction() as cur:
        sid = samples.create(
            cur, v1, "CANON2", "sample", "n/a", "", "libA",
        )
        row = samples.get(cur, sid)
    assert row["SQR"] == ""
    assert row["SQRP"] == ""


def test_update_canonicalizes_sqr(two_visits):
    v1, _ = two_visits
    with transaction() as cur:
        sid = samples.create(cur, v1, "CANON3", "sample", "05", "06", "libA")
        samples.update(cur, sid, sqr=" 07 ", sqrp="N/A")
        row = samples.get(cur, sid)
    assert row["SQR"] == "07"
    assert row["SQRP"] == ""


def test_create_allows_null_antibody_class(two_visits):
    v1, _ = two_visits
    with transaction() as cur:
        sid = samples.create(cur, v1, "SAMP_NO_AB", "input", "SQR1", "SQRP1", "libA")
        row = samples.get(cur, sid)
    assert row["antibody_class"] is None


def test_create_duplicate_name_raises(two_visits):
    """sample_name is GLOBALLY unique — duplicates across visits also fail."""
    v1, v2 = two_visits
    with transaction() as cur:
        samples.create(cur, v1, "DUP", "sample", "SQR1", "SQRP1", "libA")
    with pytest.raises(mariadb.IntegrityError):
        with transaction() as cur:
            samples.create(cur, v2, "DUP", "sample", "SQR2", "SQRP2", "libB")


def test_create_unknown_visit_id_raises(two_visits):
    with pytest.raises(mariadb.IntegrityError):
        with transaction() as cur:
            samples.create(
                cur, 9_999_999, "ORPHAN", "sample", "SQR1", "SQRP1", "libA",
            )


def test_create_invalid_sample_type_raises(two_visits):
    v1, _ = two_visits
    with pytest.raises(mariadb.Error):
        with transaction() as cur:
            samples.create(cur, v1, "BADTYPE", "wrong", "SQR1", "SQRP1", "libA")


def test_get_missing_returns_none(two_visits):
    with transaction() as cur:
        assert samples.get(cur, 9_999_999) is None


def test_get_by_name_missing_returns_none(two_visits):
    with transaction() as cur:
        assert samples.get_by_name(cur, "nope") is None


def test_get_by_name_returns_row(two_visits):
    v1, _ = two_visits
    with transaction() as cur:
        sid = samples.create(cur, v1, "BYNAME", "sample", "SQR1", "SQRP1", "libA")
        row = samples.get_by_name(cur, "BYNAME")
    assert row["sample_id"] == sid


# --------------------------------------------------------------------------- #
# get_or_create
# --------------------------------------------------------------------------- #

def test_get_or_create_inserts_when_missing(two_visits):
    v1, _ = two_visits
    with transaction() as cur:
        sid, created = samples.get_or_create(
            cur, v1, "GOC", "sample", "SQR1", "SQRP1", "libA",
            antibody_class="IgG",
        )
    assert created is True
    with transaction() as cur:
        assert samples.get(cur, sid)["antibody_class"] == "IgG"


def test_get_or_create_returns_existing_without_modifying(two_visits):
    v1, v2 = two_visits
    with transaction() as cur:
        first_id = samples.create(
            cur, v1, "GOC2", "sample", "SQR1", "SQRP1", "libA",
            antibody_class="IgG",
        )
    with transaction() as cur:
        sid, created = samples.get_or_create(
            cur, v2, "GOC2", "input", "SQR9", "SQRP9", "libZ",
            antibody_class="ignored",
        )
    assert sid == first_id
    assert created is False
    with transaction() as cur:
        row = samples.get(cur, sid)
    assert row["visit_id"] == v1  # NOT moved to v2
    assert row["library"] == "libA"
    assert row["antibody_class"] == "IgG"


# --------------------------------------------------------------------------- #
# list_for_visit / count_for_visit
# --------------------------------------------------------------------------- #

def test_list_for_visit_orders_by_sample_id(two_visits):
    v1, v2 = two_visits
    with transaction() as cur:
        a = samples.create(cur, v1, "A", "sample", "SQR1", "SQRP1", "libA")
        b = samples.create(cur, v1, "B", "input",  "SQR1", "SQRP1", "libA")
        samples.create(cur, v2, "C", "sample", "SQR1", "SQRP1", "libA")
    with transaction() as cur:
        rows = samples.list_for_visit(cur, v1)
    assert [r["sample_id"] for r in rows] == [a, b]


def test_list_for_visit_rejects_unknown_order_by(two_visits):
    v1, _ = two_visits
    with transaction() as cur:
        with pytest.raises(ValueError):
            samples.list_for_visit(cur, v1, order_by="; DROP TABLE samples")


def test_count_for_visit_isolated_per_visit(two_visits):
    v1, v2 = two_visits
    with transaction() as cur:
        samples.create(cur, v1, "A", "sample", "SQR1", "SQRP1", "libA")
        samples.create(cur, v1, "B", "input",  "SQR1", "SQRP1", "libA")
        samples.create(cur, v2, "C", "sample", "SQR1", "SQRP1", "libA")
    with transaction() as cur:
        assert samples.count_for_visit(cur, v1) == 2
        assert samples.count_for_visit(cur, v2) == 1


# --------------------------------------------------------------------------- #
# update
# --------------------------------------------------------------------------- #

def test_update_partial_only_changes_provided_fields(two_visits):
    v1, _ = two_visits
    with transaction() as cur:
        sid = samples.create(
            cur, v1, "U1", "sample", "SQR1", "SQRP1", "libA",
            antibody_class="IgG",
        )
    with transaction() as cur:
        changed = samples.update(cur, sid, library="libB")
    assert changed is True
    with transaction() as cur:
        row = samples.get(cur, sid)
    assert row["library"] == "libB"
    assert row["SQR"] == "SQR1"
    assert row["antibody_class"] == "IgG"


def test_update_with_all_none_is_noop(two_visits):
    v1, _ = two_visits
    with transaction() as cur:
        sid = samples.create(cur, v1, "U2", "sample", "SQR1", "SQRP1", "libA")
    with transaction() as cur:
        assert samples.update(cur, sid) is False


def test_update_unknown_id_returns_false(two_visits):
    with transaction() as cur:
        assert samples.update(cur, 9_999_999, library="x") is False


def test_update_does_not_expose_visit_id(two_visits):
    v1, v2 = two_visits
    with transaction() as cur:
        sid = samples.create(cur, v1, "MOVE", "sample", "SQR1", "SQRP1", "libA")
        with pytest.raises(TypeError):
            samples.update(cur, sid, visit_id=v2)


# --------------------------------------------------------------------------- #
# delete / exists
# --------------------------------------------------------------------------- #

def test_delete_returns_true_when_row_removed(two_visits):
    v1, _ = two_visits
    with transaction() as cur:
        sid = samples.create(cur, v1, "D1", "sample", "SQR1", "SQRP1", "libA")
    with transaction() as cur:
        assert samples.delete(cur, sid) is True
        assert samples.get(cur, sid) is None


def test_delete_unknown_id_returns_false(two_visits):
    with transaction() as cur:
        assert samples.delete(cur, 9_999_999) is False


def test_delete_cascades_to_sample_metadata(two_visits):
    v1, _ = two_visits
    with transaction() as cur:
        sid = samples.create(cur, v1, "DCASC", "sample", "SQR1", "SQRP1", "libA")
        cur.execute(
            "INSERT INTO sample_metadata "
            "(sample_id, key_name, value_text, value_type) VALUES (?, ?, ?, ?)",
            (sid, "well", "A01", "text"),
        )
    with transaction() as cur:
        samples.delete(cur, sid)
        cur.execute(
            "SELECT COUNT(*) FROM sample_metadata WHERE sample_id = ?", (sid,)
        )
        assert cur.fetchone()[0] == 0


def test_delete_blocked_by_sample_files(two_visits):
    """sample_files uses ON DELETE RESTRICT — sample deletes must fail
    while a file row references the sample."""
    v1, _ = two_visits
    with transaction() as cur:
        sid = samples.create(cur, v1, "DREST", "sample", "SQR1", "SQRP1", "libA")
        cur.execute(
            "INSERT INTO sample_files "
            "(sample_id, file_type, file_path) VALUES (?, ?, ?)",
            (sid, "fastq_r1", "/data/x.fastq.gz"),
        )
    with pytest.raises(mariadb.IntegrityError):
        with transaction() as cur:
            samples.delete(cur, sid)


def test_exists_by_id(two_visits):
    v1, _ = two_visits
    with transaction() as cur:
        sid = samples.create(cur, v1, "E1", "sample", "SQR1", "SQRP1", "libA")
    with transaction() as cur:
        assert samples.exists(cur, sid) is True
        assert samples.exists(cur, 9_999_999) is False


def test_exists_by_name(two_visits):
    v1, _ = two_visits
    with transaction() as cur:
        samples.create(cur, v1, "E2", "sample", "SQR1", "SQRP1", "libA")
    with transaction() as cur:
        assert samples.exists(cur, name="E2") is True
        assert samples.exists(cur, name="missing") is False


def test_exists_requires_exactly_one_arg(two_visits):
    with transaction() as cur:
        with pytest.raises(ValueError):
            samples.exists(cur)
        with pytest.raises(ValueError):
            samples.exists(cur, 1, name="x")
