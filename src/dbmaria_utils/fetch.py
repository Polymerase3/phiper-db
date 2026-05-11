"""Export project metadata + files from the database to a local folder.

These helpers are the "consumer" side of phiper-db: given a project_id,
materialize either the metadata table (CSV / Excel) or the file payloads
(downloaded via SFTP when the database is reached through an SSH jump
host, or copied directly from the filesystem when running on LiSC).

Layout produced by :func:`export_project`:

    <output_dir>/
    ├── metadata.csv                # tidy wide-form table
    ├── metadata.xlsx               # same, Excel
    ├── README.txt                  # project summary
    └── files/
        └── <sample_name>/<file_type>.<ext>     # layout='by_sample'
        # or
        └── <file_type>/<sample_name>.<ext>     # layout='by_type'

All functions take an *optional* cursor and open their own
:func:`dbmaria_utils.transaction` block when one isn't provided, so they
work as one-shot calls from a notebook or composed inside a larger
read transaction.
"""

from __future__ import annotations

import os
import shutil
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from dbmaria_utils import projects, queries
from dbmaria_utils.connection import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_SSH_SECTION,
    _resolve_ssh_credentials,
    transaction,
)


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #

def _cur_ctx(cur):
    """Return a context manager yielding a usable cursor."""
    if cur is None:
        return transaction()
    return nullcontext(cur)


def _ssh_credentials(
    *,
    config_path: str | Path | None = DEFAULT_CONFIG_PATH,
    section: str = DEFAULT_SSH_SECTION,
    **overrides: Any,
) -> dict[str, Any]:
    """Resolve SSH credentials the same way :func:`init_pool` does.

    Returns an empty dict (or a dict without ``ssh_host``) when the user
    is running on LiSC itself and doesn't need a jump host.
    """
    return _resolve_ssh_credentials(config_path, section, overrides)


def _file_extension(path: str) -> str:
    """Return ``.fastq.gz`` for compound extensions, else ``.ext`` from splitext."""
    lower = path.lower()
    for compound in (".fastq.gz", ".fq.gz", ".tar.gz", ".bam.bai"):
        if lower.endswith(compound):
            return compound
    return os.path.splitext(path)[1]


def _layout_target(
    layout: str,
    base: Path,
    sample_name: str,
    file_type: str,
    src_path: str,
) -> Path:
    """Compute the local destination path for *src_path* under *base*."""
    ext = _file_extension(src_path)
    if layout == "by_sample":
        return base / sample_name / f"{file_type}{ext}"
    if layout == "by_type":
        return base / file_type / f"{sample_name}{ext}"
    if layout == "flat":
        return base / os.path.basename(src_path)
    raise ValueError(
        f"layout must be one of 'by_sample', 'by_type', 'flat'; got {layout!r}"
    )


# --------------------------------------------------------------------------- #
# Transport: SFTP or local copy
# --------------------------------------------------------------------------- #

