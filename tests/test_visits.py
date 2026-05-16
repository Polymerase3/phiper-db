"""Integration tests for noxdb.visits (require a live MariaDB)."""

from __future__ import annotations

import mariadb
import pytest

from noxdb import projects, subjects, transaction, visits

from tests._helpers import wipe_all


@pytest.fixture
def two_subjects(_init_pool):
    """Two subjects under one project. Cleanup wipes everything via cascade."""
    with transaction() as cur:
        wipe_all(cur)
        projects.create(cur, "VPROJ")
        s1 = subjects.create(cur, "S1", "F")
        s2 = subjects.create(cur, "S2", "M")
    yield s1, s2
    with transaction() as cur:
        wipe_all(cur)


# --------------------------------------------------------------------------- #
# create / get / get_by_subject_timepoint
# --------------------------------------------------------------------------- #

def test_create_returns_new_id_and_persists(two_subjects):
    s1, _ = two_subjects
    with transaction() as cur:
        vid = visits.create(cur, s1, "control", 30, timepoint="baseline")
        assert isinstance(vid, int) and vid > 0
        row = visits.get(cur, vid)
    assert row["subject_id"] == s1
    assert row["timepoint"] == "baseline"
    assert row["group_test"] == "control"
    assert row["age"] == 30


def test_create_allows_null_timepoint(two_subjects):
    s1, _ = two_subjects
    with transaction() as cur:
        vid = visits.create(cur, s1, "control", 30)
        row = visits.get(cur, vid)
    assert row["timepoint"] is None


def test_create_duplicate_subject_timepoint_raises(two_subjects):
    s1, _ = two_subjects
    with transaction() as cur:
        visits.create(cur, s1, "control", 30, timepoint="baseline")
    with pytest.raises(mariadb.IntegrityError):
        with transaction() as cur:
            visits.create(cur, s1, "treated", 31, timepoint="baseline")


def test_create_null_timepoint_allows_duplicates(two_subjects):
    """MariaDB UNIQUE doesn't dedupe NULLs — multiple NULL-timepoint visits
    for the same subject are permitted by the schema."""
    s1, _ = two_subjects
    with transaction() as cur:
        v1 = visits.create(cur, s1, "control", 30)
        v2 = visits.create(cur, s1, "treated", 31)
    assert v1 != v2


def test_create_unknown_subject_id_raises(two_subjects):
    with pytest.raises(mariadb.IntegrityError):
        with transaction() as cur:
            visits.create(cur, 9_999_999, "control", 30, timepoint="baseline")


def test_create_negative_age_raises(two_subjects):
    s1, _ = two_subjects
    with pytest.raises(mariadb.Error):
        with transaction() as cur:
            visits.create(cur, s1, "control", -1, timepoint="baseline")


def test_get_missing_returns_none(two_subjects):
    with transaction() as cur:
        assert visits.get(cur, 9_999_999) is None


def test_get_by_subject_timepoint_uses_is_null_for_none(two_subjects):
    s1, _ = two_subjects
    with transaction() as cur:
        vid = visits.create(cur, s1, "control", 30)  # NULL timepoint
        # Confirm `= NULL` would not match — `IS NULL` does
        row = visits.get_by_subject_timepoint(cur, s1, None)
    assert row is not None
    assert row["visit_id"] == vid


def test_get_by_subject_timepoint_missing_returns_none(two_subjects):
    s1, _ = two_subjects
    with transaction() as cur:
        assert visits.get_by_subject_timepoint(cur, s1, "nope") is None


# --------------------------------------------------------------------------- #
# get_or_create
# --------------------------------------------------------------------------- #

def test_get_or_create_inserts_when_missing(two_subjects):
    s1, _ = two_subjects
    with transaction() as cur:
        vid, created = visits.get_or_create(cur, s1, "baseline", "control", 30)
    assert created is True
    with transaction() as cur:
        assert visits.get(cur, vid)["age"] == 30


def test_get_or_create_returns_existing_without_modifying(two_subjects):
    s1, _ = two_subjects
    with transaction() as cur:
        first_id = visits.create(cur, s1, "control", 30, timepoint="baseline")
    with transaction() as cur:
        vid, created = visits.get_or_create(cur, s1, "baseline", "treated", 99)
    assert vid == first_id
    assert created is False
    with transaction() as cur:
        row = visits.get(cur, vid)
    assert row["group_test"] == "control"
    assert row["age"] == 30


