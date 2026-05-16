# noxDB

Schema, migrations, and Python tooling for `ccr_metadata`, the lab's
MariaDB metadata database (MariaDB ≥ 10, InnoDB, Galera). The database
stores **metadata and file pointers**; bulk data lives on disk.

## What's in here

<div class="grid cards" markdown>

-   :material-download:{ .lg .middle } **[Install](install.md)**

    ---
    Install the package, configure `~/.my.cnf`, and connect through the
    LiSC SSH jump host.

-   :material-rocket-launch:{ .lg .middle } **[Quickstart](quickstart.md)**

    ---
    End-to-end example: open a pool, register a subject + visit + sample,
    export a project.

-   :material-database:{ .lg .middle } **[Schema](schema.md)**

    ---
    The `subject → visit → sample` lineage, the `project_samples`
    membership junction, table layout,
    and naming conventions.

-   :material-table-edit:{ .lg .middle } **[Preparing data](data-preparation.md)**

    ---
    How to structure your data before loading it into the database.

-   :material-test-tube:{ .lg .middle } **[Testing](testing.md)**

    ---
    Running the test suite and writing new tests.

-   :material-hand-heart:{ .lg .middle } **[Contributing](contributing.md)**

    ---
    Contribution guidelines, branching model, and release process.

-   :material-book-open-variant:{ .lg .middle } **[API reference](reference/index.md)**

    ---
    Auto-generated from docstrings for every public module.

-   :material-history:{ .lg .middle } **[Changelog](changelog.md)**

    ---
    Versioned release notes (Keep a Changelog format).

</div>

## Model

Two independent axes (since migration `003_cross_project_samples`):

```
lineage:     subject ──┬── visit ──── sample ──┬── sample_files
                        │              │        └── sample_metadata (EAV)
                        │              │
                        │              └── (subject is stable: sex, origin)
                        └── visit_metadata (EAV, attached to visit)

membership:  project ──< project_samples >── sample   (many-to-many)
```

- **subject → visit → sample** — pure lineage / provenance, with **no
  project affiliation**.
- **project** — a named set of samples; owns no rows directly.
- **project_samples** — the many-to-many junction that is the sole
  source of truth for which samples belong to which project (one
  sample can belong to several — e.g. shared plate controls).

See [Schema](schema.md) for the full breakdown.

## Contact

- Schema, DB admin, access: **Mateusz Franciszek Kołek** —
  <mateusz.kolek@meduniwien.ac.at>
- Co-maintainer: **Gabriel Innocenti** — <gabriel.innocenti@meduniwien.ac.at>
- Bugs / feature requests:
  [GitHub issues](https://github.com/Polymerase3/noxdb/issues)
