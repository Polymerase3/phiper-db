"""Tests for noxdb.metadata.

Two tiers:

- ``TestEavSplit`` / ``TestRowToValue`` are pure-function unit tests and
  do not require a live MariaDB.
- The rest are integration tests that exercise the upsert / round-trip
  paths and need ``_init_pool`` from conftest.
"""

from __future__ import annotations

from decimal import Decimal

import mariadb
import pytest

from noxdb import metadata, projects, samples, subjects, transaction, visits
from noxdb.metadata import _eav_split, _row_to_value

from tests._helpers import wipe_all


# =========================================================================== #
# Unit tier — pure helpers, no DB required
# =========================================================================== #

class TestEavSplit:
    def test_true_routes_to_bool_branch(self):
        assert _eav_split(True) == ("bool", None, None, True, None)

    def test_false_routes_to_bool_branch(self):
        assert _eav_split(False) == ("bool", None, None, False, None)

    def test_zero_int_is_int_not_bool(self):
        # Regression: bool is a subclass of int, so the order of isinstance
        # checks matters. 0 must come back as int, not bool.
        assert _eav_split(0) == ("int", 0, None, None, None)

    def test_one_int_is_int_not_bool(self):
        assert _eav_split(1) == ("int", 1, None, None, None)

    def test_negative_int(self):
        assert _eav_split(-42) == ("int", -42, None, None, None)

    def test_float_routes_to_numeric(self):
        assert _eav_split(3.14) == ("numeric", None, 3.14, None, None)

    def test_zero_float_is_numeric(self):
        assert _eav_split(0.0) == ("numeric", None, 0.0, None, None)

    def test_str_routes_to_text(self):
        assert _eav_split("hello") == ("text", None, None, None, "hello")

    def test_empty_str_is_text_not_none(self):
        assert _eav_split("") == ("text", None, None, None, "")

    def test_none_raises_value_error(self):
        with pytest.raises(ValueError):
            _eav_split(None)

    def test_unsupported_type_raises_type_error(self):
        with pytest.raises(TypeError):
            _eav_split([1, 2])

    def test_dict_raises_type_error(self):
        with pytest.raises(TypeError):
            _eav_split({"k": "v"})


class TestRowToValue:
    def test_int_row(self):
        row = {"value_type": "int", "value_int": 42, "value_numeric": None,
               "value_bool": None, "value_text": None}
        assert _row_to_value(row) == 42

    def test_numeric_row_preserves_decimal(self):
        row = {"value_type": "numeric", "value_int": None,
               "value_numeric": Decimal("3.14"),
               "value_bool": None, "value_text": None}
        assert _row_to_value(row) == Decimal("3.14")
        assert isinstance(_row_to_value(row), Decimal)

    def test_bool_row_returns_python_bool(self):
        # Driver returns 0/1 because BOOLEAN is TINYINT(1).
        row = {"value_type": "bool", "value_int": None, "value_numeric": None,
               "value_bool": 1, "value_text": None}
        result = _row_to_value(row)
        assert result is True
        assert isinstance(result, bool)

    def test_bool_row_zero_returns_false(self):
        row = {"value_type": "bool", "value_int": None, "value_numeric": None,
               "value_bool": 0, "value_text": None}
        result = _row_to_value(row)
        assert result is False
        assert isinstance(result, bool)

    def test_text_row(self):
        row = {"value_type": "text", "value_int": None, "value_numeric": None,
               "value_bool": None, "value_text": "hi"}
        assert _row_to_value(row) == "hi"

    def test_unknown_value_type_raises(self):
        row = {"value_type": "garbage", "value_int": None, "value_numeric": None,
               "value_bool": None, "value_text": None}
        with pytest.raises(ValueError):
            _row_to_value(row)


# =========================================================================== #
# Integration tier — live MariaDB required
# =========================================================================== #

@pytest.fixture
def parent_ids(_init_pool):
    """One project / subject / visit / sample for metadata tests to attach to.

    Yields ``(visit_id, sample_id)``. Cleanup wipes everything via project
    cascade.
    """
    with transaction() as cur:
        wipe_all(cur)
        projects.create(cur, "MPROJ")
        sid = subjects.create(cur, "S1", "F")
        vid = visits.create(cur, sid, "control", 30, timepoint="baseline")
        smp = samples.create(cur, vid, "SMP1", "sample", "SQR1", "SQRP1", "libA")
    yield vid, smp
    with transaction() as cur:
        wipe_all(cur)


