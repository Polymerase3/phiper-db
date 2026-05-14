# Install

## Requirements

Before you start, make sure you have all of the following:

- **Python 3.10 or newer** — check with `python --version` or `python3 --version`
- **A MariaDB client library** installed on your system (see Step 1 below) — the Python driver needs it to build
- **Access to the LiSC network** — either you are on-site, connected to the VPN, or you have SSH access to `ccr-lab.lisc.univie.ac.at`
- **Git** — to clone the repository (`git --version` should return something)

---

## Step 1 — Install the MariaDB client

The Python `mariadb` driver is not pure Python — it compiles against your system's MariaDB client library. You need to install that library first, before installing the package.

=== "Linux (Debian / Ubuntu)"

    ```bash
    sudo apt update
    sudo apt install libmariadb-dev
    ```

=== "Linux (Fedora / RHEL / Rocky)"

    ```bash
    sudo dnf install mariadb-devel
    ```

=== "macOS"

    You need [Homebrew](https://brew.sh). If you don't have it, install it first (the Homebrew site has a one-liner).

    ```bash
    brew install mariadb-connector-c
    ```

    After that, you may also need to tell the compiler where the library lives. Run:

    ```bash
    export CFLAGS="-I$(brew --prefix mariadb-connector-c)/include"
    export LDFLAGS="-L$(brew --prefix mariadb-connector-c)/lib"
    ```

    Add those two lines to your `~/.zshrc` (or `~/.bash_profile`) if you want them to persist across terminal sessions.

=== "Windows"

    1. Download the **MariaDB Connector/C** installer from the [official MariaDB downloads page](https://mariadb.com/downloads/connectors/).
    2. Run the installer and follow the prompts. The default installation path is fine.
    3. After installation, restart your terminal (or PowerShell) so the new paths are picked up.

---

## Step 2 — Get the package

**Clone the repository** from GitHub. Open a terminal, navigate to wherever you keep your code, and run:

```bash
git clone https://github.com/Polymerase3/phiper-db.git
```

This creates a `phiper-db/` folder. Go into it:

```bash
cd phiper-db
```

**Install the package and its dependencies.** We install it in "editable" mode (`-e`) so that any local changes you make are picked up immediately without reinstalling:

```bash
pip install -e .
```

If you plan to do data analysis (pulling tables into pandas, SFTP fetches), install the `analysis` extras instead:

```bash
pip install -e ".[analysis]"
```

!!! tip
    If you get a `pip: command not found` error, try `pip3` instead. If you want to keep things tidy, create a virtual environment first: `python3 -m venv .venv && source .venv/bin/activate`, then run the `pip install` command above.

---

## Step 3 — Set up your database credentials

Connection settings are stored in a plain-text config file called `~/.my.cnf` in your home directory. The library reads the `[labdb]` section automatically every time it connects.

**Create or open the file:**

```bash
# On Linux / macOS
nano ~/.my.cnf

# On Windows (PowerShell)
notepad $HOME\.my.cnf
```

**Add the following block** (replace the placeholders with the values you received from an admin):

```ini
[labdb]
host=<galera-internal-hostname>
port=3306
user=<your-db-username>
password=<your-db-password>
database=dbmaria_project
```

What each field means:

| Field      | What to put there                                                  |
|------------|--------------------------------------------------------------------|
| `host`     | The internal hostname of the database server — ask an admin        |
| `port`     | Leave this as `3306` unless told otherwise                         |
| `user`     | Your personal database username — provided by an admin             |
| `password` | Your database password — provided by an admin                      |
| `database` | Leave this as `dbmaria_project`                                    |

**Save the file**, then lock down its permissions so only you can read it:

```bash
# Linux / macOS only
chmod 600 ~/.my.cnf
```

---

## Step 4 — Set up SSH tunnel credentials

The database server lives on the LiSC internal network. If you are working from outside LiSC (e.g. from home), you cannot reach it directly. The library can automatically open an SSH tunnel through the lab's jump host (`ccr-lab.lisc.univie.ac.at`) — but you need to tell it how to log in there.

**Add a second section** to the same `~/.my.cnf` file, directly below `[labdb]`:

```ini
[labdb-ssh]
ssh_host=ccr-lab.lisc.univie.ac.at
ssh_user=<your-lisc-username>
ssh_pkey=~/.ssh/id_ed25519
```

What each field means:

| Field        | What to put there                                                                 |
|--------------|-----------------------------------------------------------------------------------|
| `ssh_host`   | The jump host — always `ccr-lab.lisc.univie.ac.at`                               |
| `ssh_user`   | Your LiSC username (the one you use to SSH into the cluster)                     |
| `ssh_pkey`   | Path to your **private** SSH key — usually `~/.ssh/id_ed25519` or `~/.ssh/id_rsa` |

**Don't have an SSH key set up?** You have two options:

- **Option A (recommended):** Generate a key pair and upload the public key to the jump host. Run `ssh-keygen -t ed25519` and follow the prompts, then ask an admin to add your public key (`~/.ssh/id_ed25519.pub`) to the server.
- **Option B:** Use your password instead. Replace `ssh_pkey` with:

    ```ini
    ssh_password=<your-lisc-password>
    ```

!!! note
    If you are running your script directly on `ccr-lab` (i.e. you are already inside the LiSC network), you can skip this step entirely — the library will connect directly without an SSH tunnel when `ssh_host` is not set.

---

## Step 5 — Test your connection

Copy the script below into a file called `test_connection.py` and run it with `python test_connection.py`.

```python
from dbmaria_utils.connection import init_pool

print("Connecting to the database...")
try:
    pool = init_pool()
    conn = pool.get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 AS ok")
    result = cursor.fetchone()
    cursor.close()
    conn.close()
    print("Connection successful! Database responded:", result)
except Exception as e:
    print("Connection failed:", e)
    print("\nThings to check:")
    print("  1. Is ~/.my.cnf present and does it have a [labdb] section?")
    print("  2. Are your host / user / password correct?")
    print("  3. If working remotely, does ~/.my.cnf have a [labdb-ssh] section?")
    print("  4. Can you SSH into ccr-lab.lisc.univie.ac.at manually?")
```

A successful run looks like this:

```
Connecting to the database...
Connection successful! Database responded: (1,)
```

If it fails, the error message and the checklist printed at the end are your first debugging steps.
