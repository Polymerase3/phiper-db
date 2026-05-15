# Quickstart

This guide is for **read-only users** — researchers who want to query the
database, explore projects, and pull data into pandas. It assumes you have
already [installed the package](install.md) and configured `~/.my.cnf`.

---

## 1. Connecting to the database

The CCR database lives inside the LiSC network and is only reachable directly
when you are on-site or on the VPN. From outside, you must tunnel through the
SSH gateway first.

> **⚠ Prefer Option B (SSH tunnel) when working remotely.**
>
> Option A makes a direct TCP connection to the database host on every
> `init_pool()` call. Opening and closing that connection repeatedly from
> outside the LiSC network will trigger **fail2ban** on the SSH gateway and
> get your IP temporarily banned. Always use Option B and keep the tunnel
> open for your entire session — it opens once and stays alive.

### Option A — On-site / VPN (direct)

`init_pool()` connects straight to the DB host configured in `~/.my.cnf`.
Nothing else is needed:

```python
from noxdb import init_pool, close_pool

init_pool()
# ... your queries ...
close_pool()
```

### Option B — Remote (SSH tunnel)

The database is not directly reachable from outside LiSC. You need to open a
local port-forwarding tunnel through the SSH gateway first, then tell
`init_pool()` to connect through it.

#### Step 1 — Add `local_port` to `~/.my.cnf`

In the `[noxdb-ssh]` section, add the local port the tunnel will bind to:

```ini
[noxdb-ssh]
ssh_host = ccr-lab.lisc.univie.ac.at
ssh_user = youruser
local_port = 3307
```

This is how `init_pool()` knows which port to look for.

#### Step 2 — Open the tunnel (once per session)

Run this in your terminal before starting any Python session:

```bash
ssh -f -N -L 3307:<host>:3306 youruser@ccr-lab.lisc.univie.ac.at
```

Replace `<host>` with the value of `host` from the `[noxdb]` section of your
`~/.my.cnf`. The `-f` flag backgrounds the process; `-N` means no remote
command is run — the tunnel just stays open.

#### Step 3 — Verify the tunnel is alive

```bash
ss -tlnp | grep 3307
```

You should see a line like:

```
LISTEN  0  128  127.0.0.1:3307  0.0.0.0:*
```

If the output is empty, the tunnel is not running. Re-run the `ssh` command.

#### Step 4 — Connect from Python

```python
from noxdb import init_pool, close_pool

init_pool()   # detects 127.0.0.1:3307 is listening and connects through it
# ... your queries ...
close_pool()
```

`init_pool()` checks whether `local_port` (3307) is already bound. If it is,
it connects through the existing tunnel without opening a new one. If it is
not, it raises an error — which is intentional: you should always start the
tunnel explicitly so you know it is running.

#### Killing the tunnel when you are done

```bash
pkill -f "L 3307:<host>:3306"
```

---

## 2. Listing all projects

```python
from noxdb import projects, transaction

with transaction() as cur:
    proj_list = projects.list_all(cur)
```

| project_id | project_name       | description                                                              |
|------------|--------------------|--------------------------------------------------------------------------|
| 7          | ADMCI_NED          |                                                                          |
| 10         | BAT_BATIOS_Kiefer  | BAT (n=62) + BATIOS (n=74), 136 serum samples                            |
| 13         | BC-Engl            | bladder cancer: Cis (n=141), Carbo (n=47), RCE (n=126), ICI (n=79), … |
| 19         | CRC_radiotherapy   | diff. timepoints, 320 samples                                            |
| 25         | HCC_MUW            | 150 + 30 (TKI therapy) + 78 HCs + 48 TKI-treated                        |
| …          | …                  | …                                                                        |
| 58         | input              | Control samples (input DNA)                                              |
| 61         | mockIP             | Mock IP control samples                                                  |
| 64         | anchor             | Anchor control samples                                                   |
| 67         | NC                 | Negative control (NC) samples                                            |

21 projects total (18 study projects + 3 control projects + input).

---

## 3. Project summary

```python
from noxdb import queries

with transaction() as cur:
    summary = queries.project_summary(cur, project_id=7)
```

