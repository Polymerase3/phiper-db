"""EAV metadata wrapper for `visit_metadata` and `sample_metadata`.

Both tables share the same shape: four typed value columns
(``value_int``, ``value_numeric``, ``value_bool``, ``value_text``) plus a
``value_type`` discriminator and a CHECK constraint that requires exactly
one value column to be populated and to match ``value_type``. This module
hides the encoding behind a small key-value API:

    metadata.set_visit(cur, visit_id, "bmi", 22.7)        # numeric
    metadata.set_visit(cur, visit_id, "smoker", False)    # bool
    metadata.get_visit(cur, visit_id, "bmi")              # -> Decimal('22.700000')
    metadata.list_for_visit(cur, visit_id)                # -> dict[str, Any]
    metadata.delete_visit(cur, visit_id, "bmi")           # -> bool

Equivalent ``_sample`` functions exist for ``sample_metadata``.

Python types map to value columns as follows. Order matters: ``bool`` is a
subclass of ``int``, so it is checked first.

    bool   -> value_bool      (value_type='bool')
    int    -> value_int       (value_type='int')
    float  -> value_numeric   (value_type='numeric')
    str    -> value_text      (value_type='text')

Numeric values are stored as ``DECIMAL(20,6)``; the driver returns them
as ``decimal.Decimal``. Round-trips therefore convert ``float`` -> ``Decimal``.

Writes use ``INSERT ... ON DUPLICATE KEY UPDATE`` against the existing
UNIQUE on ``(parent_id, key_name)``, so :func:`set_visit` and
:func:`set_sample` are idempotent. The function returns one of
``"inserted"``, ``"updated"``, or ``"unchanged"`` based on ``cur.rowcount``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal, NamedTuple

SetResult = Literal["inserted", "updated", "unchanged"]


class _Target(NamedTuple):
    table: str
    parent_col: str


_VISIT = _Target("visit_metadata", "visit_id")
_SAMPLE = _Target("sample_metadata", "sample_id")


# --------------------------------------------------------------------------- #
# Pure helpers (unit-testable without a DB)
# --------------------------------------------------------------------------- #

def _eav_split(value: Any) -> tuple[str, int | None, Any, bool | None, str | None]:
    """Split *value* into ``(value_type, v_int, v_numeric, v_bool, v_text)``.

    ``bool`` is checked before ``int`` because ``isinstance(True, int)`` is
    True in Python — flipping the order would silently misclassify booleans
    as integers.

    Raises ``ValueError`` for ``None`` (the schema CHECK forbids all-NULL
    rows; failing here makes the error legible). Raises ``TypeError`` for
    any other unsupported type.
    """
    if value is None:
        raise ValueError(
            "metadata values cannot be None; use delete_visit/delete_sample "
            "to remove an entry"
        )
    if isinstance(value, bool):
        return ("bool", None, None, value, None)
    if isinstance(value, int):
        return ("int", value, None, None, None)
    if isinstance(value, float):
        return ("numeric", None, value, None, None)
    if isinstance(value, str):
        return ("text", None, None, None, value)
    raise TypeError(f"Unsupported metadata value type: {type(value).__name__}")


def _row_to_value(row: dict[str, Any]) -> Any:
    """Inverse of :func:`_eav_split`. Picks the populated column by
    ``value_type`` and coerces ``value_bool`` back to a Python ``bool``
    (the driver returns ``0``/``1`` because BOOLEAN is TINYINT(1))."""
    vt = row["value_type"]
    if vt == "int":
        return row["value_int"]
    if vt == "numeric":
        return row["value_numeric"]
    if vt == "bool":
        return bool(row["value_bool"])
    if vt == "text":
        return row["value_text"]
    raise ValueError(f"Unknown value_type in metadata row: {vt!r}")


# --------------------------------------------------------------------------- #
# Shared DB paths
# --------------------------------------------------------------------------- #

def _set(cur, target: _Target, parent_id: int, key: str, value: Any) -> SetResult:
    vt, vi, vn, vb, vtxt = _eav_split(value)
    cur.execute(
        f"INSERT INTO {target.table} "
        f"({target.parent_col}, key_name, value_int, value_numeric, "
        "value_bool, value_text, value_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON DUPLICATE KEY UPDATE "
        "value_int = VALUES(value_int), "
        "value_numeric = VALUES(value_numeric), "
        "value_bool = VALUES(value_bool), "
        "value_text = VALUES(value_text), "
        "value_type = VALUES(value_type)",
        (parent_id, key, vi, vn, vb, vtxt, vt),
    )
    # MariaDB INSERT ... ON DUPLICATE KEY UPDATE rowcount:
    #   1 = row inserted, 2 = row updated, 0 = row matched but unchanged.
    rc = cur.rowcount
    if rc == 1:
        return "inserted"
    if rc == 2:
        return "updated"
    return "unchanged"


def _get(cur, target: _Target, parent_id: int, key: str) -> Any:
    cur.execute(
        f"SELECT value_int, value_numeric, value_bool, value_text, value_type "
        f"FROM {target.table} "
        f"WHERE {target.parent_col} = ? AND key_name = ?",
        (parent_id, key),
    )
    row = cur.fetchone()
    if row is None:
        return None
    columns = [d[0] for d in cur.description]
    return _row_to_value(dict(zip(columns, row)))


def _list(cur, target: _Target, parent_id: int) -> dict[str, Any]:
    cur.execute(
        f"SELECT key_name, value_int, value_numeric, value_bool, value_text, "
        f"value_type FROM {target.table} WHERE {target.parent_col} = ?",
        (parent_id,),
    )
    rows = cur.fetchall()
    columns = [d[0] for d in cur.description]
    out: dict[str, Any] = {}
    for row in rows:
        d = dict(zip(columns, row))
        out[d["key_name"]] = _row_to_value(d)
    return out


def _delete(cur, target: _Target, parent_id: int, key: str) -> bool:
    cur.execute(
        f"DELETE FROM {target.table} WHERE {target.parent_col} = ? AND key_name = ?",
        (parent_id, key),
    )
    return cur.rowcount > 0


# --------------------------------------------------------------------------- #
# Public API: visit_metadata
# --------------------------------------------------------------------------- #

def set_visit(cur, visit_id: int, key: str, value: Any) -> SetResult:
    """Upsert ``(visit_id, key)`` with *value*. Idempotent.

    Returns ``"inserted"`` on first write, ``"updated"`` when the value
    changed, ``"unchanged"`` when the row already had the same value.
    Raises ``ValueError`` for ``None`` values and ``TypeError`` for
    unsupported types.
    """
    return _set(cur, _VISIT, visit_id, key, value)


def get_visit(cur, visit_id: int, key: str) -> Any:
    """Return the native value for ``(visit_id, key)``, or ``None`` if not set."""
    return _get(cur, _VISIT, visit_id, key)


def list_for_visit(cur, visit_id: int) -> dict[str, Any]:
    """Return all metadata for *visit_id* as ``{key: value, ...}``."""
    return _list(cur, _VISIT, visit_id)


def delete_visit(cur, visit_id: int, key: str) -> bool:
    """Delete ``(visit_id, key)``. Returns True iff a row was removed."""
    return _delete(cur, _VISIT, visit_id, key)


# --------------------------------------------------------------------------- #
# Public API: sample_metadata
# --------------------------------------------------------------------------- #

def set_sample(cur, sample_id: int, key: str, value: Any) -> SetResult:
    """Upsert ``(sample_id, key)`` with *value*. See :func:`set_visit`."""
    return _set(cur, _SAMPLE, sample_id, key, value)


def get_sample(cur, sample_id: int, key: str) -> Any:
    """Return the native value for ``(sample_id, key)``, or ``None``."""
    return _get(cur, _SAMPLE, sample_id, key)


def list_for_sample(cur, sample_id: int) -> dict[str, Any]:
    """Return all metadata for *sample_id* as ``{key: value, ...}``."""
    return _list(cur, _SAMPLE, sample_id)


def delete_sample(cur, sample_id: int, key: str) -> bool:
    """Delete ``(sample_id, key)``. Returns True iff a row was removed."""
    return _delete(cur, _SAMPLE, sample_id, key)