# --------------------------------------------------------------------------- #
# set_* return values & idempotency
# --------------------------------------------------------------------------- #

def test_set_visit_first_call_is_inserted(parent_ids):
    vid, _ = parent_ids
    with transaction() as cur:
        result = metadata.set_visit(cur, vid, "bmi", 22.7)
    assert result == "inserted"


def test_set_visit_same_value_is_unchanged(parent_ids):
    vid, _ = parent_ids
    with transaction() as cur:
        metadata.set_visit(cur, vid, "bmi", 22.7)
    with transaction() as cur:
        result = metadata.set_visit(cur, vid, "bmi", 22.7)
    assert result == "unchanged"


def test_set_visit_changed_value_is_updated(parent_ids):
    vid, _ = parent_ids
    with transaction() as cur:
        metadata.set_visit(cur, vid, "bmi", 22.7)
    with transaction() as cur:
        result = metadata.set_visit(cur, vid, "bmi", 23.5)
    assert result == "updated"
    with transaction() as cur:
        assert metadata.get_visit(cur, vid, "bmi") == Decimal("23.5")


def test_set_visit_type_switch_nulls_old_column(parent_ids):
    """Switching int -> text must NULL value_int and satisfy the CHECK."""
    vid, _ = parent_ids
    with transaction() as cur:
        metadata.set_visit(cur, vid, "score", 5)
    with transaction() as cur:
        result = metadata.set_visit(cur, vid, "score", "n/a")
    assert result == "updated"
    with transaction() as cur:
        cur.execute(
            "SELECT value_int, value_numeric, value_bool, value_text, value_type "
            "FROM visit_metadata WHERE visit_id = ? AND key_name = ?",
            (vid, "score"),
        )
        v_int, v_num, v_bool, v_text, v_type = cur.fetchone()
    assert v_int is None
    assert v_num is None
    assert v_bool is None
    assert v_text == "n/a"
    assert v_type == "text"


# --------------------------------------------------------------------------- #
# error paths
# --------------------------------------------------------------------------- #

def test_set_visit_none_raises_value_error(parent_ids):
    vid, _ = parent_ids
    with transaction() as cur:
        with pytest.raises(ValueError):
            metadata.set_visit(cur, vid, "k", None)


def test_set_visit_unsupported_type_raises(parent_ids):
    vid, _ = parent_ids
    with transaction() as cur:
        with pytest.raises(TypeError):
            metadata.set_visit(cur, vid, "k", [1, 2])


def test_set_visit_unknown_visit_id_raises_integrity_error(parent_ids):
    with pytest.raises(mariadb.IntegrityError):
        with transaction() as cur:
            metadata.set_visit(cur, 9_999_999, "k", 1)


# --------------------------------------------------------------------------- #
# round-trip per type
# --------------------------------------------------------------------------- #

def test_round_trip_int(parent_ids):
    vid, _ = parent_ids
    with transaction() as cur:
        metadata.set_visit(cur, vid, "k", 42)
    with transaction() as cur:
        v = metadata.get_visit(cur, vid, "k")
    assert v == 42
    assert isinstance(v, int)
    assert not isinstance(v, bool)


def test_round_trip_int_zero(parent_ids):
    vid, _ = parent_ids
    with transaction() as cur:
        metadata.set_visit(cur, vid, "k", 0)
    with transaction() as cur:
        v = metadata.get_visit(cur, vid, "k")
    assert v == 0
    assert isinstance(v, int)
    assert not isinstance(v, bool)


def test_round_trip_bool_true(parent_ids):
    vid, _ = parent_ids
    with transaction() as cur:
        metadata.set_visit(cur, vid, "smoker", True)
    with transaction() as cur:
        v = metadata.get_visit(cur, vid, "smoker")
    assert v is True
    assert isinstance(v, bool)


def test_round_trip_bool_false(parent_ids):
    vid, _ = parent_ids
    with transaction() as cur:
        metadata.set_visit(cur, vid, "smoker", False)
    with transaction() as cur:
        v = metadata.get_visit(cur, vid, "smoker")
    assert v is False
    assert isinstance(v, bool)


def test_round_trip_numeric_returns_decimal(parent_ids):
    vid, _ = parent_ids
    with transaction() as cur:
        metadata.set_visit(cur, vid, "bmi", 22.7)
    with transaction() as cur:
        v = metadata.get_visit(cur, vid, "bmi")
    assert isinstance(v, Decimal)
    assert v == Decimal("22.7")