```json
{
  "project_id": 7,
  "n_subjects": 110,
  "n_visits": 110,
  "n_samples": 110,
  "n_files": 220,
  "files_by_type": {
    "counts": 110,
    "zigp_norm": 110
  },
  "n_controls": 64,
  "controls_by_type": {
    "mockIP": 32,
    "anchor": 16,
    "NC": 16
  }
}
```

`n_samples` counts only real patient/study samples. `n_controls` and
`controls_by_type` show the plate controls (mockIP / anchor / NC) matched to
this project via SQR + SQRP.

---

## 4. Samples for a project

The main query for pulling all samples belonging to a project. Returns real
samples joined with their plate controls (mockIP, anchor, NC) in a single
flat `DataFrame`. Control rows carry their own `project_id` (e.g. 61 for
mockIP) so they are always distinguishable.

```python
with transaction() as cur:
    df = queries.samples_for_project(cur, project_id=7)
```

| project_id | subject_id | subject_code                       | visit_id | timepoint | sample_id | sample_name                        | sample_type | SQR | SQRP | library | antibody_class |
|------------|------------|------------------------------------|----------|-----------|-----------|------------------------------------|-------------|-----|------|---------|----------------|
| 7          | 649        | R14P02_77_FAU0001_ADMCI_NED_A_T_C2 | 649      | baseline  | 649       | R14P02_77_FAU0001_ADMCI_NED_A_T_C2 | sample      | 07  | 02   | A_T_C2  | None           |
| …          | …          | …                                  | …        | …         | …         | …                                  | …           | …   | …    | …       | …              |
| 61         | 14926      | R14P02_81_Mock_1_A_T_C2            | 15838    | baseline  | 15838     | R14P02_81_Mock_1_A_T_C2            | mockIP      | 07  | 02   | A_T_C2  | None           |
| …          | …          | …                                  | …        | …         | …         | …                                  | …           | …   | …    | …       | …              |

174 rows total (110 `sample` + 32 `mockIP` + 16 `anchor` + 16 `NC`).

### Real samples only

```python
with transaction() as cur:
    df = queries.samples_for_project(cur, project_id=7, include_controls=False)
```

110 rows.

### Filtering by file presence

```python
with transaction() as cur:
    df_with    = queries.samples_for_project(cur, project_id=7, has_files=True)
    df_without = queries.samples_for_project(cur, project_id=7, has_files=False)
```

174 with files, 0 without (file filter applies to both real samples and controls).

---

## 5. Subjects

```python
from noxdb import subjects

with transaction() as cur:
    subj_list = subjects.list_for_project(cur, project_id=7)
```

```json
{
  "subject_id": 649,
  "project_id": 7,
  "subject_code": "R14P02_77_FAU0001_ADMCI_NED_A_T_C2",
  "sex": "F",
  "origin": "Netherlands",
  "created_at": "2026-05-14T12:27:07"
}
```

110 subjects in this project.

---

## 6. Visits

```python
with transaction() as cur:
    cur.execute("SELECT * FROM visits WHERE subject_id = ? LIMIT 1", (649,))
```

```json
{
  "visit_id": 649,
  "subject_id": 649,
  "timepoint": "baseline",
  "group_test": "Controls",
  "age": 83,
  "created_at": "2026-05-14T12:27:13"
}
```

---

## 7. Sample detail

```python
from noxdb import samples

with transaction() as cur:
    s = samples.get(cur, sample_id=649)
```

```json
{
  "sample_id": 649,
  "visit_id": 649,
  "sample_name": "R14P02_77_FAU0001_ADMCI_NED_A_T_C2",
  "sample_type": "sample",
  "SQR": "07",
  "SQRP": "02",
  "library": "A_T_C2",
  "antibody_class": null,
  "created_at": "2026-05-14T12:27:18"
}
```

---

## 8. Samples with metadata

Equivalent to `samples_for_project` but includes all EAV metadata columns
joined in. Includes controls by default.

```python
with transaction() as cur:
    dfm = queries.samples_with_metadata(cur, project_id=7)
```

174 rows × 12 columns.

---

## 9. Files for a project

Returns files for the real study samples only (not controls).

```python
with transaction() as cur:
    dff = queries.files_for_project(cur, project_id=7)
```

