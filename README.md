# phiper-db

[![Project Status: WIP – Initial development is in progress.](https://www.repostatus.org/badges/latest/wip.svg)](https://www.repostatus.org/#wip)
[![CI](https://github.com/Polymerase3/phiper-db/actions/workflows/ci.yml/badge.svg)](https://github.com/Polymerase3/phiper-db/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/Polymerase3/phiper-db/branch/main/graph/badge.svg)](https://codecov.io/gh/Polymerase3/phiper-db)
[![version](https://img.shields.io/badge/version-0.4.1-blue)](./NEWS.md)
[![docs](https://img.shields.io/badge/docs-mkdocs--material-blue)](https://polymerase3.github.io/phiper-db/)

**📖 Documentation:** <https://polymerase3.github.io/phiper-db/>

---

## What is this?

`phiper-db` is the lab's MariaDB metadata database and the Python
package that talks to it.

The database stores **metadata and file pointers** — who the subject
was, when the visit happened, which sample came out of it, and where
the resulting fastq / bam / counts files live on disk. The bulk data
itself stays on `/lisc/archive` and `/lisc/work`; the database just
knows how to find it.

Everything is organised hierarchically:

```
project → subject → visit → sample → sample_files
```

Flexible typed key/value metadata can be attached to visits and
samples (e.g. `bmi=22.7`, `smoker=False`, `group="control"`).

The Python package gives you:

- A connection pool with optional SSH tunneling through the LiSC
  jump host.
- Per-table CRUD helpers (`projects`, `subjects`, `visits`,
  `samples`, `files`, `metadata`).
- Composite read-only queries that join the hierarchy and pivot the
  metadata into tidy tables.
- High-level workflows that register a subject + visit + sample +
  files in one atomic transaction.
- An importer + CLI that loads a whole project folder
  (`project.yaml`, `subjects.csv`, `visits.csv`, `samples.csv`,
  `files/manifest.csv`) into the database in one go.
- An exporter that downloads a project snapshot (metadata table +
  files) back to a local folder.

---

## Install

Step-by-step. Copy-paste each block in order.

### 1. System dependencies

The Python `mariadb` driver builds against MariaDB client headers.

```bash
# Debian / Ubuntu
sudo apt-get update
sudo apt-get install -y git python3-venv libmariadb-dev
```

On macOS use `brew install mariadb-connector-c`; on Windows install
the [MariaDB Connector/C](https://mariadb.com/downloads/connectors/connectors-data-access/c-connector/).

### 2. Clone the repository

```bash
git clone https://github.com/Polymerase3/phiper-db.git
cd phiper-db
```

### 3. Create a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
```

### 4. Install the package

```bash
pip install --upgrade pip
pip install -e ".[analysis]"
```

The `analysis` extra pulls in `pandas`, `openpyxl`, and `paramiko`,
which you'll need for the query / fetch helpers. Skip it
(`pip install -e .`) if you only need the low-level CRUD layer.

### 5. Smoke test

```bash
python -c "import dbmaria_utils; print(dbmaria_utils.__name__, 'OK')"
```

If that prints `dbmaria_utils OK` you're done.

---

## Configure your credentials

The library reads connection settings from `~/.my.cnf`. Create the
file (it's the same INI used by the `mariadb` CLI) with two sections:

```ini
[labdb]
host=<galera-internal-hostname>
port=3306
user=<your-db-user>
password=<your-db-password>
database=dbmaria_project

[labdb-ssh]
ssh_host=ccr-lab.lisc.univie.ac.at
ssh_user=<your-lisc-username>
ssh_pkey=~/.ssh/id_ed25519        # public-key auth (preferred)
# ssh_password=<lisc-password>    # alternative if you don't use keys
```

- The `[labdb]` section is your database login. Ask an admin for
  values to fill in.
- The `[labdb-ssh]` section is only needed when you're connecting
  from **outside** LiSC — the library opens an SSH tunnel through
  `ccr-lab` automatically. On a machine that already sits inside
  LiSC, leave this section out and the library connects directly.

Protect the file: `chmod 600 ~/.my.cnf`.

For the full credential resolution order (kwargs > env vars > INI),
see the [Install page in the docs](https://polymerase3.github.io/phiper-db/install/).

---

## Where to go next

- **[Quickstart](https://polymerase3.github.io/phiper-db/quickstart/)** —
  end-to-end Python example.
- **[Schema](https://polymerase3.github.io/phiper-db/schema/)** —
  the table layout and naming conventions.
- **[API reference](https://polymerase3.github.io/phiper-db/reference/)** —
  every public function.
- **[CLI](https://polymerase3.github.io/phiper-db/cli/)** —
  bulk-import a project folder with `scripts/import_project.py`.

---

## Contact

- Schema, DB admin, access:
  **Mateusz Franciszek Kołek** — <mateusz.kolek@meduniwien.ac.at>
- Co-maintainer:
  **Gabriel Innocenti** — <gabriel.innocenti@meduniwien.ac.at>
- Bugs / feature requests:
  [GitHub issues](https://github.com/Polymerase3/phiper-db/issues)
