# Implementation Spec: v1 Slice D2 — LSP JSON-RPC Stdio Client

**Status: spec ready for implementation, not yet built.** Written for
hand-off to a different implementing agent ("omp"); review happens in a
fresh Claude session afterward, this project's usual review role. Read
this spec's "Context" and "Design decisions" in full before coding — D2
sits directly on top of D1 (`pyright_process.py`, committed as `830c8a3`)
and reuses two of its established patterns (injectable seams, Future-based
async-result handoff) rather than reinventing them.

## Context

D1 (lazy per-repo `PyrightProcess`/`PyrightProcessRegistry` subprocess
lifecycle) is **committed and pushed** (`830c8a3`, `src/acie/daemon/
pyright_process.py`, 16/16 focused tests, full suite 665 passed / 3
pre-existing failures — see memory `bd57cb79` for the post-review decision
revision: `basedpyright` is now a **core** dependency, not an optional
extra, and binary discovery checks `basedpyright-langserver` first,
falling back to `pyright-langserver`). D1 spawns the child and wires its
`stdin`/`stdout` pipes but **never reads or writes them** — that is
explicitly D2's job (D1 spec, "Scope of D1", and D1 module docstring
decision 5).

Per the locked v1 slice breakdown (memory `3627eece`), **D2** is: *"LSP
JSON-RPC stdio client (initialize/initialized handshake; evaluate
microsoft/multilspy vs hand-rolled framing)."* This spec resolves that
evaluation and specifies the client precisely. It deliberately does
**not** cover:

- **D3** (the actual background enrichment pass — issuing real
  `textDocument/didOpen`, `textDocument/definition`,
  `textDocument/references` requests against ACIE's own unresolved/
  AMBIGUOUS sites, and writing `INFERRED` facts through `WriteQueue`) or
  **D4** (the merge rule). D2 builds the generic wire client only; it
  does not know what pyright is *for*.
- **D5** (`acie scan [path]` CLI) or **D6** (daemon trigger wiring —
  `runtime.py` never imports this module in this slice, same as D1).

D2 is built and tested **standalone**, against a small fake LSP server
fixture (a `sys.executable`-run script that speaks real Content-Length
framing), with **zero real `basedpyright` process required** for the
unit-test suite — same "prove the isolated piece works" shape D1 used.
**Do not wire this into `pyright_process.py`, `runtime.py`, or
`create_daemon()` in this pass.**

### Resolving the multilspy-vs-hand-rolled question (verified, not assumed)

