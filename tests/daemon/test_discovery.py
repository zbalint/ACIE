import glob
import json
import os
import stat

import pytest

from acie.daemon.discovery import (
    delete_discovery_file,
    read_discovery_file,
    write_discovery_file,
)


def _path(tmp_path):
    return str(tmp_path / "daemon.json")


def test_write_then_read_round_trips_the_payload(tmp_path):
    path = _path(tmp_path)

    write_discovery_file(path, service_port=54321, auth_token=None, daemon_pid=9999)

    assert read_discovery_file(path) == {
        "service_port": 54321,
        "auth_token": None,
        "daemon_pid": 9999,
    }


def test_write_creates_parent_directory_if_missing(tmp_path):
    path = str(tmp_path / "nested" / "daemon.json")

    write_discovery_file(path, service_port=1, auth_token=None, daemon_pid=1)

    assert os.path.exists(path)


def test_write_sets_owner_only_permissions(tmp_path):
    path = _path(tmp_path)

    write_discovery_file(path, service_port=1, auth_token="tok", daemon_pid=1)

    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600


def test_write_leaves_no_leftover_temp_file(tmp_path):
    path = _path(tmp_path)

    write_discovery_file(path, service_port=1, auth_token=None, daemon_pid=1)

    assert glob.glob(str(tmp_path / "*.tmp")) == []


def test_write_removes_the_temp_file_when_the_final_replace_fails(tmp_path, monkeypatch):
    # Regression: os.replace(tmp_path, path) sat outside the try/except
    # that cleans up the temp file, so a rename failure after a fully
    # written temp file leaked it permanently.
    path = _path(tmp_path)

    def failing_replace(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr("acie.daemon.discovery.os.replace", failing_replace)

    with pytest.raises(OSError, match="replace failed"):
        write_discovery_file(path, service_port=1, auth_token=None, daemon_pid=1)

    assert glob.glob(str(tmp_path / "*.tmp")) == []


def test_write_is_atomic_a_reader_never_observes_a_partial_file(tmp_path):
    # os.replace is atomic on POSIX -- once the file exists at `path` at
    # all, it must already contain complete, valid JSON; there is no
    # window where a reader can see a truncated/partial write.
    path = _path(tmp_path)

    write_discovery_file(path, service_port=42, auth_token=None, daemon_pid=7)

    with open(path, encoding="utf-8") as f:
        # Would raise if the file were ever observably partial.
        payload = json.load(f)
    assert payload["service_port"] == 42


def test_read_missing_file_returns_none(tmp_path):
    assert read_discovery_file(_path(tmp_path)) is None


def test_read_malformed_json_returns_none(tmp_path):
    path = _path(tmp_path)
    with open(path, "w", encoding="utf-8") as f:
        f.write("not json{{{")

    assert read_discovery_file(path) is None


def test_read_non_object_json_returns_none(tmp_path):
    path = _path(tmp_path)
    with open(path, "w", encoding="utf-8") as f:
        f.write("[1, 2, 3]")

    assert read_discovery_file(path) is None


def test_delete_removes_an_existing_file(tmp_path):
    path = _path(tmp_path)
    write_discovery_file(path, service_port=1, auth_token=None, daemon_pid=1)

    delete_discovery_file(path)

    assert not os.path.exists(path)


def test_delete_is_idempotent_when_file_is_already_missing(tmp_path):
    path = _path(tmp_path)

    delete_discovery_file(path)  # must not raise
    delete_discovery_file(path)  # must not raise
