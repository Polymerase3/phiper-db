# Quickstart

This page walks through a short end-to-end session: open a connection,
register a subject with a visit and a sample, attach a file, and export
the project back out to disk.

It assumes you have already [installed the package](install.md) and
configured `~/.my.cnf`.

## 1. Open a pool

```python
from dbmaria_utils import init_pool, transaction, close_pool

init_pool()           # reads [labdb] (and [labdb-ssh] if present) from ~/.my.cnf
```

`init_pool()` opens an SSH tunnel automatically when `ssh_host` is set.
Call [`close_pool()`][dbmaria_utils.connection.close_pool] at shutdown
(or in a test teardown) to release sockets and the tunnel.

## 2. Create a project

```python
from dbmaria_utils import projects

with transaction() as cur:
    project_id = projects.get_or_create(
        cur,
        "STUDY42",
        pi_name="Dr. Test",
        description="Pilot longitudinal cohort.",
    )
```

`get_or_create` is idempotent — re-running this block returns the same
`project_id`.

## 3. Register a subject + visit (one transaction)

The [`workflows`][dbmaria_utils.workflows] module bundles common
multi-table operations so they happen atomically:

```python
from dbmaria_utils import workflows

with transaction() as cur:
    subject_id, visit_id = workflows.register_subject_with_visit(
        cur,
        project_id=project_id,
        subject_code="S001",
        sex="F",
        origin="Vienna",
        timepoint="baseline",
        visit_metadata={
            "age_years": 34,
            "group": "control",
        },
    )
```

If anything inside the block raises, the subject, the visit, **and**
the metadata are rolled back together.

## 4. Register a sample with a file on disk

```python
with transaction() as cur:
    sample_id = workflows.register_sample_with_files(
        cur,
        visit_id=visit_id,
        sample_name="STUDY42_S001_baseline_RNA",
        sample_type="RNA",
        files=[
            {
                "file_path": "/lisc/archive/study42/S001/baseline/reads_R1.fastq.gz",
                "file_type": "fastq",
                "compute_md5": True,
            },
        ],
        sample_metadata={"ig_class": "IgG"},
    )
```

The file is stat'd, its path is checked against the storage-tier root
(`LABDB_ARCHIVE_ROOT` / `LABDB_WORK_ROOT`), and — because
`compute_md5=True` — its MD5 is computed and stored. A disk-validation
failure rolls back the sample too.

## 5. Read back: a tidy project table

```python
from dbmaria_utils import queries

with transaction() as cur:
    df = queries.project_tidy_table(cur, project_id=project_id)

print(df.head())
```

`project_tidy_table` joins `projects → subjects → visits → samples` and
pivots the EAV metadata into named columns. Returns a
`pandas.DataFrame` (requires the `analysis` extra).

## 6. Export a snapshot to disk

```python
from dbmaria_utils import fetch

fetch.export_project(
    project_id=project_id,
    out_dir="/tmp/study42_snapshot",
    formats=("csv", "xlsx"),
    file_layout="by_sample",
)
```

This writes `metadata.csv` (and `metadata.xlsx`), copies/downloads
every registered file under `files/by_sample/...`, and drops a
`README.txt` summary in the output folder. When connecting through SSH
the file copy uses SFTP automatically.

## 7. Shut down

```python
close_pool()
```

## Where to go next

- [API reference](reference/index.md) — every public function.
- [Schema](schema.md) — the table layout.
- [CLI](cli.md) — bulk-import a project folder without writing Python.
