"""Integration tests for noxdb.subjects (require a live MariaDB).

Since schema 003 subjects carry no project affiliation: ``subject_code``
is globally UNIQUE and project membership lives in ``project_samples``,
reached via the sample → visit → subject lineage. ``list_for_project`` /
``count_for_project`` therefore traverse that junction, so the tests for
them seed a full subject → visit → sample chain and link the sample to a
project.
"""

from __future__ import annotations

import mariadb
import pytest

from noxdb import projects, subjects, transaction

from tests._helpers import wipe_all


@pytest.fixture
def two_projects(_init_pool):
    """Two empty projects to scope subjects against. Cleaned up at the end.

    ``wipe_all`` clears subjects (cascading visits/samples/metadata and
    project_samples) and projects, so each test starts from a clean slate.
    """
    with transaction() as cur:
        wipe_all(cur)
        a = projects.create(cur, "PROJ_A")
        b = projects.create(cur, "PROJ_B")
    yield a, b
    with transaction() as cur:
        wipe_all(cur)


def _seed_linked_subject(
    cur,
    project_id: int,
    subject_code: str,
    sex: str = "F",
    *,
    origin: str | None = None,
) -> int:
    """Create subject → visit → sample and link the sample to a project.

    Returns the new ``subject_id``. Used by the junction-traversal tests
    (``list_for_project`` / ``count_for_project``).
    """
    sid = subjects.create(cur, subject_code, sex, origin=origin)
    cur.execute(
        "INSERT INTO visits (subject_id, timepoint, group_test, age) "
        "VALUES (?, ?, ?, ?)",
        (sid, "t0", "ctrl", 30),
    )
    vid = cur.lastrowid
    cur.execute(
        "INSERT INTO samples "
        "(visit_id, sample_name, sample_type, SQR, SQRP, library) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (vid, f"{subject_code}_S1", "sample", "Q", "Q", "libA"),
    )
    smid = cur.lastrowid
    cur.execute(
        "INSERT INTO project_samples (project_id, sample_id) VALUES (?, ?)",
        (project_id, smid),
    )
    return sid


# --------------------------------------------------------------------------- #
# create / get / get_by_code
# --------------------------------------------------------------------------- #

def test_create_returns_new_id_and_persists(two_projects):
    with transaction() as cur:
        sid = subjects.create(cur, "S1", "F", origin="Italy")
        assert isinstance(sid, int) and sid > 0
        row = subjects.get(cur, sid)
    assert row["subject_code"] == "S1"
    assert row["sex"] == "F"
    assert row["origin"] == "Italy"


def test_create_duplicate_subject_code_raises(two_projects):
    with transaction() as cur:
        subjects.create(cur, "DUP", "M")
    with pytest.raises(mariadb.IntegrityError):
        with transaction() as cur:
            subjects.create(cur, "DUP", "M")


def test_subject_code_is_globally_unique(two_projects):
    """subject_code no longer scoped to a project — the second insert of
    the same code collides regardless of which study it 'belongs' to."""
    with transaction() as cur:
        a_id = subjects.create(cur, "SHARED", "F")
        assert isinstance(a_id, int)
    with pytest.raises(mariadb.IntegrityError):
        with transaction() as cur:
            subjects.create(cur, "SHARED", "M")


def test_create_invalid_sex_raises(two_projects):
    with pytest.raises(mariadb.Error):
        with transaction() as cur:
            subjects.create(cur, "BADSEX", "X")


def test_get_missing_returns_none(two_projects):
    with transaction() as cur:
        assert subjects.get(cur, 9_999_999) is None


def test_get_by_code_missing_returns_none(two_projects):
    with transaction() as cur:
        assert subjects.get_by_code(cur, "nope") is None


# --------------------------------------------------------------------------- #
# get_or_create
# --------------------------------------------------------------------------- #

def test_get_or_create_inserts_when_missing(two_projects):
    with transaction() as cur:
        sid, created = subjects.get_or_create(cur, "GOC", "F", origin="PL")
    assert created is True
    with transaction() as cur:
        assert subjects.get(cur, sid)["origin"] == "PL"


def test_get_or_create_returns_existing_when_attrs_match(two_projects):
    with transaction() as cur:
        first_id = subjects.create(cur, "GOC2", "F", origin="orig")
    with transaction() as cur:
        sid, created = subjects.get_or_create(cur, "GOC2", "F", origin="orig")
    assert sid == first_id
    assert created is False


def test_get_or_create_raises_on_sex_conflict(two_projects):
    """A reused subject_code with a different sex is a likely
    cross-study collision — fail loudly, don't silently merge."""
    with transaction() as cur:
        subjects.create(cur, "GOC3", "F", origin="orig")
    with pytest.raises(ValueError, match="already exists with sex"):
        with transaction() as cur:
            subjects.get_or_create(cur, "GOC3", "M")


