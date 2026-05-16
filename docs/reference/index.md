# API reference

Every page below is auto-generated from the docstrings in
`src/noxdb/` by [mkdocstrings](https://mkdocstrings.github.io/).

## Modules

| Module                                       | Purpose                                                                 |
|----------------------------------------------|-------------------------------------------------------------------------|
| [`connection`](connection.md)                | Pool, `transaction()`, audit logging, SSH tunneling                     |
| [`projects`](projects.md)                    | CRUD for `projects`                                                     |
| [`subjects`](subjects.md)                    | CRUD for `subjects` (keyed on globally-unique `subject_code`)           |
| [`visits`](visits.md)                        | CRUD for `visits` (keyed on `(subject_id, timepoint)`)                  |
| [`samples`](samples.md)                      | CRUD for `samples` (`sample_name` globally UNIQUE)                      |
| [`metadata`](metadata.md)                    | EAV wrapper for `visit_metadata` and `sample_metadata`                  |
| [`files`](files.md)                          | Filesystem-validating registration for `sample_files`                   |
| [`queries`](queries.md)                      | Read-only joins, tidy tables, integrity checks                          |
| [`workflows`](workflows.md)                  | Atomic multi-table operations on top of the per-table CRUD              |
| [`fetch`](fetch.md)                          | Export project metadata + files to a local folder                       |
| [Project import](import.md)                  | Master importer: load a whole project folder atomically                 |

## Conventions

- All CRUD functions take a cursor as their first argument — the
  caller controls the transaction boundary via
  [`transaction()`][noxdb.connection.transaction].
- Single-row reads return `dict[str, Any] | None`; collection reads
  return `list[dict[str, Any]]`.
- `set_*` upserts return `"inserted" | "updated" | "unchanged"`.
- Writes go through `_LoggingCursor`, which audits to
  `~/.noxdb/audit.log` (override via `NOXDB_AUDIT_LOG`).
