# Install

## Requirements

- Python 3.10+
- `libmariadb-dev` (or your platform's MariaDB client headers) — the
  `mariadb` Python driver builds against it
- Access to the LiSC network, either directly or via the
  `ccr-lab.lisc.univie.ac.at` jump host

## Install the package

```bash
pip install -e .
```

Optional extras:

| Extra       | Pulls in                              | When you need it                                  |
|-------------|---------------------------------------|---------------------------------------------------|
| `test`      | `pytest`, `pytest-cov`, `pandas`      | Running the test suite                            |
| `analysis`  | `pandas`, `openpyxl`, `paramiko`      | `queries.project_tidy_table`, `fetch.*` (SFTP)    |
| `docs`      | `mkdocs-material`, `mkdocstrings`     | Building this documentation site locally          |

Example:

```bash
pip install -e ".[analysis]"
```

## Credentials: `~/.my.cnf`

Connection settings live in your MariaDB client config file. The
library reads the `[labdb]` section by default:

```ini
[labdb]
host=<galera-internal-hostname>
port=3306
user=<db-user>
password=<db-password>
database=dbmaria_project
```

Any of these can be overridden per call via `init_pool(...)` keyword
arguments. The database name additionally honors the `LABDB_DATABASE`
env variable.

## Connecting through the LiSC SSH jump host

The Galera cluster is on the LiSC internal network and is only
reachable from hosts inside LiSC. From outside, connect through the
project VM (`ccr-lab.lisc.univie.ac.at`) —
[`init_pool()`][dbmaria_utils.connection.init_pool] will open an SSH
tunnel for you when an SSH host is configured.

Add a `[labdb-ssh]` section alongside `[labdb]` in `~/.my.cnf`:

```ini
[labdb-ssh]
ssh_host=ccr-lab.lisc.univie.ac.at
ssh_user=<lisc-username>
ssh_pkey=~/.ssh/id_ed25519        # public-key auth (preferred)
# ssh_password=<lisc-password>    # alternative if you don't have a key uploaded
```

**Auth precedence:** `ssh_pkey` is tried first; otherwise paramiko falls
back to your ssh-agent / default `~/.ssh/id_*` keys, then to
`ssh_password` if set.

**Override precedence:** kwargs to `init_pool()` > `LABDB_SSH_*` env
vars > `[labdb-ssh]` INI section.

When `ssh_host` is unset (e.g. when running on `ccr-lab` itself), the
library connects directly to the configured DB host.

## Access tiers

Three role tiers, all restricted to hosts in `lisc.%`:

- **Admin** (full privileges): Mateusz Kołek, Gabriel Innocenti
- **Read-write** (`SELECT`/`INSERT`/`UPDATE`/`DELETE`): Lovro
  Trgovec-Greif, Melanie Prinzensteiner
- **Read-only** (`SELECT`): everyone else listed in `users/users.sql`

To request an account, email an admin with your desired role.
