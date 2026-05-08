"""Unit tests for SSH tunnel credential resolution.

These tests exercise `_resolve_ssh_credentials` and `_load_ssh_credentials`
without opening a real tunnel. The opt-in integration test at the bottom is
skipped unless `LABDB_SSH_HOST` is set in the environment.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from dbmaria_utils.connection import (
    _load_ssh_credentials,
    _resolve_ssh_credentials,
)


SSH_ENV_VARS = (
    "LABDB_SSH_HOST",
    "LABDB_SSH_PORT",
    "LABDB_SSH_USER",
    "LABDB_SSH_PASSWORD",
    "LABDB_SSH_PKEY",
    "LABDB_SSH_PKEY_PASSWORD",
)


@pytest.fixture(autouse=True)
def _clear_ssh_env(monkeypatch):
    for var in SSH_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    yield


def _write_cnf(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# _load_ssh_credentials
# --------------------------------------------------------------------------- #

def test_load_ssh_credentials_full_section(tmp_path):
    cnf = _write_cnf(
        tmp_path / "my.cnf",
        "[labdb-ssh]\n"
        "ssh_host=ccr-lab.lisc.univie.ac.at\n"
        "ssh_port=2222\n"
        "ssh_user=alice\n"
        "ssh_pkey=~/.ssh/id_ed25519\n",
    )
    creds = _load_ssh_credentials(cnf, "labdb-ssh")
    assert creds == {
        "ssh_host": "ccr-lab.lisc.univie.ac.at",
        "ssh_port": 2222,
        "ssh_user": "alice",
        "ssh_pkey": "~/.ssh/id_ed25519",
    }


def test_load_ssh_credentials_missing_section_returns_empty(tmp_path):
    cnf = _write_cnf(tmp_path / "my.cnf", "[labdb]\nuser=alice\npassword=s\n")
    assert _load_ssh_credentials(cnf, "labdb-ssh") == {}


def test_load_ssh_credentials_missing_file_returns_empty(tmp_path):
    assert _load_ssh_credentials(tmp_path / "does-not-exist.cnf", "labdb-ssh") == {}


# --------------------------------------------------------------------------- #
# _resolve_ssh_credentials precedence
# --------------------------------------------------------------------------- #

def test_resolve_ssh_no_config_no_env_returns_empty():
    assert _resolve_ssh_credentials(None, "labdb-ssh", {}) == {}


def test_resolve_ssh_ini_only(tmp_path):
    cnf = _write_cnf(
        tmp_path / "my.cnf",
        "[labdb-ssh]\nssh_host=h.example\nssh_user=alice\n",
    )
    creds = _resolve_ssh_credentials(cnf, "labdb-ssh", {})
    assert creds == {"ssh_host": "h.example", "ssh_user": "alice"}


def test_resolve_ssh_env_overrides_ini(tmp_path, monkeypatch):
    cnf = _write_cnf(
        tmp_path / "my.cnf",
        "[labdb-ssh]\nssh_host=ini.example\nssh_user=alice\nssh_port=22\n",
    )
    monkeypatch.setenv("LABDB_SSH_HOST", "env.example")
    monkeypatch.setenv("LABDB_SSH_PORT", "2222")
    creds = _resolve_ssh_credentials(cnf, "labdb-ssh", {})
    assert creds["ssh_host"] == "env.example"
    assert creds["ssh_port"] == 2222
    assert creds["ssh_user"] == "alice"  # ini value preserved


def test_resolve_ssh_kwargs_override_env_and_ini(tmp_path, monkeypatch):
    cnf = _write_cnf(
        tmp_path / "my.cnf",
        "[labdb-ssh]\nssh_host=ini.example\nssh_user=ini_user\n",
    )
    monkeypatch.setenv("LABDB_SSH_HOST", "env.example")
    monkeypatch.setenv("LABDB_SSH_USER", "env_user")
    creds = _resolve_ssh_credentials(
        cnf,
        "labdb-ssh",
        {"ssh_host": "kwarg.example", "ssh_user": "kwarg_user"},
    )
    assert creds["ssh_host"] == "kwarg.example"
    assert creds["ssh_user"] == "kwarg_user"


def test_resolve_ssh_kwargs_none_does_not_override(tmp_path):
    cnf = _write_cnf(
        tmp_path / "my.cnf",
        "[labdb-ssh]\nssh_host=ini.example\nssh_user=alice\n",
    )
    creds = _resolve_ssh_credentials(
        cnf,
        "labdb-ssh",
        {"ssh_host": None, "ssh_user": None, "ssh_password": None},
    )
    assert creds["ssh_host"] == "ini.example"
    assert creds["ssh_user"] == "alice"


def test_resolve_ssh_port_coerced_to_int_from_env(monkeypatch):
    monkeypatch.setenv("LABDB_SSH_HOST", "h")
    monkeypatch.setenv("LABDB_SSH_PORT", "2200")
    creds = _resolve_ssh_credentials(None, "labdb-ssh", {})
    assert creds["ssh_port"] == 2200
    assert isinstance(creds["ssh_port"], int)


def test_resolve_ssh_config_path_none_still_reads_env(monkeypatch):
    monkeypatch.setenv("LABDB_SSH_HOST", "h.example")
    monkeypatch.setenv("LABDB_SSH_USER", "alice")
    creds = _resolve_ssh_credentials(None, "labdb-ssh", {})
    assert creds == {"ssh_host": "h.example", "ssh_user": "alice"}


# --------------------------------------------------------------------------- #
# Opt-in integration test: opens a real tunnel through ccr-lab. Skipped unless
# LABDB_SSH_HOST is set. Run from outside LiSC with DB_HOST pointing at the
# Galera cluster's internal hostname.
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(
    not os.environ.get("LABDB_SSH_HOST"),
    reason="LABDB_SSH_HOST not set; skipping live SSH tunnel test",
)
def test_real_tunnel_select_one():
    from dbmaria_utils import close_pool, execute, init_pool

    init_pool(
        config_path=None,
        host=os.environ.get("DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database=os.environ.get("DB_NAME", "dbmaria_project"),
    )
    try:
        rows = execute("SELECT 1 AS ok")
        assert rows == [{"ok": 1}]
    finally:
        close_pool()
