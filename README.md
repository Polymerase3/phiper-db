# phiper-db

[![Project Status: WIP – Initial development is in progress.](https://www.repostatus.org/badges/latest/wip.svg)](https://www.repostatus.org/#wip)
[![CI](https://github.com/Polymerase3/phiper-db/actions/workflows/ci.yml/badge.svg)](https://github.com/Polymerase3/phiper-db/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/Polymerase3/phiper-db/branch/main/graph/badge.svg)](https://codecov.io/gh/Polymerase3/phiper-db)
[![version](https://img.shields.io/badge/version-0.2.0-blue)](./NEWS.md)

Schema, migrations, and Python tooling for `dbmaria_project`, the lab's MariaDB
metadata database (MariaDB ≥ 10, InnoDB, Galera). The DB stores metadata and
file pointers; bulk data lives on disk.

## Hierarchy: project → subject → visit → sample

- **project** — independent study or dataset.
- **subject** — one person/donor within a project. Stable attributes only
  (sex, origin). `subject_code` is unique per project.
- **visit** — one timepoint / collection event for a subject. Time-varying
  clinical metadata (age, group, timepoint) goes here.
- **sample** — one physical sample / library / Ig-class measurement attached
  to a visit. `sample_name` is globally unique.

Flexible typed key/value metadata can be attached to visits (`visit_metadata`)
and samples (`sample_metadata`). File paths are tracked in `sample_files`.

## Naming conventions

- Tables: plural snake_case (`projects`, `samples`, `sample_files`).
- Primary keys: `<table_singular>_id` (e.g. `subject_id`).
- Foreign keys reuse the parent PK name.
- Migrations: `schema/NNN_description.sql`, numbered and append-only.
- Stored file paths must be absolute (enforced by `CHECK`).

## Repo layout

- `schema/` — numbered SQL migrations
- `users/` — role and privilege definitions (`users_with_passwords.sql` is gitignored)
- `src/dbmaria_utils/` — Python wrapper package
- `scripts/` — maintenance (sweep, backup)
- `seed/` — fake data for development and CI
- `tests/`, `docs/`, `notebooks/`

## Quick start

```bash
pip install -e .
```

Credentials go in `~/.my.cnf`. To run tests, see [`docs/testing.md`](docs/testing.md).

## Connecting through the LiSC SSH jump host

The Galera cluster is on the LiSC internal network and is only reachable from
hosts inside LiSC. From outside, connect through the project VM
(`ccr-lab.lisc.univie.ac.at`) — `init_pool()` will open an SSH tunnel for you
when an SSH host is configured.

Add a `[labdb-ssh]` section alongside `[labdb]` in `~/.my.cnf`:

```ini
[labdb]
host=<galera-internal-hostname>   # the Galera endpoint as resolvable from ccr-lab
port=3306
user=<db-user>
password=<db-password>
database=dbmaria_project

[labdb-ssh]
ssh_host=ccr-lab.lisc.univie.ac.at
ssh_user=<lisc-username>
ssh_pkey=~/.ssh/id_ed25519        # public-key auth (preferred)
# ssh_password=<lisc-password>    # alternative if you don't have a key uploaded
```

Auth: `ssh_pkey` is tried first; otherwise paramiko falls back to your
ssh-agent / default `~/.ssh/id_*` keys, then to `ssh_password` if set. Any
field can also be supplied via `LABDB_SSH_*` env vars or as a kwarg to
`init_pool()`. When `ssh_host` is unset (e.g. when running on `ccr-lab`
itself), the library connects directly to the configured DB host.

## Access

Three role tiers, all restricted to hosts in `lisc.%`:

- **Admin** (full privileges): Mateusz Kołek, Gabriel Innocenti
- **Read-write** (`SELECT/INSERT/UPDATE/DELETE`): Lovro Trgovec-Greif, Melanie Prinzensteiner
- **Read-only** (`SELECT`): everyone else listed in `users/users.sql`

To request an account, email an admin (below) with your desired role. The admin
adds you to `users/users.sql`, sets a password in the gitignored
`users_with_passwords.sql`, and applies it.

## Contact

- Schema, DB admin, access: **Mateusz Franciszek Kołek** — <mateusz.kolek@meduniwien.ac.at>
- Co-maintainer: Gabriel Innocenti
- Bugs / feature requests: GitHub issues
