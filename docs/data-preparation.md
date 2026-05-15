# Preparing data for import

This page is for **lab members submitting a new project**. You do not need
access to the database server — just prepare your files in the format
described here and send the zipped folder to
**[mateusz.kolek@meduniwien.ac.at](mailto:mateusz.kolek@meduniwien.ac.at)**.

---

## Folder structure

Create one folder per project. The folder name does not matter — the project
name is taken from `project.yaml`. The layout must be exactly:

```
IBD_Vienna/                   ← folder name is free
├── project.yaml
├── subjects.csv
├── visits.csv
├── samples.csv
└── files/
    └── manifest.csv          ← optional; omit the files/ folder if you have no paths yet
```

Zip the whole folder and send it. Nothing else is needed.

---

## project.yaml

A short YAML file with three fields:

```yaml
project_name: IBD_Vienna
description: "UC (n=40), CD (n=30), HC (n=20) — serum samples collected at MUW"
pi_name: "Dr. Hubner"
```

| Field | Required | Notes |
|-------|----------|-------|
| `project_name` | **yes** | Must be unique across the whole database. Use underscores, no spaces. |
| `description` | no | Free text. Quote it if it contains commas. |
| `pi_name` | no | Responsible PI. |

---

## subjects.csv

One row per **study subject** (patient or healthy control). Controls
(mocks, anchors, NCs) go in `samples.csv` — do not add them here.

```
subject_code,sex,origin,meta_diagnosis,meta_IBD_score
IBD_VIE_001,F,Austria,UC,8
IBD_VIE_002,M,Austria,HC,
IBD_VIE_003,F,Germany,CD,5
IBD_VIE_004,M,Austria,UC,12
```

