"""Independent Content-Length-speaking LSP fixture for LspClient tests."""

import json
import sys
from pathlib import Path


_LOG_PATH = Path(sys.argv[1])


def _record(event: dict) -> None:
    with _LOG_PATH.open("a", encoding="utf-8") as log:
        log.write(json.dumps(event) + "\n")


def _read_message() -> dict | None:
    headers: dict[str, str] = {}
    while True:
        line = sys.stdin.buffer.readline()
        if line == b"":
            return None
        if line in (b"\r\n", b"\n"):
            break
        name, value = line.decode("ascii").rstrip("\r\n").split(": ", 1)
        headers[name.lower()] = value
    body = sys.stdin.buffer.read(int(headers["content-length"]))
    return json.loads(body.decode("utf-8"))


def _send_message(message: dict) -> None:
    body = json.dumps(message, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    sys.stdout.buffer.flush()


def _send_server_request() -> None:
    _send_message({"jsonrpc": "2.0", "id": "server-request", "method": "workspace/configuration", "params": {}})
    response = _read_message()
    _record({"server_request_response": response})


def main() -> None:
    while (message := _read_message()) is not None:
        _record(message)
        method = message.get("method")
        if method == "initialized":
            continue
        if method == "exit":
            return
        if method == "initialize":
            _send_message(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {"capabilities": {"definitionProvider": True}},
                }
            )
            continue
        if method == "shutdown":
            _send_message({"jsonrpc": "2.0", "id": message["id"], "result": None})
            continue
        if method == "error":
            _send_message(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32601, "message": "reserved error", "data": {"method": method}},
                }
            )
            continue
        if method == "server_request":
            _send_server_request()
        elif method == "notification":
            _send_message({"jsonrpc": "2.0", "method": "window/logMessage", "params": {}})
        elif method == "malformed":
            sys.stdout.buffer.write(b"Broken-Header\r\n\r\n")
            sys.stdout.buffer.flush()
        elif method == "exit_without_response":
            return
        _send_message({"jsonrpc": "2.0", "id": message["id"], "result": {"echo": message["params"]}})


if __name__ == "__main__":
    main()