Research memory `7d12f681` flagged `microsoft/multilspy` as "worth
evaluating... already supports Python via pyright among ~8 languages."
**That specific claim is false and is a second falsified claim from that
memory** (the first being the "identically-named `pyright-langserver`
binary" claim already corrected in memory `334046c0`). Verified directly
against the live package this session:

- `multilspy` 0.0.15's own `pyproject.toml` (`requires-python ">=3.8,
  <4.0"`) pins `jedi-language-server==0.41.3`, `requests==2.32.3`,
  `typing-extensions>=4.2.0`, `psutil>=7.0.0,<8.0.0` as hard runtime
  dependencies.
- Its `src/multilspy/language_servers/` directory has a `jedi_language_
  server/` implementation for Python and **no `pyright`/`basedpyright`
  entry at all** among its ~12 per-language backends (clangd, gopls,
  jdtls, omnisharp, rust-analyzer, typescript-language-server, etc.).
  multilspy's Python support is jedi, not pyright — the exact server
  choice the v1 map already locked in favor of *for its type-inference
  quality* (memory `bd57cb79`'s "keep the original choice... pyright's
  superior type-inference quality").

Depending on multilspy would mean pulling in a pinned, exact-version
`jedi-language-server` dependency ACIE will never use, for a client
library whose actual per-language server configs aren't a drop-in swap
target for basedpyright without forking multilspy itself — the opposite
of "reuse over reinvention" (reuse means reusing something that actually
fits the need, not adopting an unrelated dependency graph for one small
piece of it).

The other library research memory `7d12f681` named, `pylspclient`
(`yeger00/pylspclient`), was also checked directly: PyPI's current
release (0.1.2, last published March 2024) pins `pydantic>=2.5.2,<3.0.0`
and, oddly, `mypy>=1.9.0,<2.0.0` as **runtime** dependencies for what is
fundamentally a ~40-line Content-Length framing helper — disproportionate
weight, and no activity in over a year.

**Decision: hand-roll the Content-Length JSON-RPC framing**, as a new,
small, dedicated module with **zero new runtime dependencies**. This
matches the codebase's own existing precedent exactly: `protocol.py`
already hand-rolls ACIE's *own* wire framing (a 4-byte length prefix) for
its daemon transport rather than depending on any existing RPC/socket
library — LSP's Content-Length framing is the same order of complexity
and the same kind of "small, protocol-dictated format," just imposed by
an external spec instead of ACIE's own choice. `pylspclient`'s
`JsonRpcEndpoint` class remains useful only as a *design reference* (its
read-loop shape: read header lines until blank line, parse
`Content-Length`, read exactly that many body bytes) — not as a
dependency.

## Design decisions

1. **Two new modules, mirroring `protocol.py`'s pure-framing /
   stateful-I/O split**:
   - `src/acie/daemon/lsp_protocol.py` — pure, no I/O: Content-Length
     frame encoding and header/body parsing given already-read bytes.
   - `src/acie/daemon/lsp_client.py` — the stateful `LspClient`: owns a
     background reader thread against a live `PyrightProcess`'s pipes,
     request/response correlation, the `initialize`/`initialized`
     handshake, and best-effort graceful shutdown.
   Unlike `protocol.py` (whose pure functions were deliberately wired
   into real sockets by a *later* slice, `server.py`), D2's framing and
   its one and only wiring target (D1's already-existing `Popen` pipes)
   both exist now — there is no reason to split framing and wiring
   across two slices here, only across two *modules* for the same
   pure/impure separation `protocol.py` established.
2. **Deliberately not importing `protocol.py`'s `decode_frame_body`.**
   Both functions happen to do "decode UTF-8 JSON, verify it's a dict,"
   but they decode two unrelated, independently-versioned wire protocols
   (ACIE's own invented daemon RPC format vs. Microsoft's external LSP
   spec) that must stay decoupled — a future change to one must never
   risk affecting the other. The ~6-line duplication is not a reuse
   violation; sharing a coincidental implementation detail between two
   conceptually unrelated protocols would be the actual mistake.
3. **`lsp_protocol.py` public functions** (all pure, all raise
   `MalformedLspFrameError` — new, module-local exception, not shared
   with `protocol.py`'s `MalformedFrameError`, same "unrelated protocols
   stay decoupled" reasoning as decision 2):
   - `encode_frame(payload: dict) -> bytes` — serializes `payload` to
     compact UTF-8 JSON, prepends
     `f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")`. No
     `Content-Type` header (optional per spec, every real client and
     server omits it in practice — confirmed in research memory
     `7d12f681`, §2).
   - `parse_headers(raw_header_bytes: bytes) -> dict[str, str]` — given
     the raw bytes of every header line already read (excluding the
     final blank line, `\r\n`-joined), splits each line on the first
     `": "`, returns a dict keyed by the **lowercased** header name (LSP
     headers are conventionally `Content-Length`/`Content-Type` but
     matching case-insensitively is correct per HTTP-derived header
     semantics and costs nothing). Raises on a line with no `": "`
     separator.
   - `content_length_from_headers(headers: dict[str, str]) -> int` —
     looks up `headers["content-length"]`, raises if absent or not a
     valid non-negative integer.
   - `decode_body(body: bytes) -> dict` — `json.loads`, raises on
     invalid UTF-8/JSON or a non-dict top-level result. (Structurally
     identical to `protocol.py`'s `decode_frame_body` by necessity — see
     decision 2 for why it is not imported from there.)
4. **`LspClient.__init__(self, process: PyrightProcess) -> None`.** Takes
   an already-live `PyrightProcess` (D1's type) — D2 never spawns, never
   locates a binary, never touches idle-timeout/registry concerns; it
   only reads `process.popen.stdin` / `process.popen.stdout`. Starts one
   daemon background reader thread (`_reader_loop`) immediately in
   `__init__`, since a `LspClient` is only ever constructed once a caller
   already holds a live process — there is no separate "start lazily"
   phase the way D1's registry has one (D1 already owns all the laziness
   this slice needs).
5. **Request/response correlation via `concurrent.futures.Future`,
   reusing `WriteQueue`'s already-established "submit, get a Future back,
   block on it yourself" pattern** (`write_queue.py`'s own module
   docstring: *"Request-handling threads never touch a repo's write
   connection directly: they submit a transaction closure via
   `WriteQueue.submit` and block on the returned Future for the
   result."*) — same shape here: `send_request()` returns a `Future`
   immediately; the reader thread resolves it when the matching response
   frame arrives. No new synchronization primitive invented where a
   proven one already exists in this codebase.
   - Request IDs: `str(uuid.uuid4())`, matching `protocol.py`'s
     `build_request`'s own `str(uuid.uuid4())` convention for ACIE's own
     RPC envelope — consistent ID style across both of ACIE's JSON-RPC-
     shaped protocols, and LSP's spec explicitly allows a string `id`.
   - **Ordering is register-then-write, not write-then-register**: the
     pending `Future` is inserted into `self._pending[request_id]` under
     `self._lock` *before* the frame is written to `stdin` — otherwise a
     pathologically fast reader thread could observe the response before
     the sender finished registering the future it belongs to. If the
     write itself raises, the just-registered pending entry is popped
     back out before re-raising, so a failed send never leaves an
     orphaned Future nobody will ever resolve.
   - `send_request(method: str, params: dict) -> Future` — always
     returns immediately; never blocks. The caller decides how (and
     whether) to wait, via the stdlib `Future.result(timeout=...)` —
     exactly like `WriteQueue.submit()`'s callers already do. D2 does
     not invent its own timeout parameter or polling loop here.
   - `send_notification(method: str, params: dict) -> None` — same
     framing, no `id` field (per JSON-RPC 2.0), no pending-Future
     bookkeeping, fire-and-forget.
   - All writes (`send_request`, `send_notification`, and the reader
     thread's own auto-replies — decision 8) go through one
     `self._write_lock`-guarded `_write_frame()` — `Popen.stdin.write()`
     is not itself safe against concurrent writers interleaving partial
     frames, and D3 (later) will call `send_request` from more than one
     caller thread against the same client.
6. **`initialize(root_path: str, timeout: float = 30.0) -> dict`** —
   the handshake, built on top of decisions 4-5, not a separate raw
   primitive:
   - Guards **must-be-first**: raises `RuntimeError` if called twice, or
     if any other `send_request`/`send_notification` call is attempted
     on a client that has not yet completed `initialize()` — this is a
     real LSP protocol violation (research memory `7d12f681`, §2:
     *"sending requests before `initialized` is a protocol violation"*),
     not just a style preference, so D2 enforces it itself rather than
     trusting every future caller (D3) to remember the ordering.
   - `root_path` must be an absolute path (same precondition D1 already
     assumes reaches its own `repo_root` — see `pyright_process.py`
     decision 3's "already canonical before it reaches this registry").
     Converted via `pathlib.Path(root_path).as_uri()`; a relative path
     raises `ValueError` from `as_uri()` itself, surfaced as a real
     caller bug rather than caught and degraded.
   - Request params sent (all fields verified against research memory
     `7d12f681`'s direct source read of `languageServerBase.ts`):
     `processId` = `os.getpid()` (ACIE's own daemon process is
     pyright's real parent — not `null`, which LSP reserves for "no
     parent process attaches"), `rootUri` and the deprecated `rootPath`
     (both populated for maximum compatibility, per research memory
     `7d12f681`'s note that `rootUri`/`rootPath` are "the deprecated
     single-root fallback still accepted"), `workspaceFolders: [{"uri":
     ..., "name": <basename of root_path>}]` (the modern field), and
     `capabilities: {}`.
   - **`capabilities: {}` is deliberate, not a placeholder.** None of
     ACIE's five target capabilities (definition, references, hover,
     documentSymbol, callHierarchy) require the *client* to declare
     anything — those are all server-advertised capabilities. The one
     capability that *does* require a client declaration to activate
     (pull diagnostics, via `capabilities.textDocument.diagnostic.
     dynamicRegistration`) is exactly the one the v1 map already scoped
     out (research memory `7d12f681`, §1: *"If ACIE only needs the five
     core intelligence capabilities... diagnostics can be ignored
     entirely and this gap doesn't block anything"*) — so omitting it is
     the correct default, not an oversight. **Named follow-up, not
     solved here**: if a later slice wants richer client-side behavior
     (e.g. markdown-formatted hover via `textDocument.hover.
     contentFormat`), that is a `capabilities` field to add then, not a
     gap in this slice.
   - Sends the `initialize` request, blocks on `future.result(timeout=
     timeout)` (a real `TimeoutError`/`LspError` propagates to the
     caller uncaught — see decision 9), then immediately sends the
     `initialized` notification (empty params `{}`) once the response
     arrives — the two-step handshake is atomic from the caller's
     perspective, matching research memory `7d12f681`'s required
     sequence. Stores the response's `capabilities` object as
     `self.server_capabilities` and returns the full `InitializeResult`
     dict to the caller (whoever wants to inspect what pyright actually
     advertised, e.g. a later slice checking `callHierarchyProvider`
     before relying on it).
7. **`LspError(code: int, message: str, data: object | None)` — new
   exception**, raised by the reader thread via `future.set_exception(...)`
   whenever a response frame's body contains an `"error"` object (JSON-RPC
   2.0's own error shape: `{code, message, data?}`) instead of `"result"`.
   Plain, not an `AcieToolError` subclass — this is a client-library-level
   protocol exception, not one of ACIE's own tool-level error codes;
   nothing in D2's scope reaches `dispatch.py`/`errors.py` at all.
8. **Reader thread (`_reader_loop`) behavior for every frame it decodes**,
   dispatched by shape:
   - **Response** (`"id"` present, and `"result"` or `"error"` present):
     pop `self._pending[id]` under `self._lock`; if found, resolve the
     Future (`set_result`/`set_exception`); if the id is *not* found
     (already resolved, or never ours), log a warning and drop it — this
     must never crash the reader loop, since a stray/duplicate/late frame
     is an observation about the server, not a reason to tear down the
     whole client.
   - **Notification from the server** (`"method"` present, no `"id"`):
     logged at `DEBUG` and dropped. Covers `window/logMessage`,
     `textDocument/publishDiagnostics` (pyright pushes these
     unconditionally regardless of what `capabilities` declared — see
     decision 6 — so D2 must tolerate receiving them even though nothing
     in this slice asks for them), and anything else pyright sends
     unprompted. **No semantic handling of diagnostics in this slice** —
     that is D3's concern, if it ever wants them; D2 only guarantees it
     never crashes on one.
   - **Request from the server** (`"method"` *and* `"id"` both present —
     pyright asking ACIE for something, e.g. `workspace/configuration`,
     `client/registerCapability`, `window/workDoneProgress/create`):
     **auto-replied immediately with a generic empty success result**
     (`{"jsonrpc": "2.0", "id": <their id>, "result": null}`), written
     through the same `_write_frame()`/`self._write_lock` decision 5
     already established. `# shortcut: generic empty-result auto-reply
     to every server-initiated request; no per-method semantics (e.g.
     workspace/configuration returning real settings) implemented —
     upgrade when a real need for one of these surfaces, most likely
     workspace/configuration once D3 cares what settings pyright is
     actually using.` This is a deliberate, named shortcut (Coding
     Standards rule 17), not an oversight: without *some* reply, a
     synchronous request from pyright that ACIE never answers is a real
     risk of stalling pyright's own processing indefinitely while it
     waits — research memory `7d12f681` does not establish whether
     pyright would actually block on this, and D2 does not gamble on
     that being safe to ignore.
   - **Malformed/undecodable frame** (bad headers, bad JSON, wrong
     shape): logged at `WARNING` with the raw bytes truncated to a
     bounded length (never an unbounded dump into logs), reader loop
     **continues** to the next frame rather than exiting — one bad frame
     must not silently kill every future request's ability to ever
     resolve.
   - **EOF on stdout** (the child's stdout pipe closed — process exited
     or was killed by D1's own idle-timeout/close path outside this
     client's knowledge): reader loop resolves **every** currently
     pending Future with `ConnectionError("pyright-langserver stdout
     closed")` (so no caller blocks forever on a `Future.result()` that
     can now never resolve), then exits cleanly. This is D2's own
     liveness guarantee — it does not depend on D1 ever notifying D2
     that the process died; D2 discovers this itself, directly, on its
     own read.
9. **No swallowing of protocol-level failures inside D2 itself** —
   `LspError`, `TimeoutError` (from `Future.result(timeout=...)`), and
   `ConnectionError` (decision 8's EOF case) all propagate uncaught to
   whichever caller is holding the `Future`. This deliberately differs
   from D1's "always degrade silently to `None`" vocabulary: D1's own
   failures (binary absent, spawn failed) are facts discovered *at that
   layer*, appropriate to hide behind a uniform `None`. D2's failures are
   protocol-level facts about a specific request that only the *caller*
   (D3, later) has enough context to decide how to degrade — e.g.
   whether an `LspError` on one `textDocument/definition` call should
   skip just that one AMBIGUOUS site or something broader. Silently
   swallowing it inside D2 itself would make a real client-library bug
   (a framing mistake, a malformed request D2 built) indistinguishable
   from a legitimate "pyright doesn't know" answer — exactly the kind of
   thing this slice's own future tests need to be able to see, not
   D2 quietly eating them.
10. **`close(timeout: float = 5.0) -> None`** — D2's own cleanup, **never
    touches the underlying `Popen`** (that stays exclusively D1's job;
    this mirrors D1/D2's existing division of responsibility — D1 owns
    process life/death, D2 owns the protocol conversation over its
    pipes):
    - If the client completed `initialize()` and the process still
      looks alive (`process.is_alive`), attempts a best-effort graceful
      LSP shutdown first: `send_request("shutdown", {})`, waits up to
      `timeout` on its Future, then `send_notification("exit", {})` —
      wrapped in one broad `try/except Exception: pass` (documented
      inline, not silent-by-accident) since a `close()` call must never
      itself raise just because the graceful handshake didn't complete
      cleanly.
    - Sets `self._closed` (a `threading.Event()`), then
      `self._reader_thread.join(timeout=timeout)` — bounded, same
      "shared deadline, never hang forever" vocabulary D1's own
      `close()`/`WatcherRegistry.close()` already established. Sending
      `exit` above should make pyright close its own stdout, which is
      what actually lets the reader thread's blocking read return EOF
      and exit its loop on its own (decision 8) — `close()` does not
      need to interrupt the read itself, only bound how long it waits
      for that natural exit.
    - After the join (whether or not the thread actually stopped in
      time — logged at `WARNING` if not), resolves any Futures still
      pending in `self._pending` with `ConnectionError("LspClient
      closed")`, idempotently (a Future already resolved by decision
      8's own EOF path is skipped, not double-resolved — `Future.
      set_exception` on an already-done Future raises
      `InvalidStateError`, guarded with `if not future.done()`).
    - Idempotent: a second `close()` call is a safe no-op (checked via
      `self._closed.is_set()` at the top).
11. **No `ARCHITECTURE.md` change** — same reasoning as D1 decision 14;
    this slice has no MCP-facing surface, purely internal daemon
    infrastructure.
12. **`DAEMON.md` gets one addition**: extend the existing "## LSP/
    pyright Enrichment Subprocess Lifecycle" section (added by D1) with
    one new paragraph describing D2's client on top of it — do **not**
    give D2 its own top-level `##` section; it is the same subsystem
    continuing to be documented in place, the way C2-C6 all extended
    `architecture.py`'s section rather than each getting a new one.

## Public surface

```python
# src/acie/daemon/lsp_protocol.py

class MalformedLspFrameError(Exception): ...

def encode_frame(payload: dict) -> bytes: ...
def parse_headers(raw_header_bytes: bytes) -> dict[str, str]: ...
def content_length_from_headers(headers: dict[str, str]) -> int: ...
def decode_body(body: bytes) -> dict: ...
```

```python
# src/acie/daemon/lsp_client.py

class LspError(Exception):
    code: int
    message: str
    data: object | None

class LspClient:
    def __init__(self, process: "PyrightProcess") -> None: ...

    server_capabilities: dict | None  # None until initialize() completes

    def send_request(self, method: str, params: dict) -> "Future": ...
    def send_notification(self, method: str, params: dict) -> None: ...
    def initialize(self, root_path: str, timeout: float = 30.0) -> dict: ...
    def close(self, timeout: float = 5.0) -> None: ...
```

No daemon wiring, no MCP-facing change, no new `AcieToolError` codes, no
`dispatch.py`/`mcp_server.py`/`runtime.py`/`pyright_process.py` changes —
standalone and independently testable, same as D1.

## Files to touch

1. **`src/acie/daemon/lsp_protocol.py`** (new) — decision 3.
2. **`src/acie/daemon/lsp_client.py`** (new) — decisions 4-10. Follow this
   codebase's module-docstring convention (see `pyright_process.py`'s own
   top-of-file docstring, which this module should cite as its D1
   foundation, numbering its own decisions 1-12 fresh for D3 to extend).
3. **`tests/daemon/test_lsp_protocol.py`** (new) — pure framing tests, no
   subprocess needed at all.
4. **`tests/daemon/test_lsp_client.py`** (new) — see test list below. Add
   a small fake-LSP-server test helper (e.g.
   `tests/daemon/_fake_lsp_server.py`, run via `[sys.executable,
   "-m", ...]` or `[sys.executable, "<path-to-script>"]` as the `spawn`
   D1 already injects) that speaks real Content-Length framing over
   stdin/stdout: replies to `initialize` with a small canned
   `InitializeResult`, accepts `initialized`, echoes back a canned
   `{"result": {"echo": <params>}}` for any other request method so
   tests can assert round-tripping, replies to `shutdown` with `{"result":
   null}`, and exits its own process on receiving the `exit`
   notification. **This fixture's own frame reading/writing must be
   implemented independently of `lsp_protocol.py`** (plain
   `sys.stdin.buffer`/`sys.stdout.buffer` + hand-written
   `Content-Length` formatting in the fixture script itself) — reusing
   the implementation under test inside its own test's server fixture
   would make a real encoding/decoding bug invisible to the test suite.
5. **`DAEMON.md`** — one new paragraph in the existing LSP section, per
   decision 12.

## Test list

### `tests/daemon/test_lsp_protocol.py`

- `test_encode_frame_prepends_a_correct_content_length_header`
- `test_encode_frame_round_trips_through_parse_headers_and_decode_body`
- `test_parse_headers_is_case_insensitive_for_content_length`
- `test_parse_headers_raises_on_a_line_with_no_colon_separator`
- `test_content_length_from_headers_raises_when_the_header_is_missing`
- `test_content_length_from_headers_raises_on_a_non_integer_value`
- `test_decode_body_raises_on_invalid_utf8`
- `test_decode_body_raises_on_invalid_json`
- `test_decode_body_raises_when_the_top_level_value_is_not_an_object`

### `tests/daemon/test_lsp_client.py`

- `test_initialize_sends_the_handshake_and_returns_the_initialize_result`
  (assert `rootUri`, `processId`, `workspaceFolders` were actually sent —
  fixture echoes received `initialize` params back for inspection, or
  records them to a tmp file the test reads)
- `test_initialize_sends_initialized_notification_after_the_response`
  (fixture records notification receipt)
- `test_initialize_populates_server_capabilities_from_the_response`
- `test_initialize_raises_if_called_twice`
- `test_send_request_before_initialize_raises_runtime_error`
- `test_send_notification_before_initialize_raises_runtime_error`
- `test_send_request_returns_a_future_resolved_with_the_result`
- `test_send_request_future_raises_lsp_error_on_an_error_response`
  (fixture has one method name reserved to always reply with a canned
  JSON-RPC error)
- `test_two_concurrent_send_request_calls_each_resolve_to_their_own_result`
  (two different params in flight at once, assert no cross-talk)
- `test_a_server_initiated_request_receives_a_generic_auto_reply`
  (fixture sends a server->client request of its own during the test and
  asserts it received *some* well-formed response back promptly)
- `test_a_server_initiated_notification_does_not_crash_the_reader_loop`
  (fixture sends an unsolicited notification; a subsequent real request
  still resolves normally afterward)
- `test_a_malformed_frame_does_not_crash_the_reader_loop` (fixture writes
  one deliberately-broken frame directly to the pipe, followed by a real
  one; the real one still resolves)
- `test_pending_requests_are_resolved_with_connection_error_when_the_process_exits`
  (fixture process exits without responding to an in-flight request;
  assert the Future raises `ConnectionError`, not a hang)
- `test_close_sends_shutdown_then_exit_and_joins_the_reader_thread`
- `test_close_resolves_any_still_pending_futures`
- `test_close_is_idempotent`
- `test_close_does_not_touch_the_underlying_popen` (assert D1's
  `PyrightProcess.popen` is untouched by `LspClient.close()` — killing it
  remains exclusively `PyrightProcessRegistry`'s job)

## Workflow reminders for the implementing agent

- Repo TDD convention: write the failing tests first, then implement —
  this project's `tdd` skill, memory `9b020543` ("One Slice Per Session,
  Stop Before Commit for Review").
- **This is one slice.** Implement D2, run the **full** test suite
  (`cd /home/zbalint/workspace/ACIE && .venv/bin/pytest`), not just the
  new tests, and **stop before committing** — leave the diff uncommitted
  for review in a fresh session. Do not `git commit` or push. Current
  confirmed baseline (this session, post-D1): **665 passed, 3 failed** —
  the pre-existing election-port flake (`tests/test_cli.py::
  test_daemon_start_spawns_a_daemon_and_stop_shuts_it_down`,
  `test_daemon_stop_actually_terminates_the_os_process`,
  `tests/test_mcp_server.py::test_serve_mcp_exposes_and_routes_the_ten_tools`),
  tracked since slice A1. Any *other* new failure must be root-caused and
  fixed, not waved through.
- **No new runtime dependencies.** Decision "Resolving the
  multilspy-vs-hand-rolled question" above is final for this slice — do
  not add `multilspy`, `pylspclient`, or any other JSON-RPC/LSP client
  package to `pyproject.toml`. Everything D2 needs is stdlib
  (`concurrent.futures`, `threading`, `json`, `pathlib`, `os`, `uuid`).
- **Do not wire `LspClient` into `pyright_process.py`, `runtime.py`, or
  `create_daemon()`, and do not start D3/D4/D5/D6 in this same pass.** D2
  finishes once its own two modules and tests are green and standalone —
  nothing in the live daemon ever constructs an `LspClient` until D6.
- Standing dogfooding ground rule (map `5d8fa498`'s Notes) still applies
  unchanged: the live `mcp__acie__*` MCP tools stay v0-pinned throughout.
  Fine to use them while implementing; spot-check any result against
  ground truth before it drives a consequential decision.
- The fake LSP server fixture is genuinely a small, real protocol
  partner — do not shortcut it into something that only ever replies
  with hardcoded strings unconditionally; several tests above depend on
  it behaving differently per request (echoing params, replying with a
  canned error, sending its own server-initiated request/notification,
  exiting on `exit`) to actually exercise `LspClient`'s dispatch logic in
  decision 8.

## Acceptance criteria

- All new tests above pass; full existing suite still passes with only
  the three known pre-existing failures.
- `lsp_protocol.py`/`lsp_client.py` are fully standalone: importable and
  testable with zero real `basedpyright`/`pyright-langserver` install,
  zero daemon/runtime wiring, zero MCP-facing change.
- `initialize()` enforces handshake ordering (raises if any other call
  happens first, raises if called twice).
- Every pending `Future` is guaranteed to eventually resolve or raise —
  never hangs — across all three termination paths: a normal response,
  the process exiting mid-request, and `close()`.
- The reader thread never crashes on a malformed frame, an unexpected
  server notification, or a server-initiated request; it always
  auto-replies to the latter.
- `close()` never touches the underlying `Popen` (D1's exclusive
  responsibility) and is idempotent.
- No new runtime dependency added to `pyproject.toml`.
- `DAEMON.md`'s existing LSP section gains one new paragraph;
  `ARCHITECTURE.md` is untouched.
- Diff left uncommitted, ready for review.
