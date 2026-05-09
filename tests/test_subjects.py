"""Integration tests for dbmaria_utils.subjects (require a live MariaDB)."""

from __future__ import annotations

import mariadb
import pytest

from dbmaria_utils import projects, subjects, transaction

from tests._helpers import wipe_all


@pytest.fixture
def two_projects(_init_pool):
    """Two empty projects to anchor subjects against. Cleaned up at the end.

    Wiping projects cascades through subjects/visits/samples/metadata, so
    each test starts from a clean slate.
    """
    with transaction() as cur:
        wipe_all(cur)
        a = projects.create(cur, "PROJ_A")
        b = projects.create(cur, "PROJ_B")
    yield a, b
    with transaction() as cur:
        wipe_all(cur)


# --------------------------------------------------------------------------- #
# create / get / get_by_code
# --------------------------------------------------------------------------- #

def test_create_returns_new_id_and_persists(two_projects):
    pa, _ = two_projects
    with transaction() as cur:
        sid = subjects.create(cur, pa, "S1", "F", origin="Italy")
        assert isinstance(sid, int) and sid > 0
        row = subjects.get(cur, sid)
    assert row["project_id"] == pa
    assert row["subject_code"] == "S1"
    assert row["sex"] == "F"
    assert row["origin"] == "Italy"


def test_create_duplicate_within_project_raises(two_projects):
    pa, _ = two_projects
    with transaction() as cur:
        subjects.create(cur, pa, "DUP", "M")
    with pytest.raises(mariadb.IntegrityError):
        with transaction() as cur:
            subjects.create(cur, pa, "DUP", "M")


def test_same_code_allowed_across_projects(two_projects):
    pa, pb = two_projects
    with transaction() as cur:
        a_id = subjects.create(cur, pa, "SHARED", "F")
        b_id = subjects.create(cur, pb, "SHARED", "M")
    assert a_id != b_id
    with transaction() as cur:
        assert subjects.get_by_code(cur, pa, "SHARED")["sex"] == "F"
        assert subjects.get_by_code(cur, pb, "SHARED")["sex"] == "M"


def test_create_unknown_project_id_raises(two_projects):
    with pytest.raises(mariadb.IntegrityError):
        with transaction() as cur:
            subjects.create(cur, 9_999_999, "ORPHAN", "F")


def test_create_invalid_sex_raises(two_projects):
    pa, _ = two_projects
    with pytest.raises(mariadb.Error):
        with transaction() as cur:
            subjects.create(cur, pa, "BADSEX", "X")


def test_get_missing_returns_none(two_projects):
    with transaction() as cur:
        assert subjects.get(cur, 9_999_999) is None


def test_get_by_code_missing_returns_none(two_projects):
    pa, _ = two_projects
    with transaction() as cur:
        assert subjects.get_by_code(cur, pa, "nope") is None


# --------------------------------------------------------------------------- #
# get_or_create
# --------------------------------------------------------------------------- #

def test_get_or_create_inserts_when_missing(two_projects):
    pa, _ = two_projects
    with transaction() as cur:
        sid, created = subjects.get_or_create(cur, pa, "GOC", "F", origin="PL")
    assert created is True
    with transaction() as cur:
        assert subjects.get(cur, sid)["origin"] == "PL"


def test_get_or_create_returns_existing_without_modifying(two_projects):
    pa, _ = two_projects
    with transaction() as cur:
        first_id = subjects.create(cur, pa, "GOC2", "F", origin="orig")
    with transaction() as cur:
        sid, created = subjects.get_or_create(cur, pa, "GOC2", "M", origin="ignored")
    assert sid == first_id
    assert created is False
    with transaction() as cur:
        row = subjects.get(cur, sid)
    assert row["sex"] == "F"
    assert row["origin"] == "orig"


# --------------------------------------------------------------------------- #
# list_for_project / count_for_project
# --------------------------------------------------------------------------- #

