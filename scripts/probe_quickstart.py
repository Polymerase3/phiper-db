"""Probe script for collecting real DB output for docs/quickstart.md.

Run with the project venv:
    .venv/bin/python3 scripts/probe_quickstart.py

Prerequisites:
  - SSH gateway reachable (ccr-lab.lisc.univie.ac.at)
  - ~/.my.cnf has [labdb] and [labdb-ssh] sections (local_port=3307 optional)
  - pip install -e ".[analysis]" done

Steps covered: init_pool, projects, project_summary, samples_for_project,
subjects, visits, samples CRUD, queries module (samples_with_metadata,
files_for_project, project_tidy_table).

Run one block at a time — each is independently timed and flushed so a hang
is immediately visible. Pool is always closed in the finally block.
"""
import sys, time
sys.path.insert(0, "src")
sys.stdout.reconfigure(line_buffering=True)

from decimal import Decimal
from datetime import datetime, date
import json

import pandas as pd
pd.set_option("display.max_columns", 20)
pd.set_option("display.width", 120)
pd.set_option("display.max_colwidth", 30)

from dbmaria_utils import init_pool, close_pool, transaction
from dbmaria_utils import projects, subjects, visits, samples, queries

def jsonable(obj):
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    return str(obj)

def step(label):
    print(f"\n{'='*60}", flush=True)
    print(f"  {label}", flush=True)
    print(f"{'='*60}", flush=True)
    return time.time()

def done(t0, extra=""):
    print(f"  [{time.time()-t0:.2f}s] {extra}", flush=True)

import socket
def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0

if not _port_open(3307):
    print("ERROR: nothing listening on 127.0.0.1:3307 — start the tunnel first:", flush=True)
    print("  ssh -f -N -L 3307:10.65.3.50:3306 youruser@ccr-lab.lisc.univie.ac.at", flush=True)
    sys.exit(1)
print("tunnel: 127.0.0.1:3307 is open, reusing it", flush=True)

try:
    # ── 1. init_pool ──────────────────────────────────────────────────────────
    t = step("1. init_pool()")
    init_pool()
    done(t)

    with transaction() as cur:

        # ── 2. Projects table ─────────────────────────────────────────────────
        t = step("2. projects.list_all()")
        proj_list = projects.list_all(cur)
        done(t, f"{len(proj_list)} projects")
        df_proj = pd.DataFrame(proj_list)
        print(df_proj[["project_id", "project_name", "description"]].to_string(index=False), flush=True)
        first_pid = proj_list[0]["project_id"]
        print(f"\n=> Using project_id={first_pid} for subsequent queries", flush=True)

        # ── 3. project_summary ────────────────────────────────────────────────
        t = step(f"3. queries.project_summary(project_id={first_pid})")
        summary = queries.project_summary(cur, first_pid)
        done(t)
        print(json.dumps(summary, default=jsonable, indent=2), flush=True)

        # ── 4. samples_for_project ────────────────────────────────────────────
        t = step(f"4. queries.samples_for_project(project_id={first_pid})")
        df = queries.samples_for_project(cur, first_pid)
        done(t, f"{len(df)} rows")
        print(df.head(5).to_string(index=False), flush=True)
        if "timepoint" in df.columns:
            print(f"\nUnique timepoints: {df['timepoint'].dropna().unique().tolist()}", flush=True)

        t = step("4b. has_files=True / has_files=False")
        df_with = queries.samples_for_project(cur, first_pid, has_files=True)
        df_without = queries.samples_for_project(cur, first_pid, has_files=False)
        done(t, f"with files: {len(df_with)}, without: {len(df_without)}")

        # ── 5. subjects CRUD ──────────────────────────────────────────────────
        t = step(f"5. subjects.list_for_project(project_id={first_pid})")
        subj_list = subjects.list_for_project(cur, first_pid)
        done(t, f"{len(subj_list)} subjects")
        print(json.dumps(subj_list[0] if subj_list else {}, default=jsonable, indent=2), flush=True)

        # ── 6. visits CRUD ────────────────────────────────────────────────────
        if subj_list:
            sid = subj_list[0]["subject_id"]
            t = step(f"6. visits for subject_id={sid}")
            cur.execute("SELECT * FROM visits WHERE subject_id = ? LIMIT 1", (sid,))
            vcols = [d[0] for d in cur.description]
            vrow = cur.fetchone()
            done(t)
            print(json.dumps(dict(zip(vcols, vrow)) if vrow else {}, default=jsonable, indent=2), flush=True)

        # ── 7. samples CRUD ───────────────────────────────────────────────────
        if not df.empty:
            s_id = int(df.iloc[0]["sample_id"])
            t = step(f"7. samples.get(sample_id={s_id})")
            s = samples.get(cur, s_id)
            done(t)
            print(json.dumps(s, default=jsonable, indent=2), flush=True)

        # ── 8. samples_with_metadata ──────────────────────────────────────────
        t = step(f"8. queries.samples_with_metadata(project_id={first_pid})")
        dfm = queries.samples_with_metadata(cur, first_pid)
        done(t, f"{dfm.shape[0]} rows × {dfm.shape[1]} cols")
        print(f"Columns: {list(dfm.columns)}", flush=True)
        print(dfm.head(3).to_string(index=False), flush=True)

        # ── 9. files_for_project ──────────────────────────────────────────────
        t = step(f"9. queries.files_for_project(project_id={first_pid})")
        dff = queries.files_for_project(cur, first_pid)
        done(t, f"{len(dff)} files")
        if not dff.empty:
            print(dff.head(5).to_string(index=False), flush=True)

        # ── 10. project_tidy_table ────────────────────────────────────────────
        t = step(f"10. queries.project_tidy_table(project_id={first_pid})")
        dft = queries.project_tidy_table(cur, first_pid)
        done(t, f"shape={dft.shape}")
        print(f"Columns: {list(dft.columns)}", flush=True)

finally:
    close_pool()
    print("\nPool closed.", flush=True)
