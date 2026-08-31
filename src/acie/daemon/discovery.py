"""Atomic read/write/delete of the daemon discovery file.

See DAEMON.md "Process Lifecycle: Auto-Spawn on Demand": `~/.acie/daemon.json`
holds `{service_port, auth_token, daemon_pid}`, written atomically
(`O_CREAT|O_EXCL`, mode `0600`, to a PID-qualified temp path, then
`os.replace`) so a reader never observes a partially-written file.

Pure filesystem I/O only -- no socket or daemon-lifecycle logic here. The
daemon server writes on startup and deletes on shutdown; `acie serve-mcp`'s
client-probe path reads it. Both are later slices; this module owns only
the file format and atomicity.
"""

import json
import os


def write_discovery_file(
    path: str, *, service_port: int, auth_token: str | None, daemon_pid: int
) -> None:
    payload = {
        "service_port": service_port,
        "auth_token": auth_token,
        "daemon_pid": daemon_pid,
    }
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)

    # PID-qualified so two processes racing to write never collide on the
    # same temp path; O_CREAT|O_EXCL is the atomicity guarantee DAEMON.md
    # specifies for this write.
    tmp_path = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except BaseException:
        os.remove(tmp_path)
        raise
    os.replace(tmp_path, path)


def read_discovery_file(path: str) -> dict | None:
    """None on any missing/unreadable/corrupt file -- never raises.

    Per DAEMON.md's client-probe contract: "a missing file, unreadable
    file, or failed ping triggers a spawn" -- the caller treats every
    failure mode here identically, so there is no need to distinguish them.
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def delete_discovery_file(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
