# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/).

Every pull request must bump the version in `pyproject.toml` and add a
matching entry below; this is enforced by `.github/workflows/pr-checks.yml`.

## [Unreleased]

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
