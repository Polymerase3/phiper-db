# Schema

The database stores **metadata and file pointers** for biological
samples. Bulk data (fastq, BAM, counts, etc.) lives on disk under
`/lisc/archive` and `/lisc/work`; the database only knows where to
find it.

---

## Hierarchy: project → subject → visit → sample

- **project** — independent study or dataset (including dedicated control projects).
- **subject** — one person/donor within a project. Stable attributes only (`sex`, `origin`). `subject_code` is unique per project. `sex` is nullable for control samples.
- **visit** — one timepoint / collection event for a subject. Time-varying clinical metadata (`age`, `group_test`, `timepoint`) goes here. `age` is nullable for control samples.
- **sample** — one physical sample / library / Ig-class measurement attached to a visit. `sample_name` is globally unique.

Flexible typed key/value metadata can be attached to visits (`visit_metadata`) and samples (`sample_metadata`). File paths are tracked in `sample_files`.

---

## Tables

### `projects`

Top-level study or dataset. Each control type (mockIP, anchor, NC, input) has its own dedicated project row — see [Controls](#controls) below.

| Column         | Type                      | Nullable | Notes                          |
|----------------|---------------------------|----------|--------------------------------|
| `project_id`   | `BIGINT UNSIGNED` PK AI   | NO       |                                |
| `project_name` | `VARCHAR(100)`            | NO       | UNIQUE                         |
| `description`  | `TEXT`                    | YES      |                                |
| `pi_name`      | `VARCHAR(100)`            | YES      |                                |
| `created_at`   | `TIMESTAMP`               | NO       | DEFAULT `CURRENT_TIMESTAMP`    |

---

### `subjects`

One row per person / donor within a project. Stable subject-level attributes only.

| Column         | Type                      | Nullable | Notes                                                  |
|----------------|---------------------------|----------|--------------------------------------------------------|
| `subject_id`   | `BIGINT UNSIGNED` PK AI   | NO       |                                                        |
| `project_id`   | `BIGINT UNSIGNED` FK      | NO       | → `projects.project_id` CASCADE                        |
| `subject_code` | `VARCHAR(100)`            | NO       | UNIQUE within project: `(project_id, subject_code)`    |
| `sex`          | `CHAR(1)`                 | YES      | `'M'` or `'F'` when set; NULL for control subjects     |
| `origin`       | `VARCHAR(100)`            | YES      |                                                        |
| `created_at`   | `TIMESTAMP`               | NO       | DEFAULT `CURRENT_TIMESTAMP`                            |

`sex` was made nullable in migration `002_controls_support` to accommodate control sample rows that have no biological donor.

---

### `visits`

One row per subject × timepoint / collection event. Time-varying clinical data belongs here.

| Column       | Type                      | Nullable | Notes                                                       |
|--------------|---------------------------|----------|-------------------------------------------------------------|
| `visit_id`   | `BIGINT UNSIGNED` PK AI   | NO       |                                                             |
| `subject_id` | `BIGINT UNSIGNED` FK      | NO       | → `subjects.subject_id` CASCADE                             |
| `timepoint`  | `VARCHAR(50)`             | YES      | UNIQUE within subject: `(subject_id, timepoint)`; NULL rows not deduplicated |
| `group_test` | `VARCHAR(100)`            | NO       | Clinical group / arm (e.g. `Controls`, `Cases`)             |
| `age`        | `INT`                     | YES      | Age at collection; ≥ 0 when set; NULL for control visits    |
| `created_at` | `TIMESTAMP`               | NO       | DEFAULT `CURRENT_TIMESTAMP`                                 |

`age` was made nullable in migration `002_controls_support`.

---

### `samples`

One row per physical sample / library / Ig-class measurement. `sample_name` is globally unique across all projects.

| Column           | Type                                                         | Nullable | Notes                                          |
|------------------|--------------------------------------------------------------|----------|------------------------------------------------|
| `sample_id`      | `BIGINT UNSIGNED` PK AI                                      | NO       |                                                |
| `visit_id`       | `BIGINT UNSIGNED` FK                                         | NO       | → `visits.visit_id` CASCADE                    |
| `sample_name`    | `VARCHAR(100)`                                               | NO       | UNIQUE globally                                |
| `sample_type`    | `ENUM('sample','mockIP','input','anchor','NC')`              | NO       | See [Controls](#controls)                      |
| `SQR`            | `VARCHAR(10)`                                                | NO       | Sequencing run identifier — plate-level key    |
| `SQRP`           | `VARCHAR(10)`                                                | NO       | Sequencing run plate — plate-level key         |
| `library`        | `VARCHAR(50)`                                                | NO       |                                                |
| `antibody_class` | `VARCHAR(50)`                                                | YES      |                                                |
| `created_at`     | `TIMESTAMP`                                                  | NO       | DEFAULT `CURRENT_TIMESTAMP`                    |

`NC` was added to the `sample_type` ENUM in migration `002_controls_support`.

---

### `visit_metadata`

EAV (Entity-Attribute-Value) typed key/value pairs attached to a visit. Examples: BMI, smoker status, disease activity, treatment status.

| Column        | Type                                   | Nullable | Notes                                      |
|---------------|----------------------------------------|----------|--------------------------------------------|
| `id`          | `BIGINT UNSIGNED` PK AI                | NO       |                                            |
| `visit_id`    | `BIGINT UNSIGNED` FK                   | NO       | → `visits.visit_id` CASCADE                |
| `key_name`    | `VARCHAR(100)`                         | NO       | UNIQUE within visit: `(visit_id, key_name)` |
| `value_int`   | `INTEGER`                              | YES      | Set when `value_type = 'int'`              |
| `value_numeric` | `DECIMAL(20,6)`                      | YES      | Set when `value_type = 'numeric'`          |
| `value_bool`  | `BOOLEAN`                              | YES      | Set when `value_type = 'bool'`             |
| `value_text`  | `TEXT`                                 | YES      | Set when `value_type = 'text'`             |
| `value_type`  | `ENUM('int','numeric','bool','text')`  | NO       | Discriminator; exactly one value column is non-NULL (enforced by CHECK) |
| `created_at`  | `TIMESTAMP`                            | NO       | DEFAULT `CURRENT_TIMESTAMP`                |

---

### `sample_metadata`

EAV typed key/value pairs attached to a sample. Examples: well position, plate barcode, dilution factor, `passed_qc`.

| Column        | Type                                   | Nullable | Notes                                        |
|---------------|----------------------------------------|----------|----------------------------------------------|
| `id`          | `BIGINT UNSIGNED` PK AI                | NO       |                                              |
| `sample_id`   | `BIGINT UNSIGNED` FK                   | NO       | → `samples.sample_id` CASCADE                |
| `key_name`    | `VARCHAR(100)`                         | NO       | UNIQUE within sample: `(sample_id, key_name)` |
| `value_int`   | `INTEGER`                              | YES      | Set when `value_type = 'int'`                |
| `value_numeric` | `DECIMAL(20,6)`                      | YES      | Set when `value_type = 'numeric'`            |
| `value_bool`  | `BOOLEAN`                              | YES      | Set when `value_type = 'bool'`               |
| `value_text`  | `TEXT`                                 | YES      | Set when `value_type = 'text'`               |
| `value_type`  | `ENUM('int','numeric','bool','text')`  | NO       | Discriminator; exactly one value column is non-NULL (enforced by CHECK) |
| `created_at`  | `TIMESTAMP`                            | NO       | DEFAULT `CURRENT_TIMESTAMP`                  |

---

### `sample_files`

File pointers registered for a sample. The database never stores file content — only the absolute path and optional metadata.

| Column            | Type                                                                                    | Nullable | Notes                                                      |
|-------------------|-----------------------------------------------------------------------------------------|----------|------------------------------------------------------------|
| `file_id`         | `BIGINT UNSIGNED` PK AI                                                                 | NO       |                                                            |
| `sample_id`       | `BIGINT UNSIGNED` FK                                                                    | NO       | → `samples.sample_id` RESTRICT on delete                   |
| `file_type`       | `ENUM('fastq_r1','fastq_r2','fastq_single','bam','counts','beer_norm','zigp_norm','edger_norm')` | NO |                                                     |
| `file_path`       | `VARCHAR(1024)`                                                                         | NO       | UNIQUE globally; must be absolute (enforced by CHECK `LIKE '/%'`) |
| `file_size_bytes` | `BIGINT UNSIGNED`                                                                       | YES      |                                                            |
| `checksum_md5`    | `CHAR(32)`                                                                              | YES      | Must match `^[a-f0-9]{32}$` when set                       |
| `storage_tier`    | `ENUM('work','archive','scratch','external')`                                           | NO       | DEFAULT `'work'`; see [Storage tiers](#storage-tiers)      |
| `created_at`      | `TIMESTAMP`                                                                             | NO       | DEFAULT `CURRENT_TIMESTAMP`                                |

Deleting a sample that still has files is rejected (`ON DELETE RESTRICT`). Files must be deregistered first.

---

## Controls

Control samples (mockIP, anchor, NC, input) are **not stored inside study projects**. Each control type lives in its own dedicated project:

| `project_id` | `project_name` | `sample_type` | Purpose                                   |
|--------------|----------------|---------------|-------------------------------------------|
| 58           | `input`        | `input`       | Input DNA samples (no IP)                 |
| 61           | `mockIP`       | `mockIP`      | Mock immunoprecipitation controls         |
| 64           | `anchor`       | `anchor`      | Anchor controls (spike-in normalization)  |
| 67           | `NC`           | `NC`          | Negative controls                         |

### How controls are linked to study projects

Controls are matched back to a study project via the **plate coordinates** `SQR` (sequencing run) and `SQRP` (plate within that run), which every sample row carries regardless of type. A control sample processed on the same plate as a study project will share the same `SQR` + `SQRP` values.

`queries.samples_for_project` and `queries.controls_for_project` exploit this: they join through the `SQR`/`SQRP` columns to assemble the complete set of controls associated with a given project without duplicating any rows.

Because a plate can span more than one study project, the same control sample may appear in the result sets of multiple projects — this is intentional, not a data quality issue.

### Nulls in control rows

Control subjects have no biological donor, so `subjects.sex` and `visits.age` are NULL for all control rows. The non-null CHECK constraints on both columns were loosened in migration `002_controls_support` to permit this:

- `subjects.sex`: NULL or `'M'`/`'F'`
- `visits.age`: NULL or `>= 0`

---

## EAV metadata

`visit_metadata` and `sample_metadata` follow the
[Entity-Attribute-Value](https://en.wikipedia.org/wiki/Entity%E2%80%93attribute%E2%80%93value_model)
pattern: each row stores one `(key_name, value)` pair for one entity.
Exactly one of `value_int`, `value_numeric`, `value_bool`, `value_text` is non-NULL per row — `value_type` says which. A CHECK constraint enforces this.

Use [`metadata.set_visit`][noxdb.metadata.set_visit] /
[`metadata.set_sample`][noxdb.metadata.set_sample] for writes; they are
idempotent (`INSERT … ON DUPLICATE KEY UPDATE`) and return
`"inserted" | "updated" | "unchanged"`.

To get metadata back as wide-form columns, use
[`queries.samples_with_metadata`][noxdb.queries.samples_with_metadata]
or [`queries.project_tidy_table`][noxdb.queries.project_tidy_table].

---

## Storage tiers

`sample_files.storage_tier` enforces a `file_type → tier` invariant:

| `file_type`                          | Required tier         | Root env var          | Default         |
|--------------------------------------|-----------------------|-----------------------|-----------------|
| `fastq_r1`, `fastq_r2`, `fastq_single`, `bam` | `archive` | `NOXDB_ARCHIVE_ROOT`  | `/lisc/archive` |
| `counts`, `beer_norm`, `zigp_norm`, `edger_norm` | `work`  | `NOXDB_WORK_ROOT`     | `/lisc/work`    |
| anything                             | `scratch`, `external` | —                     | —               |

Flipping `archive` ↔ `work` on an existing row is rejected by
[`files.update`][noxdb.files.update]. `scratch` / `external` overrides
are still allowed for one-off cases.

---

## Naming conventions

- **Tables**: plural `snake_case` (`projects`, `samples`, `sample_files`).
- **Primary keys**: `<table_singular>_id` (e.g. `subject_id`); EAV tables use plain `id`.
- **Foreign keys**: reuse the parent PK name (`subjects.project_id`).
- **Migrations**: `schema/NNN_description.sql`, numbered and append-only. Never edit a merged migration — add the next number.
- **Stored file paths** must be absolute. Enforced by a CHECK constraint and re-validated in [`files.register`][noxdb.files.register].

---

## Where the SQL lives

- `schema/001_initial.sql` — initial schema.
- `schema/002_controls_support.sql` — nullable `sex`/`age` for control rows; adds `NC` to `sample_type` ENUM.
- `users/users.sql` — role and privilege definitions (the matching `users_with_passwords.sql` is gitignored).
- `seed/load_fake_data.py` — fake-data seed covering all four EAV value types and a longitudinal subject example.
