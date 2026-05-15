"""Temporary probe — fetch module example for docs/quickstart.md section 14.

Run with the project venv:
    .venv/bin/python3 scripts/probe_autofetch.py

Prerequisites:
  - SSH tunnel open on 127.0.0.1:3307
  - ~/.my.cnf has [noxdb] and [noxdb-ssh] sections
  - pip install -e ".[analysis]" done
"""
import sys, socket, json, tempfile, os
from decimal import Decimal
from datetime import datetime, date
sys.path.insert(0, "src")
sys.stdout.reconfigure(line_buffering=True)

import pandas as pd
pd.set_option("display.max_columns", 20)
pd.set_option("display.width", 120)
pd.set_option("display.max_colwidth", 40)

from noxdb import init_pool, close_pool, transaction
from noxdb import projects, queries
from noxdb import fetch

def jsonable(obj):
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    return str(obj)

def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0

if not _port_open(3307):
    print("ERROR: nothing listening on 127.0.0.1:3307 — start the tunnel first:")
    print("  ssh -f -N -L 3307:<db-host>:3306 youruser@ccr-lab.lisc.univie.ac.at")
    sys.exit(1)
print("tunnel OK — 127.0.0.1:3307 is open")

try:
    init_pool()
    with transaction() as cur:
        # ── project structure ────────────────────────────────────────────────
        proj_list = projects.list_all(cur)
        pid = proj_list[0]["project_id"]
        project_row = projects.get(cur, pid)
        print("\n── project row ─────────────────────────────────────────────")
        print(json.dumps(project_row, default=jsonable, indent=2))

        summary = queries.project_summary(cur, pid)
        print("\n── project_summary ─────────────────────────────────────────")
        print(json.dumps(summary, default=jsonable, indent=2))

        # ── files manifest ───────────────────────────────────────────────────
        dff = queries.files_for_project(cur, pid)
        print(f"\n── files_for_project  →  {len(dff)} files ───────────────────")
        print(dff[["file_id", "sample_name", "file_type", "file_path",
                    "storage_tier"]].head(6).to_string(index=False))

        # ── export_project (metadata + README only, no file download) ────────
        with tempfile.TemporaryDirectory() as tmpdir:
            result = fetch.export_project(
                cur,
                project_id=pid,
                output_dir=tmpdir,
                include_files=False,
                metadata_formats=("csv",),
            )
            print("\n── export_project output_dir layout ────────────────────────")
            for root, dirs, files in os.walk(tmpdir):
                rel = os.path.relpath(root, tmpdir)
                prefix = "" if rel == "." else rel + "/"
                for f in sorted(files):
                    fpath = os.path.join(root, f)
                    size = os.path.getsize(fpath)
                    print(f"  {prefix}{f}  ({size:,} bytes)")
            print("\n── README.txt ───────────────────────────────────────────────")
            print(open(result["readme"]).read())
            print("── export_project return value (files key omitted) ─────────")
            display = {k: v for k, v in result.items() if k != "files"}
            print(json.dumps(display, default=jsonable, indent=2))
finally:
    close_pool()
    print("\nPool closed.")
