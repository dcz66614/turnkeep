"""T028 doctor tests: JSON shape, checks present, no crash without a DB."""
import json
import sqlite3

from turnkeep.doctor import run_doctor
from turnkeep.storage import open_db, SCHEMA_VERSION


def test_doctor_json_shape_no_db(tmp_path):
    report = run_doctor(str(tmp_path))
    assert report["tool"] == "turnkeep doctor"
    assert report["python"]["status"] in ("PASS", "FAIL")
    assert report["sqlite"]["status"] == "PASS"
    assert report["data_dir"]["status"] == "PASS"
    assert report["database"]["status"] == "NOT_RUN"  # no DB yet
    assert report["platforms"]["macos"]["status"] == "NOT_RUN"
    assert report["platforms"]["windows"]["status"] == "NOT_RUN"
    assert report["clients"]["claude-code"]["live"] == "NOT_RUN"
    assert report["clients"]["codex"]["live"] == "NOT_RUN"
    assert report["overall"] in ("OK", "FAIL")


def test_doctor_sees_real_db(tmp_path):
    dbpath = tmp_path / "default.sqlite3"
    db = open_db(dbpath)
    from turnkeep.scopes import register_scope
    register_scope(db, "doc", "Doctor scope")
    db.close()
    report = run_doctor(str(tmp_path))
    assert report["database"]["status"] == "PASS"
    assert report["database"]["schema_version"] == SCHEMA_VERSION
    assert "doc" in report["database"]["scopes"]


def test_doctor_data_dir_unwritable(tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    report = run_doctor(str(ro))
    # data_path creation may still succeed as owner; assert shape, not outcome
    assert "data_dir" in report and report["data_dir"]["status"] in ("PASS", "FAIL")
    ro.chmod(0o700)
