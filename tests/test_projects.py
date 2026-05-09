"""Integration tests for dbmaria_utils.projects (require a live MariaDB)."""

from __future__ import annotations

import mariadb
import pytest

from dbmaria_utils import projects, transaction


@pytest.fixture
def clean_projects(_init_pool):
    """Wipe the projects table before and after each test.

    `ON DELETE CASCADE` propagates through subjects/visits/samples/metadata,
    so this keeps every test isolated from the others without per-test
    bookkeeping on each child table.
    """
    with transaction() as cur:
        cur.execute("DELETE FROM projects")
    yield
    with transaction() as cur:
        cur.execute("DELETE FROM projects")


# --------------------------------------------------------------------------- #
# create / get / get_by_name
# --------------------------------------------------------------------------- #

def test_create_returns_new_id_and_persists(clean_projects):
    with transaction() as cur:
        pid = projects.create(cur, "P1", description="d", pi_name="pi")
        assert isinstance(pid, int) and pid > 0
        row = projects.get(cur, pid)
    assert row["project_name"] == "P1"
    assert row["description"] == "d"
    assert row["pi_name"] == "pi"
    assert row["created_at"] is not None


def test_create_duplicate_name_raises_integrity_error(clean_projects):
    with transaction() as cur:
        projects.create(cur, "DUP")
    with pytest.raises(mariadb.IntegrityError):
        with transaction() as cur:
            projects.create(cur, "DUP")


def test_get_missing_returns_none(clean_projects):
    with transaction() as cur:
        assert projects.get(cur, 9_999_999) is None


def test_get_by_name_missing_returns_none(clean_projects):
    with transaction() as cur:
        assert projects.get_by_name(cur, "nope") is None


def test_get_by_name_returns_row(clean_projects):
    with transaction() as cur:
        pid = projects.create(cur, "BYNAME", pi_name="x")
        row = projects.get_by_name(cur, "BYNAME")
    assert row["project_id"] == pid
    assert row["pi_name"] == "x"


# --------------------------------------------------------------------------- #
# get_or_create
# --------------------------------------------------------------------------- #

def test_get_or_create_inserts_when_missing(clean_projects):
    with transaction() as cur:
        pid, created = projects.get_or_create(cur, "GOC", pi_name="a")
    assert created is True
    with transaction() as cur:
        assert projects.get(cur, pid)["pi_name"] == "a"


def test_get_or_create_returns_existing_without_modifying(clean_projects):
    with transaction() as cur:
        first_id = projects.create(cur, "GOC2", pi_name="orig")
    with transaction() as cur:
        pid, created = projects.get_or_create(cur, "GOC2", pi_name="ignored")
    assert pid == first_id
    assert created is False
    with transaction() as cur:
        assert projects.get(cur, pid)["pi_name"] == "orig"


# --------------------------------------------------------------------------- #
# list_all / count
# --------------------------------------------------------------------------- #

def test_list_all_orders_by_project_id_by_default(clean_projects):
    with transaction() as cur:
        a = projects.create(cur, "A")
        b = projects.create(cur, "B")
        c = projects.create(cur, "C")
    with transaction() as cur:
        rows = projects.list_all(cur)
    assert [r["project_id"] for r in rows] == [a, b, c]


def test_list_all_rejects_unknown_order_by(clean_projects):
    with transaction() as cur:
        with pytest.raises(ValueError):
            projects.list_all(cur, order_by="; DROP TABLE projects")


def test_count_reflects_inserts(clean_projects):
    with transaction() as cur:
        assert projects.count(cur) == 0
        projects.create(cur, "C1")
        projects.create(cur, "C2")
    with transaction() as cur:
        assert projects.count(cur) == 2


# --------------------------------------------------------------------------- #
# update
# --------------------------------------------------------------------------- #

def test_update_partial_only_changes_provided_fields(clean_projects):
    with transaction() as cur:
        pid = projects.create(cur, "U1", description="orig_desc", pi_name="orig_pi")
    with transaction() as cur:
        changed = projects.update(cur, pid, description="new_desc")
    assert changed is True
    with transaction() as cur:
        row = projects.get(cur, pid)
    assert row["description"] == "new_desc"
    assert row["pi_name"] == "orig_pi"


def test_update_with_all_none_is_noop(clean_projects):
    with transaction() as cur:
        pid = projects.create(cur, "U2", description="d")
    with transaction() as cur:
        changed = projects.update(cur, pid)
    assert changed is False
    with transaction() as cur:
        assert projects.get(cur, pid)["description"] == "d"


def test_update_unknown_id_returns_false(clean_projects):
    with transaction() as cur:
        assert projects.update(cur, 9_999_999, description="x") is False


# --------------------------------------------------------------------------- #
# delete
# --------------------------------------------------------------------------- #

def test_delete_returns_true_when_row_removed(clean_projects):
    with transaction() as cur:
        pid = projects.create(cur, "D1")
    with transaction() as cur:
        assert projects.delete(cur, pid) is True
        assert projects.get(cur, pid) is None


def test_delete_unknown_id_returns_false(clean_projects):
    with transaction() as cur:
        assert projects.delete(cur, 9_999_999) is False


def test_delete_cascades_to_subjects(clean_projects):
    with transaction() as cur:
        pid = projects.create(cur, "DCASC")
        cur.execute(
            "INSERT INTO subjects (project_id, subject_code, sex) VALUES (?, ?, ?)",
            (pid, "S1", "F"),
        )
    with transaction() as cur:
        projects.delete(cur, pid)
        cur.execute("SELECT COUNT(*) FROM subjects WHERE project_id = ?", (pid,))
        assert cur.fetchone()[0] == 0


# --------------------------------------------------------------------------- #
# exists
# --------------------------------------------------------------------------- #

def test_exists_by_id(clean_projects):
    with transaction() as cur:
        pid = projects.create(cur, "E1")
    with transaction() as cur:
        assert projects.exists(cur, pid) is True
        assert projects.exists(cur, 9_999_999) is False


def test_exists_by_name(clean_projects):
    with transaction() as cur:
        projects.create(cur, "E2")
    with transaction() as cur:
        assert projects.exists(cur, name="E2") is True
        assert projects.exists(cur, name="missing") is False


def test_exists_requires_exactly_one_arg(clean_projects):
    with transaction() as cur:
        with pytest.raises(ValueError):
            projects.exists(cur)
        with pytest.raises(ValueError):
            projects.exists(cur, 1, name="x")