| Column | Required | Allowed values | Notes |
|--------|----------|---------------|-------|
| `subject_code` | **yes** | any string | Must be unique globally. Use a stable code you'll recognise later. |
| `sex` | **yes** | `M`, `F` | Leave empty only if genuinely unknown — do not write `NA`. |
| `origin` | no | free text | Country or region. |
| `meta_*` | no | any | See [metadata columns](#metadata-columns) below. |

---

## visits.csv

One row per **visit** (timepoint). Most projects have one visit per subject
(`baseline`). Multi-timepoint projects add one row per visit.

```
subject_code,timepoint,group_test,age,meta_treatment
IBD_VIE_001,baseline,UC,34,infliximab
IBD_VIE_001,week12,UC,34,infliximab
IBD_VIE_002,baseline,HC,29,
IBD_VIE_003,baseline,CD,52,vedolizumab
IBD_VIE_004,baseline,UC,41,
```

| Column | Required | Notes |
|--------|----------|-------|
| `subject_code` | **yes** | Must match a code in `subjects.csv`. |
| `timepoint` | **yes** | Free text label: `baseline`, `week12`, `follow_up`, etc. |
| `group_test` | **yes** | Clinical group: `UC`, `CD`, `HC`, `patient`, `control`, etc. |
| `age` | **yes** | Integer. Leave empty if unknown (not `NA`). |
| `meta_*` | no | Visit-level metadata such as treatment, score, BMI. |

---

## samples.csv

One row per **sample tube** measured in the assay. This includes your study
samples **and** all plate controls (mockIP, anchor, NC) that appear on the
same plate. Input samples go here too if you have them.

```
sample_name,subject_code,timepoint,sample_type,sqr,sqrp,library,antibody_class,meta_batch
R25P01_01_IBD001_IBD_VIE_A_T_C2,IBD_VIE_001,baseline,sample,25,01,A_T_C2,,1
R25P01_02_IBD002_IBD_VIE_A_T_C2,IBD_VIE_001,week12,sample,25,01,A_T_C2,,1
R25P01_03_IBD003_IBD_VIE_A_T_C2,IBD_VIE_002,baseline,sample,25,01,A_T_C2,,1
R25P01_04_IBD004_IBD_VIE_A_T_C2,IBD_VIE_003,baseline,sample,25,01,A_T_C2,,1
R25P01_81_Mock_1_A_T_C2,R25P01_81_Mock_1_A_T_C2,baseline,mockIP,25,01,A_T_C2,,
R25P01_82_Mock_2_A_T_C2,R25P01_82_Mock_2_A_T_C2,baseline,mockIP,25,01,A_T_C2,,
R25P01_85_Anchor_1_A_T_C2,R25P01_85_Anchor_1_A_T_C2,baseline,anchor,25,01,A_T_C2,,
R25P01_89_NC_1_A_T_C2,R25P01_89_NC_1_A_T_C2,baseline,NC,25,01,A_T_C2,,
```

| Column | Required | Allowed values | Notes |
|--------|----------|---------------|-------|
| `sample_name` | **yes** | any string | Must be **globally unique**. Use the full name from the sequencing output — do not shorten it. |
| `subject_code` | **yes** | — | Must match `subjects.csv` for real samples. For controls, repeat the `sample_name` in this column (controls have no subject). |
| `timepoint` | **yes** | — | Must match `visits.csv` for real samples. Use `baseline` for controls. |
| `sample_type` | **yes** | `sample` `mockIP` `anchor` `NC` `input` | See table below. |
| `sqr` | **yes** | integer string | SQR number from your run sheet (zero-pad to 2 digits, e.g. `07`). |
| `sqrp` | **yes** | integer string | SQRP number. Leave empty for input samples if not applicable. |
| `library` | **yes** | e.g. `A_T_C2` | Library combination string from your run sheet. |
| `antibody_class` | no | free text | Only relevant for antibody-capture assays. |
| `meta_*` | no | any | Sample-level metadata. |

### sample_type values

| Value | Meaning |
|-------|---------|
| `sample` | Real study sample (patient / healthy control) |
| `mockIP` | Mock immunoprecipitation — negative IP control, no antibody |
| `anchor` | Anchor / carry-over control — same reference material across all plates |
| `NC` | Negative control — no template |
| `input` | Input DNA — total chromatin before IP |

### Sample naming convention

Use the format your sequencing facility provides, which typically follows:

```
R{run}P{plate}_{position}_{identifier}_{project}_{library}
```

For example: `R25P01_03_IBD003_IBD_VIE_A_T_C2`

- `R25` — run 25
- `P01` — plate 01
- `03` — well position 03
- `IBD003` — your internal sample code
- `IBD_VIE` — project code
- `A_T_C2` — library

Controls follow the same run/plate prefix but use reserved positions and
names such as `Mock_1`, `Mock_2`, `Anchor_1`, `NC_1`:

```
R25P01_81_Mock_1_A_T_C2
R25P01_82_Mock_2_A_T_C2
R25P01_85_Anchor_1_A_T_C2
R25P01_89_NC_1_A_T_C2
```

---

## files/manifest.csv *(optional)*

Include this file if you know the paths to the output files on the LiSC
storage. Leave it out if you do not have paths yet — files can be registered
later.

```
sample_name,file_path,file_type,storage_tier
R25P01_01_IBD001_IBD_VIE_A_T_C2,/lisc/data/work/ccr/counts/R25P01_01_IBD001_IBD_VIE_A_T_C2.count.gz,counts,work
R25P01_01_IBD001_IBD_VIE_A_T_C2,/lisc/data/work/ccr/zigp/R25P01_01_IBD001_IBD_VIE_A_T_C2.csv,zigp_norm,work
```

| Column | Required | Allowed values | Notes |
|--------|----------|---------------|-------|
| `sample_name` | **yes** | — | Must match a name in `samples.csv`. |
| `file_path` | **yes** | absolute path | Full path as it appears on the LiSC filesystem. |
| `file_type` | **yes** | see below | Type of the file. |
| `storage_tier` | no | `work` `archive` `scratch` `external` | Defaults to `work` if omitted. |
| `checksum_md5` | no | 32-char hex | Leave empty; we compute it on import. |

### Allowed file_type values

| Value | Description |
|-------|-------------|
| `counts` | Raw read count table (`.count.gz`) |
| `zigp_norm` | ZIGP-normalised table (`.csv`) |
| `beer_norm` | BEER-normalised table |
| `edger_norm` | edgeR-normalised table |
| `fastq_r1` | FASTQ read 1 |
| `fastq_r2` | FASTQ read 2 |
| `fastq_single` | Single-end FASTQ |
| `bam` | Aligned BAM |

---

## Metadata columns

Any column whose name starts with `meta_` is treated as a typed metadata
field. The prefix is stripped and the remainder becomes the key name stored
in the database.

```
meta_diagnosis    → key "diagnosis"
meta_IBD_score    → key "IBD_score"
meta_treatment    → key "treatment"
```

Values are automatically coerced: `true`/`false` → boolean, integers →
integer, decimals → float, everything else → text. **Empty cells are
silently skipped** — they do not insert a NULL; they simply produce no
metadata entry for that row.

- Metadata on `visits.csv` is stored per visit (time-varying values: scores, treatment).
- Metadata on `samples.csv` is stored per sample (technical values: batch, plate position).
- You can have any number of `meta_*` columns. Unknown non-`meta_` columns
  are ignored with a warning, so check the import log.

---

## Encoding and formatting rules

- **Format:** UTF-8, comma-separated (`,`), Unix line endings.
- **Header row:** always present, first row.
- **No trailing spaces** in cell values.
- **Empty optional fields:** leave the cell blank — do not write `NA`, `N/A`, `none`, or `-`.
- **Quote strings** that contain commas: `"IBD cohort, Vienna"`.
- The CSV files can be prepared in Excel and exported with *File → Save As →
  CSV UTF-8 (comma-delimited)*. Double-check that Excel has not silently
  converted your SQR codes to dates (e.g. `07` → `7-Jan`).

---

## Complete dummy example

Below is a self-contained example for a two-timepoint IBD project on one
plate (SQR 25, SQRP 01).

**`project.yaml`**

```yaml
project_name: IBD_Vienna
description: "UC (n=40), CD (n=30), HC (n=20) serum samples — MUW cohort"
pi_name: "Dr. Hubner"
```

**`subjects.csv`**

```
subject_code,sex,origin,meta_diagnosis
IBD_VIE_001,F,Austria,UC
IBD_VIE_002,M,Austria,HC
IBD_VIE_003,F,Germany,CD
IBD_VIE_004,M,Austria,UC
```

**`visits.csv`**

```
subject_code,timepoint,group_test,age,meta_treatment,meta_CRP
IBD_VIE_001,baseline,UC,34,infliximab,18.4
IBD_VIE_001,week12,UC,34,infliximab,3.1
IBD_VIE_002,baseline,HC,29,,0.8
IBD_VIE_003,baseline,CD,52,vedolizumab,11.2
IBD_VIE_004,baseline,UC,41,,24.7
```

**`samples.csv`**

```
sample_name,subject_code,timepoint,sample_type,sqr,sqrp,library,antibody_class
R25P01_01_IBD001_IBD_VIE_A_T_C2,IBD_VIE_001,baseline,sample,25,01,A_T_C2,
R25P01_02_IBD002_IBD_VIE_A_T_C2,IBD_VIE_001,week12,sample,25,01,A_T_C2,
R25P01_03_IBD003_IBD_VIE_A_T_C2,IBD_VIE_002,baseline,sample,25,01,A_T_C2,
R25P01_04_IBD004_IBD_VIE_A_T_C2,IBD_VIE_003,baseline,sample,25,01,A_T_C2,
R25P01_05_IBD005_IBD_VIE_A_T_C2,IBD_VIE_004,baseline,sample,25,01,A_T_C2,
R25P01_81_Mock_1_A_T_C2,R25P01_81_Mock_1_A_T_C2,baseline,mockIP,25,01,A_T_C2,
R25P01_82_Mock_2_A_T_C2,R25P01_82_Mock_2_A_T_C2,baseline,mockIP,25,01,A_T_C2,
R25P01_83_Mock_3_A_T_C2,R25P01_83_Mock_3_A_T_C2,baseline,mockIP,25,01,A_T_C2,
R25P01_84_Mock_4_A_T_C2,R25P01_84_Mock_4_A_T_C2,baseline,mockIP,25,01,A_T_C2,
R25P01_85_Anchor_1_A_T_C2,R25P01_85_Anchor_1_A_T_C2,baseline,anchor,25,01,A_T_C2,
R25P01_86_Anchor_2_A_T_C2,R25P01_86_Anchor_2_A_T_C2,baseline,anchor,25,01,A_T_C2,
R25P01_89_NC_1_A_T_C2,R25P01_89_NC_1_A_T_C2,baseline,NC,25,01,A_T_C2,
R25P01_90_NC_2_A_T_C2,R25P01_90_NC_2_A_T_C2,baseline,NC,25,01,A_T_C2,
```

**`files/manifest.csv`** *(optional)*

```
sample_name,file_path,file_type,storage_tier
R25P01_01_IBD001_IBD_VIE_A_T_C2,/lisc/data/work/ccr/counts/R25P01_01_IBD001_IBD_VIE_A_T_C2.count.gz,counts,work
R25P01_01_IBD001_IBD_VIE_A_T_C2,/lisc/data/work/ccr/zigp/R25P01_01_IBD001_IBD_VIE_A_T_C2.csv,zigp_norm,work
R25P01_02_IBD002_IBD_VIE_A_T_C2,/lisc/data/work/ccr/counts/R25P01_02_IBD002_IBD_VIE_A_T_C2.count.gz,counts,work
R25P01_02_IBD002_IBD_VIE_A_T_C2,/lisc/data/work/ccr/zigp/R25P01_02_IBD002_IBD_VIE_A_T_C2.csv,zigp_norm,work
```

---

## Sending your data

1. Prepare the folder as described above.
2. Zip it: `zip -r IBD_Vienna.zip IBD_Vienna/`
3. Send to **[mateusz.kolek@meduniwien.ac.at](mailto:mateusz.kolek@meduniwien.ac.at)** with the subject line `noxDB import — <project_name>`.

Include a short note with:
- The project name and a one-line description.
- Whether the files are already on LiSC storage (i.e. whether `manifest.csv` is included).
- Any unusual aspects (multiple plates, repeated samples, missing ages, etc.).
