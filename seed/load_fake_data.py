"""Insert a small set of fake-but-realistic rows into ccr_metadata.

Covers: 1 project, 2 subjects (cross-sectional + longitudinal), several
samples per visit, sample_files rows, and visit_metadata / sample_metadata
entries covering all four EAV value types (int, numeric, bool, text).

Connection settings are read from environment variables:
    DB_HOST     (default: 127.0.0.1)
    DB_PORT     (default: 3306)
    DB_USER     (default: root)
    DB_PASSWORD (default: empty)
    DB_NAME     (default: ccr_metadata)
"""

from __future__ import annotations

import os
import sys

from noxdb import close_pool, get_connection, init_pool


def insert_project(cur) -> int:
    cur.execute(
        "INSERT INTO projects (project_name, description, pi_name) "
        "VALUES (?, ?, ?)",
        ("DEMO_PROJECT", "Seed/test data for CI", "Dr. Test"),
    )
    return cur.lastrowid


def insert_subject(cur, code: str, sex: str, origin: str) -> int:
    cur.execute(
        "INSERT INTO subjects (subject_code, sex, origin) "
        "VALUES (?, ?, ?)",
        (code, sex, origin),
    )
    return cur.lastrowid


def link_project_sample(cur, project_id: int, sample_id: int) -> None:
    """Register sample under project in the project_samples junction.

    Schema 003 makes project_samples the sole project↔sample link.
    """
    cur.execute(
        "INSERT IGNORE INTO project_samples (project_id, sample_id) "
        "VALUES (?, ?)",
        (project_id, sample_id),
    )


def insert_visit(
    cur, subject_id: int, timepoint: str, group_test: str, age: int
) -> int:
    cur.execute(
        "INSERT INTO visits (subject_id, timepoint, group_test, age) "
        "VALUES (?, ?, ?, ?)",
        (subject_id, timepoint, group_test, age),
    )
    return cur.lastrowid


def insert_sample(
    cur,
    visit_id: int,
    name: str,
    sample_type: str,
    sqr: str,
    sqrp: str,
    library: str,
    antibody_class: str | None,
) -> int:
    cur.execute(
        "INSERT INTO samples "
        "(visit_id, sample_name, sample_type, SQR, SQRP, library, antibody_class) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (visit_id, name, sample_type, sqr, sqrp, library, antibody_class),
    )
    return cur.lastrowid


def insert_sample_file(
    cur,
    sample_id: int,
    file_type: str,
    file_path: str,
    size_bytes: int,
    md5: str,
    tier: str = "work",
) -> int:
    cur.execute(
        "INSERT INTO sample_files "
        "(sample_id, file_type, file_path, file_size_bytes, checksum_md5, storage_tier) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (sample_id, file_type, file_path, size_bytes, md5, tier),
    )
    return cur.lastrowid


def insert_visit_metadata(cur, visit_id: int, key: str, value) -> None:
    vt, vi, vn, vb, vtxt = _eav_split(value)
    cur.execute(
        "INSERT INTO visit_metadata "
        "(visit_id, key_name, value_int, value_numeric, value_bool, value_text, value_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (visit_id, key, vi, vn, vb, vtxt, vt),
    )


def insert_sample_metadata(cur, sample_id: int, key: str, value) -> None:
    vt, vi, vn, vb, vtxt = _eav_split(value)
    cur.execute(
        "INSERT INTO sample_metadata "
        "(sample_id, key_name, value_int, value_numeric, value_bool, value_text, value_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sample_id, key, vi, vn, vb, vtxt, vt),
    )


def _eav_split(value):
    # bool must come before int — bool is a subclass of int in Python
    if isinstance(value, bool):
        return ("bool", None, None, value, None)
    if isinstance(value, int):
        return ("int", value, None, None, None)
    if isinstance(value, float):
        return ("numeric", None, value, None, None)
    if isinstance(value, str):
        return ("text", None, None, None, value)
    raise TypeError(f"Unsupported EAV value type: {type(value).__name__}")


def load(conn) -> None:
    cur = conn.cursor()
    try:
        _load_with_cursor(cur)
    finally:
        cur.close()