| file_id | sample_id | sample_name                        | subject_code                       | timepoint | file_type | file_path                                                                    | file_size_bytes | checksum_md5 | storage_tier | created_at          |
|---------|-----------|------------------------------------|------------------------------------|-----------|-----------|------------------------------------------------------------------------------|-----------------|--------------|--------------|---------------------|
| 226     | 649       | R14P02_77_FAU0001_ADMCI_NED_A_T_C2 | R14P02_77_FAU0001_ADMCI_NED_A_T_C2 | baseline  | counts    | /lisc/data/work/ccr/counts/R14P02_77_FAU0001_ADMCI_NED_A_T_C2.count.gz      | None            | None         | work         | 2026-05-14 12:27:23 |
| 229     | 649       | R14P02_77_FAU0001_ADMCI_NED_A_T_C2 | R14P02_77_FAU0001_ADMCI_NED_A_T_C2 | baseline  | zigp_norm | /lisc/data/work/ccr/zigp/R14P02_77_FAU0001_ADMCI_NED_A_T_C2.csv             | None            | None         | work         | 2026-05-14 12:27:24 |
| …       | …         | …                                  | …                                  | …         | …         | …                                                                            | …               | …            | …            | …                   |

220 files total (110 `counts` + 110 `zigp_norm`).

---

## 10. Project tidy table

A single wide DataFrame joining all levels (project → subject → visit →
sample) with metadata pivoted into columns. Includes controls. The standard
starting point for downstream analysis:

```python
with transaction() as cur:
    dft = queries.project_tidy_table(cur, project_id=7)
```

Shape: 174 rows × 12 columns.

---

## 11. Plate controls for a project

Controls (mockIP, anchor, NC) are stored in their own dedicated projects and
are matched back to a study project via SQR + SQRP. Because a plate can span
multiple projects, the same control may appear for several projects — there is
no duplication, just a shared reference.

```python
with transaction() as cur:
    dfc = queries.controls_for_project(cur, project_id=7)
```

```
sample_type
NC        16
anchor    16
mockIP    32
```

| sample_id | sample_name              | sample_type | SQR | SQRP | library | project_id |
|-----------|--------------------------|-------------|-----|------|---------|------------|
| 15838     | R14P02_81_Mock_1_A_T_C2  | mockIP      | 07  | 02   | A_T_C2  | 61         |
| 15841     | R14P02_82_Mock_2_A_T_C2  | mockIP      | 07  | 02   | A_T_C2  | 61         |
| …         | …                        | …           | …   | …    | …       | …          |

64 controls total (32 `mockIP` + 16 `anchor` + 16 `NC`).

### Filtering by type

```python
with transaction() as cur:
    mocks   = queries.controls_for_project(cur, project_id=7, sample_types=["mockIP"])
    anchors = queries.controls_for_project(cur, project_id=7, sample_types=["anchor"])
    qc      = queries.controls_for_project(cur, project_id=7, sample_types=["anchor", "NC"])
```

---

## 12. Input samples

Input DNA samples are not associated with any study project. Use
`list_inputs()` to retrieve all of them globally:

```python
with transaction() as cur:
    dfi = queries.list_inputs(cur)
```

| sample_id | sample_name           | sample_type | SQR | SQRP | library | project_id |
|-----------|-----------------------|-------------|-----|------|---------|------------|
| 15616     | R01P02_1_input_A_v0_s | input       | 01  |      | A_v0_s  | 58         |
| 15619     | R01P02_2_input_A_v0_s | input       | 01  |      | A_v0_s  | 58         |
| …         | …                     | …           | …   | …    | …       | …          |

56 input rows total.

---

## 13. Shut down

Always close the pool when you are done:

```python
close_pool()
```

In scripts, use a `try/finally` to guarantee cleanup even if a query fails:

```python
try:
    init_pool()
    with transaction() as cur:
        df = queries.samples_for_project(cur, project_id=7)
finally:
    close_pool()
```

---

## 14. Full fetch example

The `fetch` module is the consumer side of noxdb: given a project, it
materialises the project structure, a tidy metadata table, and — when running
on LiSC or through an SFTP jump host — the actual files.

### Project structure and file manifest

```python
from noxdb import init_pool, close_pool, transaction
from noxdb import projects, queries

try:
    init_pool()
    with transaction() as cur:
        project_row = projects.get(cur, project_id=7)
        summary     = queries.project_summary(cur, project_id=7)
        dff         = queries.files_for_project(cur, project_id=7)
finally:
    close_pool()
```

