# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/).

Every pull request must bump the version in `pyproject.toml` and add a
matching entry below; this is enforced by `.github/workflows/pr-checks.yml`.

## [Unreleased]

## [0.2.0] - 2026-05-09

### Added
- Per-table CRUD modules in `dbmaria_utils`:
  - `projects` — `create`, `get`, `get_by_name`, `get_or_create`, `list_all`,
    `update`, `delete`, `exists` (id/name XOR), `count`.
  - `subjects` — same shape keyed on the composite `(project_id,
    subject_code)`, plus `list_for_project` / `count_for_project`.
  - `visits` — keyed on `(subject_id, timepoint)`; `get_or_create` rejects
    NULL timepoints because the UNIQUE doesn't deduplicate them.
  - `samples` — `sample_name` is globally UNIQUE; module mirrors the
    `projects` shape with `list_for_visit` / `count_for_visit`.
  - `metadata` — shared EAV wrapper for `visit_metadata` and
    `sample_metadata` with explicit `set_visit / get_visit /
    list_for_visit / delete_visit` (and `_sample` mirrors). Idempotent
    via `INSERT … ON DUPLICATE KEY UPDATE`; `set_*` returns
    `"inserted" | "updated" | "unchanged"`. Bool stored in BOOLEAN is
    coerced back to Python `bool` on read.
  - `files` — filesystem-validating registration for `sample_files`:
    absolute path + regular-file check, fastq/bam extension validation,
    file-type → tier derivation (archive vs work), `os.path.realpath`
    prefix check that blocks symlink escapes and sibling-prefix paths,
    optional chunked MD5 (`compute_md5`) or caller-supplied checksum,
    `register`, `get_or_register` (idempotent on `file_path` and
    UNIQUE-violation race-safe), `restat`, plus the standard CRUD
    helpers. Roots configurable via `LABDB_ARCHIVE_ROOT` /
    `LABDB_WORK_ROOT` (defaults `/lisc/archive`, `/lisc/work`).
  - `update(storage_tier=…)` enforces the same `file_type → tier`
    invariant as `register()` — flipping `archive` ↔ `work` is rejected;
    `scratch` / `external` overrides are still allowed.

### Changed
- CI now runs `pytest --cov` against `src/dbmaria_utils` (branch
  coverage), prints a missing-lines report in the workflow log, and
  uploads `coverage.xml` as a build artifact. `pytest-cov` is a new
  optional `test` dependency; coverage settings live under
  `[tool.coverage.*]` in `pyproject.toml`.
- `_LoggingCursor` and `execute()` pass `params` through unchanged when
  the caller provides an empty container; only `params is None` is
  substituted with `()`.
- `_log_if_write` now strips leading SQL comments and an optional
  `WITH …` CTE before classifying the statement, so audited writes are
  no longer missed when the query starts with comments or a CTE.
- `get_connection()` cleanup wraps `rollback()` and `close()` in their
  own try/except so the original DB exception is preserved; rollback /
  close failures are logged instead of masking it.

### Fixed
- `seed/load_fake_data.load()` now closes its cursor in a `try/finally`,
  preventing cursor leaks back into the pool.
- `.github/workflows/pr-checks.yml` version-extraction commands no
  longer abort the step on `grep` misses; the explicit `-z` guard now
  reports the error as intended.

## [0.1.1] - 2026-05-08

### Added
- Initial schema in `schema/001_initial.sql`: project → subject → visit →
  sample hierarchy with EAV metadata tables (`visit_metadata`,
  `sample_metadata`) and `sample_files` for tracking file pointers.
- User and role definitions in `users/users.sql` (admin / read-write /
  read-only tiers, restricted to `lisc.%` hosts).
- `dbmaria_utils` Python package with:
  - Connection pool (`init_pool`, `close_pool`, `get_connection`).
  - `transaction()` context manager with audit-logging cursor wrapper.
  - `execute()` helper returning rows as `list[dict]`.
  - Audit log of write statements at `~/.labdb/audit.log` (override via
    `LABDB_AUDIT_LOG`).
  - Credentials read from `~/.my.cnf` `[labdb]` section by default;
    overridable per-call via `init_pool(...)` keyword arguments and via the
    `LABDB_DATABASE` env var.
- Fake-data seed script (`seed/load_fake_data.py`) covering all four EAV
  value types and a longitudinal subject example.
- CI workflow running pytest against MariaDB 10.11.
