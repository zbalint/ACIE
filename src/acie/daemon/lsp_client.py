"""Stateful JSON-RPC client over D1's live pyright subprocess pipes.

This module implements D2 decisions 1-12. It owns an LSP conversation only:
D1's ``PyrightProcessRegistry`` retains process creation, lifetime, and
termination ownership, while later D3/D6 slices retain enrichment and daemon
wiring. D2 deliberately uses stdlib Futures for request/result handoff,
matching ``write_queue.py``'s established caller-blocks-on-a-Future seam.

Decisions: (1) separate pure framing in lsp_protocol; (2) do not couple it to
ACIE daemon framing; (3) module-local protocol errors; (4) eager reader;
(5) register-before-write Futures plus serialized writes; (6) mandatory
initialize/initialized ordering; (7) client-level LspError; (8) resilient
shape-dispatching reader and generic server-request replies; (9) propagate
per-request protocol failures; (10) best-effort protocol close only; (11) no
MCP surface; (12) lifecycle documentation stays in DAEMON.md's LSP section.
"""

import logging
import os
import threading
import uuid
from concurrent.futures import Future
from pathlib import Path
from typing import TYPE_CHECKING

from acie.daemon.lsp_protocol import (
    MalformedLspFrameError,
    content_length_from_headers,
    decode_body,
    encode_frame,
    parse_headers,
)

if TYPE_CHECKING:
    from acie.daemon.pyright_process import PyrightProcess

_logger = logging.getLogger(__name__)
_RAW_LOG_LIMIT = 512


