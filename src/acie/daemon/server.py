"""The daemon's socket accept-loop, connect-per-call handling, election-port
startup mutex, discovery-file lifecycle, and graceful shutdown/signal wiring.

See DAEMON.md "IPC Transport", "Process Lifecycle: Auto-Spawn on Demand",
and "Shutdown / Stop Semantics". `DaemonServer` is the transport/process-
lifecycle layer only: it takes an already-assembled `dispatch` callable
(request envelope dict -> response envelope dict) and an `on_shutdown`
callback (draining every per-repo write queue) as injected seams --
constructing the real `dispatch_request`/`WriteQueue`/`BootstrapCoordinator`
wiring, and the CLI surface that spawns this module as a subprocess, are
later slices (bootstrap.py and write_queue.py's own docstrings already
named this daemon-server slice as where that wiring lands; this slice adds
the transport/lifecycle layer those seams plug into, not the plug itself).
"""

import faulthandler
import os
import signal
import socket
import threading
from typing import Callable

from acie.daemon.discovery import delete_discovery_file, write_discovery_file
from acie.daemon.protocol import (
    LENGTH_PREFIX_SIZE,
    MalformedFrameError,
    build_error_response,
    build_success_response,
    decode_frame_body,
    decode_length_prefix,
    encode_frame,
)
from acie.daemon.sockets import recv_exact

# `method: "shutdown"` is a transport-level control message, not one of the
# 10 tools in dispatch.py's DISPATCH_TABLE -- DAEMON.md's "Shutdown / Stop
# Semantics" names it as an RPC over the already-locked envelope, so it's
# intercepted here before ever reaching the injected `dispatch` callable.
_SHUTDOWN_METHOD = "shutdown"
_PING_METHOD = "ping"

_ACCEPT_BACKLOG = 128

# One ACIE daemon is shared by this machine, so this deliberately fixed port
# is an OS-level election mutex rather than a per-repository service endpoint.
DAEMON_ELECTION_PORT = 57831


class AnotherDaemonRunningError(Exception):
    """Raised by start() when the election port is already held.

    Per DAEMON.md "Process Lifecycle": the election port is a fixed,
    exclusively-bound OS-level mutex during daemon startup -- a bind
    failure here means another daemon is already starting or running, and
    this process must not proceed to a second full startup.
    """