def test_list_for_project_orders_by_subject_id(two_projects):
    pa, pb = two_projects
    with transaction() as cur:
        a1 = subjects.create(cur, pa, "A1", "F")
        a2 = subjects.create(cur, pa, "A2", "M")
        subjects.create(cur, pb, "B1", "F")
    with transaction() as cur:
        rows = subjects.list_for_project(cur, pa)
    assert [r["subject_id"] for r in rows] == [a1, a2]


def test_list_for_project_rejects_unknown_order_by(two_projects):
    pa, _ = two_projects
    with transaction() as cur:
        with pytest.raises(ValueError):
            subjects.list_for_project(cur, pa, order_by="; DROP TABLE subjects")


def test_count_for_project_isolated_per_project(two_projects):
    pa, pb = two_projects
    with transaction() as cur:
        subjects.create(cur, pa, "A1", "F")
        subjects.create(cur, pa, "A2", "M")
        subjects.create(cur, pb, "B1", "F")
    with transaction() as cur:
        assert subjects.count_for_project(cur, pa) == 2
        assert subjects.count_for_project(cur, pb) == 1


# --------------------------------------------------------------------------- #
# update
# --------------------------------------------------------------------------- #

def test_update_partial_only_changes_provided_fields(two_projects):
    pa, _ = two_projects
    with transaction() as cur:
        sid = subjects.create(cur, pa, "U1", "F", origin="orig")
    with transaction() as cur:
        changed = subjects.update(cur, sid, origin="updated")
    assert changed is True
    with transaction() as cur:
        row = subjects.get(cur, sid)
    assert row["origin"] == "updated"
    assert row["sex"] == "F"
    assert row["subject_code"] == "U1"


def test_update_with_all_none_is_noop(two_projects):
    pa, _ = two_projects
    with transaction() as cur:
        sid = subjects.create(cur, pa, "U2", "F", origin="d")
    with transaction() as cur:
        changed = subjects.update(cur, sid)
    assert changed is False
    with transaction() as cur:
        assert subjects.get(cur, sid)["origin"] == "d"


def test_update_unknown_id_returns_false(two_projects):
    with transaction() as cur:
        assert subjects.update(cur, 9_999_999, origin="x") is False


def test_update_does_not_expose_project_id(two_projects):
    """The signature must not accept project_id — moving subjects across
    projects is not a routine operation and is intentionally excluded."""
    pa, pb = two_projects
    with transaction() as cur:
        sid = subjects.create(cur, pa, "MOVE", "F")
        with pytest.raises(TypeError):
            subjects.update(cur, sid, project_id=pb)


# --------------------------------------------------------------------------- #
# delete / exists
# --------------------------------------------------------------------------- #

def test_delete_returns_true_when_row_removed(two_projects):
    pa, _ = two_projects
    with transaction() as cur:
        sid = subjects.create(cur, pa, "D1", "F")
    with transaction() as cur:
        assert subjects.delete(cur, sid) is True
        assert subjects.get(cur, sid) is None


def test_delete_unknown_id_returns_false(two_projects):
    with transaction() as cur:
        assert subjects.delete(cur, 9_999_999) is False


def test_delete_cascades_to_visits(two_projects):
    pa, _ = two_projects
    with transaction() as cur:
        sid = subjects.create(cur, pa, "DCASC", "F")
        cur.execute(
            "INSERT INTO visits (subject_id, timepoint, group_test, age) "
            "VALUES (?, ?, ?, ?)",
            (sid, "baseline", "control", 30),
        )
    with transaction() as cur:
        subjects.delete(cur, sid)
        cur.execute("SELECT COUNT(*) FROM visits WHERE subject_id = ?", (sid,))
        assert cur.fetchone()[0] == 0


def test_exists_by_id(two_projects):
    pa, _ = two_projects
    with transaction() as cur:
        sid = subjects.create(cur, pa, "E1", "F")
    with transaction() as cur:
        assert subjects.exists(cur, sid) is True
        assert subjects.exists(cur, 9_999_999) is False