def test_get_or_create_raises_on_origin_conflict(two_projects):
    with transaction() as cur:
        subjects.create(cur, "GOC4", "F", origin="PL")
    with pytest.raises(ValueError, match="already exists with origin"):
        with transaction() as cur:
            subjects.get_or_create(cur, "GOC4", "F", origin="AT")


def test_get_or_create_reuses_when_incoming_attrs_are_none(two_projects):
    """An incoming NULL sex/origin asserts nothing, so reuse is fine."""
    with transaction() as cur:
        first_id = subjects.create(cur, "GOC5", "F", origin="orig")
    with transaction() as cur:
        sid, created = subjects.get_or_create(cur, "GOC5", None)
    assert sid == first_id
    assert created is False
    with transaction() as cur:
        row = subjects.get(cur, sid)
    assert row["sex"] == "F"
    assert row["origin"] == "orig"


# --------------------------------------------------------------------------- #
# list_for_project / count_for_project (traverse project_samples)
# --------------------------------------------------------------------------- #

def test_list_for_project_orders_by_subject_id(two_projects):
    pa, pb = two_projects
    with transaction() as cur:
        a1 = _seed_linked_subject(cur, pa, "A1", "F")
        a2 = _seed_linked_subject(cur, pa, "A2", "M")
        _seed_linked_subject(cur, pb, "B1", "F")
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
        _seed_linked_subject(cur, pa, "A1", "F")
        _seed_linked_subject(cur, pa, "A2", "M")
        _seed_linked_subject(cur, pb, "B1", "F")
    with transaction() as cur:
        assert subjects.count_for_project(cur, pa) == 2
        assert subjects.count_for_project(cur, pb) == 1


def test_count_for_project_distinct_subjects(two_projects):
    """A subject with several samples in a project counts once."""
    pa, _ = two_projects
    with transaction() as cur:
        sid = subjects.create(cur, "MULTI", "F")
        cur.execute(
            "INSERT INTO visits (subject_id, timepoint, group_test, age) "
            "VALUES (?, ?, ?, ?)",
            (sid, "t0", "ctrl", 30),
        )
        vid = cur.lastrowid
        for n in (1, 2, 3):
            cur.execute(
                "INSERT INTO samples "
                "(visit_id, sample_name, sample_type, SQR, SQRP, library) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (vid, f"MULTI_S{n}", "sample", "Q", "Q", "libA"),
            )
            cur.execute(
                "INSERT INTO project_samples (project_id, sample_id) "
                "VALUES (?, ?)",
                (pa, cur.lastrowid),
            )
    with transaction() as cur:
        assert subjects.count_for_project(cur, pa) == 1


# --------------------------------------------------------------------------- #
# update
# --------------------------------------------------------------------------- #

def test_update_partial_only_changes_provided_fields(two_projects):
    with transaction() as cur:
        sid = subjects.create(cur, "U1", "F", origin="orig")
    with transaction() as cur:
        changed = subjects.update(cur, sid, origin="updated")
    assert changed is True
    with transaction() as cur:
        row = subjects.get(cur, sid)
    assert row["origin"] == "updated"
    assert row["sex"] == "F"
    assert row["subject_code"] == "U1"


def test_update_with_all_none_is_noop(two_projects):
    with transaction() as cur:
        sid = subjects.create(cur, "U2", "F", origin="d")
    with transaction() as cur:
        changed = subjects.update(cur, sid)
    assert changed is False
    with transaction() as cur:
        assert subjects.get(cur, sid)["origin"] == "d"


def test_update_unknown_id_returns_false(two_projects):
    with transaction() as cur:
        assert subjects.update(cur, 9_999_999, origin="x") is False


def test_update_does_not_expose_project_id(two_projects):
    """The signature must not accept project_id — subjects carry no
    project affiliation under the cross-project samples schema."""
    with transaction() as cur:
        sid = subjects.create(cur, "MOVE", "F")
        with pytest.raises(TypeError):
            subjects.update(cur, sid, project_id=1)


# --------------------------------------------------------------------------- #
# delete / exists
# --------------------------------------------------------------------------- #

def test_delete_returns_true_when_row_removed(two_projects):
    with transaction() as cur:
        sid = subjects.create(cur, "D1", "F")
    with transaction() as cur:
        assert subjects.delete(cur, sid) is True
        assert subjects.get(cur, sid) is None


def test_delete_unknown_id_returns_false(two_projects):
    with transaction() as cur:
        assert subjects.delete(cur, 9_999_999) is False


def test_delete_cascades_to_visits(two_projects):
    with transaction() as cur:
        sid = subjects.create(cur, "DCASC", "F")
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
    with transaction() as cur:
        sid = subjects.create(cur, "E1", "F")
    with transaction() as cur:
        assert subjects.exists(cur, sid) is True
        assert subjects.exists(cur, 9_999_999) is False