class LspError(Exception):
    """A JSON-RPC error returned by the LSP server."""

    def __init__(self, code: int, message: str, data: object | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class LspClient:
    """One LSP conversation over an already-live D1 ``PyrightProcess``."""

    def __init__(self, process: "PyrightProcess") -> None:
        self._process = process
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._pending: dict[str, Future] = {}
        self._closed = threading.Event()
        self._initialize_started = False
        self._closing = False
        self._initialized = False
        self.server_capabilities: dict | None = None
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    def send_request(self, method: str, params: dict) -> Future:
        """Send a post-initialization request and return its response Future."""
        return self._send_request(method, params, require_initialized=True)

    def send_notification(self, method: str, params: dict) -> None:
        """Send a post-initialization notification."""
        with self._lock:
            if not self._initialized:
                raise RuntimeError("LspClient must complete initialize() before other LSP messages")
            if self._closing or self._closed.is_set():
                raise RuntimeError("LspClient is closed")
        self._write_frame({"jsonrpc": "2.0", "method": method, "params": params})

    def initialize(self, root_path: str, timeout: float = 30.0) -> dict:
        """Complete the required initialize/initialized handshake."""
        with self._lock:
            if self._initialize_started:
                raise RuntimeError("LspClient.initialize() may only be called once")
            self._initialize_started = True

        root = Path(root_path)
        root_uri = root.as_uri()
        result = self._send_request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": root_uri,
                "rootPath": root_path,
                "workspaceFolders": [{"uri": root_uri, "name": root.name}],
                "capabilities": {},
            },
        ).result(timeout=timeout)
        if not isinstance(result, dict):
            raise MalformedLspFrameError("initialize result must be a JSON object")
        capabilities = result.get("capabilities")
        if not isinstance(capabilities, dict):
            raise MalformedLspFrameError("initialize result capabilities must be a JSON object")

        self._write_frame({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        with self._lock:
            self.server_capabilities = capabilities
            self._initialized = True
        return result

    def close(self, timeout: float = 5.0) -> None:
        """Best-effort LSP shutdown without terminating D1's underlying process."""
        with self._lock:
            if self._closing:
                return
            self._closing = True
            initialized = self._initialized

        if initialized and self._process.is_alive:
            try:
                self._send_request("shutdown", {}, allow_closing=True).result(timeout=timeout)
                self._write_frame({"jsonrpc": "2.0", "method": "exit", "params": {}})
            except Exception:  # Best effort: close must survive an already-broken LSP conversation.
                _logger.debug("LSP graceful shutdown failed", exc_info=True)

        self._closed.set()
        self._reader_thread.join(timeout=timeout)
        if self._reader_thread.is_alive():
            _logger.warning("LSP reader thread did not stop within %.2f seconds", timeout)
        self._fail_pending(ConnectionError("LspClient closed"))

    def _send_request(
        self,
        method: str,
        params: dict,
        *,
        require_initialized: bool = False,
        allow_closing: bool = False,
    ) -> Future:
        request_id = str(uuid.uuid4())
        future: Future = Future()
        with self._write_lock:
            with self._lock:
                if require_initialized and not self._initialized:
                    raise RuntimeError("LspClient must complete initialize() before other LSP messages")
                if self._closed.is_set() or (self._closing and not allow_closing):
                    raise RuntimeError("LspClient is closed")
                self._pending[request_id] = future
            try:
                self._write_frame_locked(
                    {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
                )
            except BaseException:
                with self._lock:
                    self._pending.pop(request_id, None)
                raise
        return future

    def _write_frame(self, payload: dict) -> None:
        with self._write_lock:
            self._write_frame_locked(payload)

    def _write_frame_locked(self, payload: dict) -> None:
        """Write one framed message while the caller holds ``_write_lock``."""
        stdin = self._process.popen.stdin
        if stdin is None:
            raise ConnectionError("pyright-langserver stdin is unavailable")
        stdin.write(encode_frame(payload))
        stdin.flush()

    def _reader_loop(self) -> None:
        while not self._closed.is_set():
            try:
                message = self._read_message()
            except MalformedLspFrameError as exc:
                _logger.warning("Ignoring malformed LSP frame: %s", exc)
                continue
            except Exception as exc:
                _logger.warning("LSP reader failed while reading a frame", exc_info=True)
                self._fail_pending(ConnectionError(f"pyright-langserver stdout read failed: {exc}"))
                return
            if message is None:
                self._fail_pending(ConnectionError("pyright-langserver stdout closed"))
                return
            try:
                self._dispatch_message(message)
            except Exception:
                _logger.warning(
                    "Ignoring malformed LSP message: %r",
                    repr(message)[:_RAW_LOG_LIMIT],
                    exc_info=True,
                )

    def _read_message(self) -> dict | None:
        stdout = self._process.popen.stdout
        if stdout is None:
            return None
        lines: list[bytes] = []
        while True:
            line = stdout.readline()
            if line == b"":
                return None
            if line in (b"\r\n", b"\n"):
                break
            lines.append(line.rstrip(b"\r\n"))
        raw_header = b"\r\n".join(lines)
        try:
            headers = parse_headers(raw_header)
            length = content_length_from_headers(headers)
        except MalformedLspFrameError as exc:
            raise MalformedLspFrameError(
                f"{exc}; raw frame bytes={raw_header[:_RAW_LOG_LIMIT]!r}"
            ) from exc
        body = stdout.read(length)
        if len(body) != length:
            raise MalformedLspFrameError(
                f"LSP frame body declared {length} bytes but stdout supplied {len(body)}; "
                f"raw frame bytes={body[:_RAW_LOG_LIMIT]!r}"
            )
        try:
            return decode_body(body)
        except MalformedLspFrameError as exc:
            raise MalformedLspFrameError(
                f"{exc}; raw frame bytes={body[:_RAW_LOG_LIMIT]!r}"
            ) from exc

    def _dispatch_message(self, message: dict) -> None:
        if "id" in message and ("result" in message or "error" in message):
            self._handle_response(message)
        elif "method" in message and "id" in message:
            # shortcut: generic empty-result auto-reply to every server-initiated request; no per-method
            # semantics (notably workspace/configuration) until D3 needs real pyright settings.
            try:
                self._write_frame({"jsonrpc": "2.0", "id": message["id"], "result": None})
            except Exception:
                _logger.warning("Could not auto-reply to LSP server request", exc_info=True)
        elif "method" in message:
            _logger.debug("Ignoring LSP server notification %r", message.get("method"))
        else:
            _logger.warning("Ignoring unrecognized LSP message: %r", repr(message)[:_RAW_LOG_LIMIT])

    def _handle_response(self, message: dict) -> None:
        request_id = message["id"]
        with self._lock:
            future = self._pending.pop(request_id, None)
        if future is None:
            _logger.warning("Ignoring LSP response for unknown request id %r", request_id)
            return
        if "error" in message:
            error = message["error"]
            if not isinstance(error, dict):
                future.set_exception(MalformedLspFrameError("LSP response error must be an object"))
                return
            future.set_exception(LspError(error.get("code"), error.get("message"), error.get("data")))
            return
        future.set_result(message["result"])

    def _fail_pending(self, error: Exception) -> None:
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for future in pending:
            if not future.done():
                future.set_exception(error)