class _Transport:
    """Minimal abstraction over local copy vs paramiko SFTP.

    The SFTP path is constructed only when ``ssh_creds`` includes an
    ``ssh_host``; otherwise the transport assumes the source path is
    locally readable (e.g. running on LiSC with the mount available).
    """

    def __init__(self, ssh_creds: dict[str, Any]):
        self._ssh_creds = ssh_creds
        self._client = None
        self._sftp = None

    def __enter__(self) -> "_Transport":
        if self._ssh_creds.get("ssh_host"):
            self._open_sftp()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _open_sftp(self) -> None:
        try:
            import paramiko
        except ImportError as exc:  # pragma: no cover - import guard
            raise ImportError(
                "paramiko is required for SFTP downloads; install with "
                "`pip install 'phiper-db[analysis]'` or `pip install paramiko`"
            ) from exc

        creds = self._ssh_creds
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        connect_kwargs: dict[str, Any] = {
            "hostname": creds["ssh_host"],
            "port": int(creds.get("ssh_port", 22)),
            "username": creds.get("ssh_user"),
        }
        if creds.get("ssh_pkey"):
            connect_kwargs["key_filename"] = str(
                Path(creds["ssh_pkey"]).expanduser()
            )
            if creds.get("ssh_pkey_password"):
                connect_kwargs["passphrase"] = creds["ssh_pkey_password"]
        if creds.get("ssh_password"):
            connect_kwargs["password"] = creds["ssh_password"]
        client.connect(**connect_kwargs)
        self._client = client
        self._sftp = client.open_sftp()

    def fetch(self, src: str, dst: Path) -> int:
        """Copy *src* (remote or local) to *dst* (always local). Returns size."""
        dst.parent.mkdir(parents=True, exist_ok=True)
        if self._sftp is not None:
            self._sftp.get(src, str(dst))
        else:
            shutil.copyfile(src, dst)
        return dst.stat().st_size

    def close(self) -> None:
        if self._sftp is not None:
            try:
                self._sftp.close()
            except Exception:  # pragma: no cover - best-effort cleanup
                pass
            self._sftp = None
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # pragma: no cover - best-effort cleanup
                pass
            self._client = None


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def export_metadata_table(
    cur=None,
    *,
    project_id: int,
    output_dir: Path | str,
    formats: tuple[str, ...] = ("csv",),
) -> dict[str, Path]:
    """Write the project tidy table to *output_dir* in the requested formats.

    *formats* is an iterable of ``'csv'`` and/or ``'xlsx'``. Returns a
    dict mapping each requested format to the path of the file written.
    Raises ``ImportError`` for ``'xlsx'`` if :mod:`openpyxl` is missing.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    with _cur_ctx(cur) as c:
        df = queries.project_tidy_table(c, project_id)

    written: dict[str, Path] = {}
    for fmt in formats:
        if fmt == "csv":
            path = out / "metadata.csv"
            df.to_csv(path, index=False)
            written["csv"] = path
        elif fmt == "xlsx":
            path = out / "metadata.xlsx"
            df.to_excel(path, index=False)
            written["xlsx"] = path
        else:
            raise ValueError(f"unknown format {fmt!r}; expected 'csv' or 'xlsx'")
    return written


def download_files_for_project(
    cur=None,
    *,
    project_id: int,
    output_dir: Path | str,
    file_types: list[str] | None = None,
    layout: str = "by_sample",
    config_path: str | Path | None = DEFAULT_CONFIG_PATH,
    ssh_section: str = DEFAULT_SSH_SECTION,
    **ssh_overrides: Any,
) -> dict[str, Any]:
    """Copy every file registered for *project_id* into *output_dir*.

    *file_types*: if given, only files whose ``file_type`` is in the list
    are downloaded. ``None`` means download every type.

    *layout*: ``'by_sample'`` (default) groups files under per-sample
    subdirectories; ``'by_type'`` groups by file_type; ``'flat'`` writes
    every file at the top level (be aware that ``sample_files.file_path``
    is globally UNIQUE but basenames are not).

    SSH credentials are resolved from *config_path* / *ssh_section* the
    same way :func:`init_pool` does. When ``ssh_host`` is unset the
    function falls back to local file copy via :func:`shutil.copyfile`.
    Returns a report dict: ``{downloaded, skipped, failed, output_dir}``.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    with _cur_ctx(cur) as c:
        df = queries.files_for_project(c, project_id)

    if df.empty:
        return {
            "downloaded": [],
            "skipped": [],
            "failed": [],
            "output_dir": str(out),
        }

    if file_types is not None:
        df = df[df["file_type"].isin(file_types)]

    creds = _ssh_credentials(
        config_path=config_path, section=ssh_section, **ssh_overrides
    )
    downloaded: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    with _Transport(creds) as transport:
        for row in df.to_dict("records"):
            dst = _layout_target(
                layout, out, row["sample_name"], row["file_type"], row["file_path"]
            )
            if dst.exists():
                skipped.append({"file_path": row["file_path"], "dst": str(dst)})
                continue
            try:
                size = transport.fetch(row["file_path"], dst)
            except Exception as exc:
                failed.append(
                    {"file_path": row["file_path"], "dst": str(dst), "error": str(exc)}
                )
                continue
            downloaded.append(
                {"file_path": row["file_path"], "dst": str(dst), "size": size}
            )

    return {
        "downloaded": downloaded,
        "skipped": skipped,
        "failed": failed,
        "output_dir": str(out),
    }


def export_project(
    cur=None,
    *,
    project_id: int,
    output_dir: Path | str,
    file_types: list[str] | None = None,
    layout: str = "by_sample",
    metadata_formats: tuple[str, ...] = ("csv",),
    include_files: bool = True,
    config_path: str | Path | None = DEFAULT_CONFIG_PATH,
    ssh_section: str = DEFAULT_SSH_SECTION,
    **ssh_overrides: Any,
) -> dict[str, Any]:
    """One-shot export: metadata table + files + README.

    Combines :func:`export_metadata_table` and :func:`download_files_for_project`
    and writes a small ``README.txt`` describing the project. Useful for
    handing a self-contained snapshot to a collaborator.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    with _cur_ctx(cur) as c:
        project_row = projects.get(c, project_id)
        summary = queries.project_summary(c, project_id)
        meta_paths = export_metadata_table(
            c,
            project_id=project_id,
            output_dir=out,
            formats=metadata_formats,
        )

    file_report: dict[str, Any] = {
        "downloaded": [], "skipped": [], "failed": [], "output_dir": None,
    }
    if include_files:
        file_report = download_files_for_project(
            cur=cur,
            project_id=project_id,
            output_dir=out / "files",
            file_types=file_types,
            layout=layout,
            config_path=config_path,
            ssh_section=ssh_section,
            **ssh_overrides,
        )

    readme_path = out / "README.txt"
    readme_path.write_text(_render_readme(project_row, summary), encoding="utf-8")

    return {
        "project": project_row,
        "summary": summary,
        "metadata": {fmt: str(p) for fmt, p in meta_paths.items()},
        "files": file_report,
        "readme": str(readme_path),
        "output_dir": str(out),
    }


def _render_readme(project_row: dict[str, Any] | None, summary: dict[str, Any]) -> str:
    """Human-readable project description shipped alongside the export."""
    if project_row is None:
        return f"Project {summary['project_id']} not found in database.\n"
    lines = [
        f"Project: {project_row['project_name']} (id={project_row['project_id']})",
        f"PI: {project_row.get('pi_name') or '-'}",
        f"Description: {project_row.get('description') or '-'}",
        f"Created: {project_row.get('created_at')}",
        "",
        "Counts:",
        f"  subjects: {summary['n_subjects']}",
        f"  visits:   {summary['n_visits']}",
        f"  samples:  {summary['n_samples']}",
        f"  files:    {summary['n_files']}",
    ]
    if summary["files_by_type"]:
        lines.append("")
        lines.append("Files by type:")
        for ft, n in sorted(summary["files_by_type"].items()):
            lines.append(f"  {ft}: {n}")
    return "\n".join(lines) + "\n"