class DaemonServer:
    """One daemon's loopback-TCP server: connect-per-call RPC + lifecycle.

    `dispatch` is called for every non-shutdown request and must return a
    complete response envelope dict (see protocol.py's
    `build_success_response`/`build_error_response`). `on_shutdown`, if
    given, is called once during a graceful shutdown to drain per-repo
    write queues -- injected rather than constructed here so this module
    stays decoupled from WriteQueue/BootstrapCoordinator construction.

    `election_port`, if given, is bound as an exclusive startup mutex (see
    AnotherDaemonRunningError). `discovery_path`/`auth_token`, if given,
    make `start()` write `~/.acie/daemon.json`-shaped discovery file (see
    discovery.py) and `shutdown()` delete it.
    """

    def __init__(
        self,
        dispatch: Callable[[dict], dict],
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        election_port: int | None = None,
        discovery_path: str | None = None,
        auth_token: str | None = None,
        on_shutdown: Callable[[], None] | None = None,
    ) -> None:
        self._dispatch = dispatch
        self._host = host
        self._requested_port = port
        self._election_port = election_port
        self._discovery_path = discovery_path
        self._auth_token = auth_token
        self._on_shutdown = on_shutdown

        self._election_sock: socket.socket | None = None
        self._server_sock: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None

        self._lock = threading.Lock()
        self._shutting_down = threading.Event()
        self._shutdown_started = False

    @property
    def port(self) -> int:
        if self._server_sock is None:
            raise RuntimeError("server has not been started")
        return self._server_sock.getsockname()[1]

    def start(self) -> None:
        server_sock: socket.socket | None = None
        discovery_published = False
        try:
            if self._election_port is not None:
                self._election_sock = self._bind_election_port(self._election_port)

            server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server_sock.bind((self._host, self._requested_port))
            server_sock.listen(_ACCEPT_BACKLOG)
            self._server_sock = server_sock

            if self._discovery_path is not None:
                write_discovery_file(
                    self._discovery_path,
                    service_port=self.port,
                    auth_token=self._auth_token,
                    daemon_pid=os.getpid(),
                )
                discovery_published = True

            self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
            self._accept_thread.start()
        except Exception:
            if discovery_published and self._discovery_path is not None:
                delete_discovery_file(self._discovery_path)
            if server_sock is not None:
                server_sock.close()
            self._server_sock = None
            self._accept_thread = None
            if self._election_sock is not None:
                self._election_sock.close()
                self._election_sock = None
            raise

    def _bind_election_port(self, election_port: int) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind((self._host, election_port))
        except OSError as exc:
            sock.close()
            raise AnotherDaemonRunningError(
                f"election port {election_port} is already bound -- "
                "another daemon is starting or already running"
            ) from exc
        try:
            sock.listen(1)
        except OSError:
            # Not AnotherDaemonRunningError -- bind() already succeeded, so
            # this isn't "already bound", it's a genuine local failure
            # (e.g. resource exhaustion). Close the socket so it isn't
            # leaked and the election port doesn't stay permanently held.
            sock.close()
            raise
        return sock

    def serve_forever(self) -> None:
        """Blocks the calling thread until a graceful shutdown completes."""
        if self._accept_thread is not None:
            self._accept_thread.join()

    def shutdown(self) -> None:
        """Graceful shutdown: same routine the `shutdown` RPC and SIGTERM/SIGINT trigger.

        Idempotent -- a second call is a no-op. Order matches DAEMON.md
        "Shutdown / Stop Semantics": stop accepting new RPCs first (so
        anything arriving mid-drain gets DAEMON_SHUTTING_DOWN rather than a
        connection hang), drain every per-repo write queue, delete the
        discovery file, then close the listening socket.
        """
        with self._lock:
            if self._shutdown_started:
                return
            self._shutdown_started = True
        self._shutting_down.set()

        if self._on_shutdown is not None:
            self._on_shutdown()
        if self._discovery_path is not None:
            delete_discovery_file(self._discovery_path)

        if self._server_sock is not None:
            self._server_sock.close()
        if self._election_sock is not None:
            self._election_sock.close()

    def _accept_loop(self) -> None:
        while True:
            try:
                conn, _addr = self._server_sock.accept()
            except OSError:
                return  # listening socket closed by shutdown() -- clean exit.
            threading.Thread(target=self._handle_connection, args=(conn,), daemon=True).start()

    def _handle_connection(self, conn: socket.socket) -> None:
        try:
            request = self._read_request(conn)
            if request is None:
                return
            try:
                response = self._dispatch_one(request)
            except Exception as exc:  # noqa: BLE001 -- dispatch_request already wraps tool
                # errors into INTERNAL_ERROR responses; this is a last-resort guard against
                # a genuinely unexpected exception (e.g. a missing `git` binary) escaping
                # dispatch entirely. It must still reach the client as a response instead of
                # silently closing the connection, which is indistinguishable from "no
                # daemon running" and was this bug: a bare except here used to swallow it.
                response = build_error_response(request.get("id"), "INTERNAL_ERROR", str(exc))
            conn.sendall(encode_frame(response))
        except (MalformedFrameError, OSError):
            # Framing failure while reading, or the peer disconnected before/while we
            # replied -- neither has a request to answer or a connection left to answer on.
            pass
        finally:
            conn.close()

    def _read_request(self, conn: socket.socket) -> dict | None:
        prefix = recv_exact(conn, LENGTH_PREFIX_SIZE)
        if prefix is None:
            return None
        length = decode_length_prefix(prefix)
        body = recv_exact(conn, length)
        if body is None:
            return None
        return decode_frame_body(body)

    def _dispatch_one(self, request: dict) -> dict:
        request_id = request.get("id")
        method = request.get("method")

        if method == _SHUTDOWN_METHOD:
            self.shutdown()
            return build_success_response(request_id, {"status": "shutting_down"})

        if self._shutting_down.is_set():
            return build_error_response(
                request_id, "DAEMON_SHUTTING_DOWN", "daemon is shutting down"
            )

        if method == _PING_METHOD:
            return build_success_response(request_id, {"status": "ok"})

        return self._dispatch(request)


def install_signal_handlers(server: DaemonServer) -> None:
    """Wires SIGTERM/SIGINT to the exact same graceful shutdown() routine.

    Per DAEMON.md "Shutdown / Stop Semantics": "SIGTERM/SIGINT are wired to
    the exact same graceful drain-and-cleanup routine, so systemd,
    containers, and a plain kill all get a clean exit too -- not just the
    CLI path." Installing handlers is left as a separate opt-in call (not
    automatic in __init__/start()) so tests and embedding callers that
    don't want process-wide signal handlers replaced aren't forced to.
    """

    def _handler(signum, frame):  # noqa: ARG001 -- signal.signal's required handler shape.
        server.shutdown()

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def main() -> int:
    """Run the production daemon in the foreground until it shuts down."""
    from acie.daemon.runtime import create_daemon

    # A fatal signal (SIGSEGV/SIGABRT/SIGFPE/SIGBUS) bypasses Python's own
    # exception/logging machinery entirely, so without this a crash leaves
    # nothing in daemon.log to explain it (see the 2026-09-01 live-test
    # segfault this daemon suffered with zero trace of why). faulthandler
    # dumps the native Python frame stack to stderr right before the
    # process dies, and _spawn_daemon (cli.py) already redirects this
    # process's stderr to daemon.log.
    faulthandler.enable()
    server = create_daemon(election_port=DAEMON_ELECTION_PORT)
    install_signal_handlers(server)
    server.start()
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
