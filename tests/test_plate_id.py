"""Unit tests for SQR/SQRP canonicalization (no DB required)."""

from __future__ import annotations

import pytest

from noxdb.samples import canonical_plate_id
from noxdb._import import schema


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("01", "01"),        # padding preserved — it is the canonical shape
        (" 01 ", "01"),      # whitespace stripped
        ("", ""),            # empty → empty
        ("NA", ""),          # sentinel → canonical empty
        ("na", ""),
        ("N/A", ""),
        ("n/a", ""),
        (None, ""),
        ("Q1", "Q1"),        # non-numeric, non-sentinel → verbatim
        ("12", "12"),
    ],
)
def test_canonical_plate_id(raw, expected):
    assert canonical_plate_id(raw) == expected


def test_validate_plate_id_returns_canonical_and_no_warning_when_clean():
    canon, warn = schema.validate_plate_id("01", field="samples.csv row 2.sqr")
    assert canon == "01"
    assert warn is None


def test_validate_plate_id_warns_on_normalization():
    canon, warn = schema.validate_plate_id("NA", field="samples.csv row 2.sqrp")
    assert canon == ""
    assert warn is not None
    assert "normalized to ''" in warn


def test_validate_plate_id_whitespace_only_does_not_warn():
    """A pure whitespace difference is not worth a warning — the loader
    already strips cells, so it never reaches the DB un-stripped. Only
    the NA/empty sentinel collapse (a real semantic change) warns."""
    canon, warn = schema.validate_plate_id(" 07 ", field="samples.csv row 9.sqr")
    assert canon == "07"
    assert warn is None


def test_validate_plate_id_raises_when_too_long():
    with pytest.raises(ValueError, match="max is 10"):
        schema.validate_plate_id("ABCDEFGHIJK", field="samples.csv row 3.sqr")