def test_round_trip_text(parent_ids):
    vid, _ = parent_ids
    with transaction() as cur:
        metadata.set_visit(cur, vid, "arm", "treatment")
    with transaction() as cur:
        assert metadata.get_visit(cur, vid, "arm") == "treatment"


def test_round_trip_empty_string(parent_ids):
    vid, _ = parent_ids
    with transaction() as cur:
        metadata.set_visit(cur, vid, "note", "")
    with transaction() as cur:
        assert metadata.get_visit(cur, vid, "note") == ""


# --------------------------------------------------------------------------- #
# get / list / delete
# --------------------------------------------------------------------------- #

def test_get_visit_missing_key_returns_none(parent_ids):
    vid, _ = parent_ids
    with transaction() as cur:
        assert metadata.get_visit(cur, vid, "nope") is None


def test_list_for_visit_empty(parent_ids):
    vid, _ = parent_ids
    with transaction() as cur:
        assert metadata.list_for_visit(cur, vid) == {}


def test_list_for_visit_returns_all_keys(parent_ids):
    vid, _ = parent_ids
    with transaction() as cur:
        metadata.set_visit(cur, vid, "bmi", 22.7)
        metadata.set_visit(cur, vid, "smoker", False)
        metadata.set_visit(cur, vid, "arm", "treatment")
        metadata.set_visit(cur, vid, "score", 5)
    with transaction() as cur:
        result = metadata.list_for_visit(cur, vid)
    assert set(result) == {"bmi", "smoker", "arm", "score"}
    assert result["bmi"] == Decimal("22.7")
    assert result["smoker"] is False
    assert result["arm"] == "treatment"
    assert result["score"] == 5


def test_delete_visit_returns_true_when_removed(parent_ids):
    vid, _ = parent_ids
    with transaction() as cur:
        metadata.set_visit(cur, vid, "k", 1)
    with transaction() as cur:
        assert metadata.delete_visit(cur, vid, "k") is True
        assert metadata.get_visit(cur, vid, "k") is None


def test_delete_visit_missing_returns_false(parent_ids):
    vid, _ = parent_ids
    with transaction() as cur:
        assert metadata.delete_visit(cur, vid, "nope") is False


# --------------------------------------------------------------------------- #
# sample-metadata variants — same paths against the other target
# --------------------------------------------------------------------------- #

def test_set_sample_round_trips_all_types(parent_ids):
    _, smp = parent_ids
    with transaction() as cur:
        metadata.set_sample(cur, smp, "well", "A01")
        metadata.set_sample(cur, smp, "dilution", 1.5)
        metadata.set_sample(cur, smp, "passed_qc", True)
        metadata.set_sample(cur, smp, "read_length", 150)
    with transaction() as cur:
        result = metadata.list_for_sample(cur, smp)
    assert result["well"] == "A01"
    assert result["dilution"] == Decimal("1.5")
    assert result["passed_qc"] is True
    assert result["read_length"] == 150


def test_set_sample_idempotent(parent_ids):
    _, smp = parent_ids
    with transaction() as cur:
        assert metadata.set_sample(cur, smp, "k", 1) == "inserted"
    with transaction() as cur:
        assert metadata.set_sample(cur, smp, "k", 1) == "unchanged"
    with transaction() as cur:
        assert metadata.set_sample(cur, smp, "k", 2) == "updated"


def test_set_sample_unknown_id_raises(parent_ids):
    with pytest.raises(mariadb.IntegrityError):
        with transaction() as cur:
            metadata.set_sample(cur, 9_999_999, "k", 1)


def test_delete_sample(parent_ids):
    _, smp = parent_ids
    with transaction() as cur:
        metadata.set_sample(cur, smp, "k", 1)
    with transaction() as cur:
        assert metadata.delete_sample(cur, smp, "k") is True
        assert metadata.get_sample(cur, smp, "k") is None


def test_visit_and_sample_targets_are_isolated(parent_ids):
    """Same key on both targets must not collide."""
    vid, smp = parent_ids
    with transaction() as cur:
        metadata.set_visit(cur, vid, "shared_key", "visit_value")
        metadata.set_sample(cur, smp, "shared_key", "sample_value")
    with transaction() as cur:
        assert metadata.get_visit(cur, vid, "shared_key") == "visit_value"
        assert metadata.get_sample(cur, smp, "shared_key") == "sample_value"