def _load_with_cursor(cur) -> None:
    project_id = insert_project(cur)

    # Subject 1 — cross-sectional, single visit
    s1 = insert_subject(cur, "SUBJ001", "F", "Italy")
    v1 = insert_visit(cur, s1, "baseline", "control", 34)

    # Subject 2 — longitudinal, three visits
    s2 = insert_subject(cur, "SUBJ002", "M", "Poland")
    v2a = insert_visit(cur, s2, "baseline", "treated", 41)
    v2b = insert_visit(cur, s2, "month_3", "treated", 41)
    v2c = insert_visit(cur, s2, "month_6", "treated", 42)

    # visit_metadata covering all four EAV types
    insert_visit_metadata(cur, v1, "bmi", 22.7)            # numeric
    insert_visit_metadata(cur, v1, "smoker", False)        # bool
    insert_visit_metadata(cur, v2a, "disease_score", 4)    # int
    insert_visit_metadata(cur, v2a, "treatment_arm", "A")  # text
    insert_visit_metadata(cur, v2b, "smoker", True)
    insert_visit_metadata(cur, v2c, "bmi", 24.1)

    # Samples — several per visit, mixing sample_type values
    samples_per_visit = {
        v1: [
            ("SUBJ001_BL_S1", "sample", "SQR1", "SQRP1", "libA", "IgG"),
            ("SUBJ001_BL_IN", "input",  "SQR1", "SQRP1", "libA", None),
            ("SUBJ001_BL_MK", "mockIP", "SQR1", "SQRP1", "libA", None),
        ],
        v2a: [
            ("SUBJ002_BL_S1", "sample", "SQR2", "SQRP2", "libB", "IgA"),
            ("SUBJ002_BL_AN", "anchor", "SQR2", "SQRP2", "libB", None),
        ],
        v2b: [
            ("SUBJ002_M3_S1", "sample", "SQR2", "SQRP2", "libB", "IgA"),
            ("SUBJ002_M3_IN", "input",  "SQR2", "SQRP2", "libB", None),
        ],
        v2c: [
            ("SUBJ002_M6_S1", "sample", "SQR2", "SQRP2", "libB", "IgM"),
            ("SUBJ002_M6_S2", "sample", "SQR2", "SQRP2", "libB", "IgG"),
        ],
    }

    sample_ids: list[int] = []
    for visit_id, rows in samples_per_visit.items():
        for name, stype, sqr, sqrp, lib, ab in rows:
            sid = insert_sample(cur, visit_id, name, stype, sqr, sqrp, lib, ab)
            sample_ids.append(sid)
            link_project_sample(cur, project_id, sid)

    # sample_metadata covering all four EAV types
    first = sample_ids[0]
    insert_sample_metadata(cur, first, "well",            "A01")     # text
    insert_sample_metadata(cur, first, "dilution_factor", 1.5)       # numeric
    insert_sample_metadata(cur, first, "passed_qc",       True)      # bool
    insert_sample_metadata(cur, first, "read_length",     150)       # int
    insert_sample_metadata(cur, sample_ids[1], "well", "A02")
    insert_sample_metadata(cur, sample_ids[1], "passed_qc", False)

    # sample_files — a few rows on the first sample
    insert_sample_file(
        cur, first, "fastq_r1",
        "/data/demo/SUBJ001_BL_S1_R1.fastq.gz",
        123_456_789,
        "0123456789abcdef0123456789abcdef",
    )
    insert_sample_file(
        cur, first, "fastq_r2",
        "/data/demo/SUBJ001_BL_S1_R2.fastq.gz",
        123_456_780,
        "fedcba9876543210fedcba9876543210",
    )
    insert_sample_file(
        cur, sample_ids[1], "bam",
        "/data/demo/SUBJ001_BL_IN.bam",
        987_654_321,
        "aaaabbbbccccddddeeeeffff00001111",
        "archive",
    )


def main() -> int:
    # Configure the pool from env vars so this script works in CI without
    # a ~/.my.cnf file. The context manager handles commit/rollback.
    init_pool(
        config_path=None,
        host=os.environ.get("DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ.get("DB_USER", "root"),
        password=os.environ.get("DB_PASSWORD", ""),
        database=os.environ.get("DB_NAME", "ccr_metadata"),
    )
    try:
        with get_connection() as conn:
            load(conn)
    finally:
        close_pool()
    return 0


if __name__ == "__main__":
    sys.exit(main())
