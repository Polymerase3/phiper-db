# CLI: `import_project.py`

Bulk-load a whole project folder into the database in one atomic
transaction.

## Folder layout

```
my_project/
├── project.yaml          # project name, PI, description
├── subjects.csv          # subject_code, sex, origin, meta_*
├── visits.csv            # subject_code, timepoint, meta_*
├── samples.csv           # sample_name, visit ref, sample_type, meta_*
└── files/
    └── manifest.csv      # sample_name, file_path, file_type, md5?
```

CSV columns prefixed with `meta_` are treated as typed metadata keys.
Values are coerced through `int → float → bool → str` until one fits,
and stored in `visit_metadata` / `sample_metadata` accordingly.

## Usage

```bash
python scripts/import_project.py PATH/TO/my_project [options]
```

| Flag                | Meaning                                                                 |
|---------------------|-------------------------------------------------------------------------|
| `--dry-run`         | Validate everything; do not commit.                                     |
| `--force`           | Re-run on a folder that has been imported before. Idempotent.           |
| `--compute-md5`     | Compute MD5 for every file under `files/`.                              |
| `--skip-disk-check` | Skip the `os.path.isfile` validation (use only when files are remote).  |
| `--log-dir DIR`     | Where to write `<ts>_<project>.log`. Defaults to the project folder.    |

## Exit codes

| Code | Meaning                                                          |
|------|------------------------------------------------------------------|
| `0`  | Success.                                                         |
| `2`  | Validation failure — `ProjectImportError` with a list of issues. |
| `3`  | Unexpected error (raised exception).                             |

A JSON report is always written to stdout summarizing what was
inserted, what already existed, and what was unchanged per table.

## Idempotency rules

- With `--force`, every row goes through `get_or_create` / `set_*`. A
  second run on the same unchanged folder reports `inserted: 0` and
  `unchanged` for everything.
- **Cross-project collisions on `sample_name` or `file_path` are
  blocked even with `--force`** — those UNIQUEs are global by design,
  and silently re-attaching a sample to a different project would be
  silent data corruption.

## Library entry point

The same functionality is available as a Python call:

```python
from dbmaria_utils._import import import_project_from_dir, ProjectImportError

try:
    report = import_project_from_dir(
        "PATH/TO/my_project",
        dry_run=False,
        force=False,
        compute_md5=True,
        skip_disk_check=False,
        log_dir=None,
    )
except ProjectImportError as e:
    for issue in e.errors:
        print(issue)
```

See [Project import](reference/import.md) for full signatures.
