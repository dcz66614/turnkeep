"""T028 platform path tests. Rules are unit-tested with monkeypatched
platform.system()/env — that is NOT platform evidence (T043 does the
real macOS/Windows runs)."""
import os
import sys
from pathlib import Path

import pytest

from turnkeep.storage import default_data_dir, data_path


def test_linux_default(monkeypatch):
    monkeypatch.delenv("TURNKEEP_DATA_DIR", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    p = default_data_dir(system="Linux")
    assert p == Path.home() / ".local" / "share" / "turnkeep"


def test_macos_default(monkeypatch):
    monkeypatch.delenv("TURNKEEP_DATA_DIR", raising=False)
    p = default_data_dir(system="Darwin")
    assert p == Path.home() / "Library" / "Application Support" / "turnkeep"


def test_windows_default(monkeypatch):
    monkeypatch.delenv("TURNKEEP_DATA_DIR", raising=False)
    monkeypatch.setenv("APPDATA", r"C:\Users\sam\AppData\Roaming")
    p = default_data_dir(system="Windows")
    assert p == Path(r"C:\Users\sam\AppData\Roaming") / "Turnkeep"


def test_env_override_wins(monkeypatch):
    monkeypatch.setenv("TURNKEEP_DATA_DIR", "/tmp/custom-tk")
    assert default_data_dir(system="Windows") == Path("/tmp/custom-tk")


def test_scope_path_sanitises_spaces_and_chinese(tmp_path, monkeypatch):
    monkeypatch.setenv("TURNKEEP_DATA_DIR", str(tmp_path))
    p = data_path("my project 项目", base_dir=str(tmp_path))
    assert " " not in p.name and p.suffix == ".sqlite3"
    assert p.parent == tmp_path


def test_data_path_cwd_independent(tmp_path, monkeypatch):
    monkeypatch.setenv("TURNKEEP_DATA_DIR", str(tmp_path))
    monkeypatch.chdir("/tmp")
    assert data_path("s1") == tmp_path / "s1.sqlite3"