def test_get_or_create_rejects_null_timepoint(two_subjects):
    s1, _ = two_subjects
    with transaction() as cur:
        with pytest.raises(ValueError):
            visits.get_or_create(cur, s1, None, "control", 30)


# --------------------------------------------------------------------------- #
# list_for_subject / count_for_subject
# --------------------------------------------------------------------------- #

def test_list_for_subject_orders_by_visit_id(two_subjects):
    s1, s2 = two_subjects
    with transaction() as cur:
        v1 = visits.create(cur, s1, "control", 30, timepoint="baseline")
        v2 = visits.create(cur, s1, "control", 31, timepoint="m3")
        visits.create(cur, s2, "control", 40, timepoint="baseline")
    with transaction() as cur:
        rows = visits.list_for_subject(cur, s1)
    assert [r["visit_id"] for r in rows] == [v1, v2]


def test_list_for_subject_rejects_unknown_order_by(two_subjects):
    s1, _ = two_subjects
    with transaction() as cur:
        with pytest.raises(ValueError):
            visits.list_for_subject(cur, s1, order_by="; DROP TABLE visits")


def test_count_for_subject_isolated_per_subject(two_subjects):
    s1, s2 = two_subjects
    with transaction() as cur:
        visits.create(cur, s1, "control", 30, timepoint="baseline")
        visits.create(cur, s1, "control", 31, timepoint="m3")
        visits.create(cur, s2, "control", 40, timepoint="baseline")
    with transaction() as cur:
        assert visits.count_for_subject(cur, s1) == 2
        assert visits.count_for_subject(cur, s2) == 1


# --------------------------------------------------------------------------- #
# update
# --------------------------------------------------------------------------- #

def test_update_partial_only_changes_provided_fields(two_subjects):
    s1, _ = two_subjects
    with transaction() as cur:
        vid = visits.create(cur, s1, "control", 30, timepoint="baseline")
    with transaction() as cur:
        changed = visits.update(cur, vid, age=31)
    assert changed is True
    with transaction() as cur:
        row = visits.get(cur, vid)
    assert row["age"] == 31
    assert row["group_test"] == "control"
    assert row["timepoint"] == "baseline"


def test_update_with_all_none_is_noop(two_subjects):
    s1, _ = two_subjects
    with transaction() as cur:
        vid = visits.create(cur, s1, "control", 30, timepoint="baseline")
    with transaction() as cur:
        changed = visits.update(cur, vid)
    assert changed is False


def test_update_unknown_id_returns_false(two_subjects):
    with transaction() as cur:
        assert visits.update(cur, 9_999_999, age=10) is False


def test_update_does_not_expose_subject_id(two_subjects):
    s1, s2 = two_subjects
    with transaction() as cur:
        vid = visits.create(cur, s1, "control", 30, timepoint="baseline")
        with pytest.raises(TypeError):
            visits.update(cur, vid, subject_id=s2)


# --------------------------------------------------------------------------- #
# delete / exists
# --------------------------------------------------------------------------- #

def test_delete_returns_true_when_row_removed(two_subjects):
    s1, _ = two_subjects
    with transaction() as cur:
        vid = visits.create(cur, s1, "control", 30, timepoint="baseline")
    with transaction() as cur:
        assert visits.delete(cur, vid) is True
        assert visits.get(cur, vid) is None


def test_delete_unknown_id_returns_false(two_subjects):
    with transaction() as cur:
        assert visits.delete(cur, 9_999_999) is False


def test_delete_cascades_to_visit_metadata(two_subjects):
    s1, _ = two_subjects
    with transaction() as cur:
        vid = visits.create(cur, s1, "control", 30, timepoint="baseline")
        cur.execute(
            "INSERT INTO visit_metadata "
            "(visit_id, key_name, value_int, value_type) VALUES (?, ?, ?, ?)",
            (vid, "score", 5, "int"),
        )
    with transaction() as cur:
        visits.delete(cur, vid)
        cur.execute(
            "SELECT COUNT(*) FROM visit_metadata WHERE visit_id = ?", (vid,)
        )
        assert cur.fetchone()[0] == 0


def test_exists_by_id(two_subjects):
    s1, _ = two_subjects
    with transaction() as cur:
        vid = visits.create(cur, s1, "control", 30, timepoint="baseline")
    with transaction() as cur:
        assert visits.exists(cur, vid) is True
        assert visits.exists(cur, 9_999_999) is False