`project_row`:

```json
{
  "project_id": 7,
  "project_name": "ADMCI_NED",
  "description": null,
  "pi_name": "Arno Bourgonje",
  "created_at": "2026-05-14T12:27:07"
}
```

`summary`:

```json
{
  "project_id": 7,
  "n_subjects": 110,
  "n_visits": 110,
  "n_samples": 110,
  "n_files": 220,
  "files_by_type": {
    "counts": 110,
    "zigp_norm": 110
  },
  "n_controls": 64,
  "controls_by_type": {
    "mockIP": 32,
    "anchor": 16,
    "NC": 16
  }
}
```

`dff` — first 6 rows of 220:

```
 file_id                        sample_name file_type                                                              file_path storage_tier
     226 R14P02_77_FAU0001_ADMCI_NED_A_T_C2    counts /lisc/data/work/ccr/counts/R14P02_77_FAU0001_ADMCI_NED_A_T_C2.count.gz         work
     229 R14P02_77_FAU0001_ADMCI_NED_A_T_C2 zigp_norm        /lisc/data/work/ccr/zigp/R14P02_77_FAU0001_ADMCI_NED_A_T_C2.csv         work
     232 R14P02_74_FAU0002_ADMCI_NED_A_T_C2    counts /lisc/data/work/ccr/counts/R14P02_74_FAU0002_ADMCI_NED_A_T_C2.count.gz         work
     235 R14P02_74_FAU0002_ADMCI_NED_A_T_C2 zigp_norm        /lisc/data/work/ccr/zigp/R14P02_74_FAU0002_ADMCI_NED_A_T_C2.csv         work
     238  R19P04_14_MG0213_ADMCI_NED_A_T_C2    counts  /lisc/data/work/ccr/counts/R19P04_14_MG0213_ADMCI_NED_A_T_C2.count.gz         work
     241  R19P04_14_MG0213_ADMCI_NED_A_T_C2 zigp_norm         /lisc/data/work/ccr/zigp/R19P04_14_MG0213_ADMCI_NED_A_T_C2.csv         work
```

### Exporting to a local folder

`fetch.export_project` combines metadata, a README, and (optionally) the
actual files into a single output directory. Pass `include_files=False` to
get just the metadata and README without downloading:

```python
from noxdb import init_pool, close_pool, transaction
from noxdb import fetch

try:
    init_pool()
    with transaction() as cur:
        result = fetch.export_project(
            cur,
            project_id=7,
            output_dir="exports/ADMCI_NED",
            include_files=False,          # set True on LiSC to also pull files
            metadata_formats=("csv",),
        )
finally:
    close_pool()
```

Output directory layout:

```
exports/ADMCI_NED/
├── README.txt     (209 bytes)
└── metadata.csv   (18,892 bytes)
```

`README.txt`:

```
Project: ADMCI_NED (id=7)
PI: Arno Bourgonje
Description: -
Created: 2026-05-14 12:27:07

Counts:
  subjects: 110
  visits:   110
  samples:  110
  files:    220

Files by type:
  counts: 110
  zigp_norm: 110
```

`result` (the return value):

```json
{
  "project":  { "project_id": 7, "project_name": "ADMCI_NED", ... },
  "summary":  { "n_subjects": 110, "n_files": 220, ... },
  "metadata": { "csv": "exports/ADMCI_NED/metadata.csv" },
  "readme":   "exports/ADMCI_NED/README.txt",
  "output_dir": "exports/ADMCI_NED"
}
```

To also download the actual files (requires running on LiSC or an active SFTP
connection through the SSH gateway):

```python
result = fetch.export_project(
    cur,
    project_id=7,
    output_dir="exports/ADMCI_NED",
    include_files=True,
    file_types=["counts"],        # omit to get all types
    layout="by_sample",           # or 'by_type' / 'flat'
)
```

The `result["files"]` key then contains `downloaded`, `skipped`, and `failed`
lists so you can see exactly what was fetched and what failed.

---

## Where to go next

- [API reference](reference/index.md) — every public function.
- [Schema](schema.md) — the table layout.
- [Install](install.md) — prerequisites and `~/.my.cnf` setup.
