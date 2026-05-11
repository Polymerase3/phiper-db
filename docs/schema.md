# Schema

The database stores **metadata and file pointers** for biological
samples. Bulk data (fastq, BAM, etc.) lives on disk under
`/lisc/archive` and `/lisc/work`; the database only knows where to
find it.

## Hierarchy: project → subject → visit → sample

- **project** — independent study or dataset.
- **subject** — one person/donor within a project. Stable attributes
  only (sex, origin). `subject_code` is unique per project.
- **visit** — one timepoint / collection event for a subject.
  Time-varying clinical metadata (age, group, timepoint) goes here.
- **sample** — one physical sample / library / Ig-class measurement
  attached to a visit. `sample_name` is globally unique.

Flexible typed key/value metadata can be attached to visits
(`visit_metadata`) and samples (`sample_metadata`). File paths are
tracked in `sample_files`.

## Tables at a glance

| Table             | Purpose                                          | Notable constraints                                        |
|-------------------|--------------------------------------------------|------------------------------------------------------------|
| `projects`        | Top-level study / dataset                        | `project_name` UNIQUE                                       |
| `subjects`        | Person / donor within a project                  | `(project_id, subject_code)` UNIQUE                         |
| `visits`          | One timepoint for a subject                      | `(subject_id, timepoint)` UNIQUE (NULL not deduped)         |
| `samples`         | Physical sample / library                        | `sample_name` UNIQUE **globally**                           |
| `visit_metadata`  | EAV typed key/value attached to a visit          | `(visit_id, meta_key)` UNIQUE; one typed value column wins  |
| `sample_metadata` | EAV typed key/value attached to a sample         | `(sample_id, meta_key)` UNIQUE; one typed value column wins |
| `sample_files`    | File pointers for a sample                       | `file_path` UNIQUE **globally**, absolute-path `CHECK`      |

## Naming conventions

- **Tables**: plural `snake_case` (`projects`, `samples`, `sample_files`).
- **Primary keys**: `<table_singular>_id` (e.g. `subject_id`).
- **Foreign keys**: reuse the parent PK name (`subjects.project_id`).
- **Migrations**: `schema/NNN_description.sql`, numbered and
  append-only. Never edit a merged migration — add the next number.
- **Stored file paths** must be absolute. Enforced by a `CHECK`
  constraint and re-validated in
  [`files.register`][dbmaria_utils.files.register].

## EAV metadata

`visit_metadata` and `sample_metadata` are
[Entity-Attribute-Value](https://en.wikipedia.org/wiki/Entity%E2%80%93attribute%E2%80%93value_model)
tables: each row stores one `(meta_key, value)` pair for one entity.
Each row carries exactly one of `value_int`, `value_float`,
`value_bool`, `value_text` — `value_type` says which.

Use [`metadata.set_visit`][dbmaria_utils.metadata.set_visit] /
[`metadata.set_sample`][dbmaria_utils.metadata.set_sample] for writes;
they are idempotent (`INSERT … ON DUPLICATE KEY UPDATE`) and return
`"inserted" | "updated" | "unchanged"`.

To get metadata back as wide-form columns, use
[`queries.samples_with_metadata`][dbmaria_utils.queries.samples_with_metadata]
or [`queries.project_tidy_table`][dbmaria_utils.queries.project_tidy_table].

## Storage tiers

`sample_files.storage_tier` enforces a `file_type → tier` invariant:

| `file_type`         | Required tier      | Root env var          | Default        |
|---------------------|--------------------|-----------------------|----------------|
| `fastq`, `bam`, …   | `archive`          | `LABDB_ARCHIVE_ROOT`  | `/lisc/archive`|
| derived / temp      | `work`             | `LABDB_WORK_ROOT`     | `/lisc/work`   |
| anything            | `scratch`, `external` | —                  | —              |

Flipping `archive` ↔ `work` on an existing row is rejected by
[`files.update`][dbmaria_utils.files.update]. `scratch` / `external`
overrides are still allowed for one-off cases.

## Where the SQL lives

- `schema/001_initial.sql` — initial schema; subsequent migrations
  numbered sequentially.
- `users/users.sql` — role and privilege definitions (the matching
  `users_with_passwords.sql` is gitignored).
- `seed/load_fake_data.py` — fake-data seed covering all four EAV
  value types and a longitudinal subject example.
