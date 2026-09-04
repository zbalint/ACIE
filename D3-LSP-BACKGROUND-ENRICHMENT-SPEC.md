# v1 Slice D3 — Background pyright Enrichment Pass

Status: spec-and-plan only, written for external implementation (same role
split as D1/D2 — this document was **not** implemented in the session that
wrote it; a fresh session should implement it, and another fresh session
should review the resulting diff against this spec before commit).

Implements the fifth piece of Capability D (wayfinder ticket `89be4cc1`)
per the locked D1–D6 breakdown (SALTMDB memory `3627eece`): the background
enrichment pass that issues real `textDocument/didOpen`,
`textDocument/definition` (and, where useful, `textDocument/references`)
requests over D2's `LspClient` against ACIE's own `AMBIGUOUS`/unresolved
`calls`+`inherits` sites, and writes newly-resolved facts through the
existing per-repo `WriteQueue` as `confidence=INFERRED`, with provenance
stamped from a **live-queried** basedpyright version (resolving the open
question left by D1's decision-4/16 revision, memory `bd57cb79` — see
"Provenance version source" below).

## Why D3 is next

D2 is committed (`d3e4b90`, not yet pushed) and independently review-signed
off (memory `4201c082`): `LspClient` gives D3 `initialize()` /
`send_request()` / `send_notification()` / `close()` over an already-live
D1 `PyrightProcess`, with request/response correlation via
`concurrent.futures.Future` and a documented "propagate protocol failures
to the caller" contract (D2 decision 9) that D3 is the first real consumer
of. D4 (merge-rule enforcement) is explicitly the *next* slice after D3 per
`4201c082`'s own handoff note — this spec is deliberately scoped to leave
D4 real work to do (see "What D3 does **not** do" below), not to pre-empt
it.

Baseline reconfirmed this session: `.venv/bin/pytest` → 692 passed, 3
failed (the same pre-existing election-port flake tracked since slice A1 —
unchanged).

## Two open questions this spec had to resolve, verified live (not guessed)

### 1. Provenance version source

D1's decision-4/16 revision (`bd57cb79`) already established, via a real
smoke test, that probing `basedpyright-langserver --version` (the binary
D1 actually spawns) returns `None` — basedpyright's langserver has no
`--version` CLI surface — and left "provenance versioning will need a
different source if it's ever required" as an open item for whichever
slice needed it. D3 is that slice.

Verified live this session (spawned a real `PyrightProcessRegistry` +
`LspClient` against this repo, called `initialize()`, printed the full
raw result — not just the `capabilities` sub-object `LspClient` itself
keeps): the **LSP `initialize` response's `serverInfo` field** carries it
directly:

```json
{"name": "basedpyright", "version": "1.39.10"}
```

`LspClient.initialize()` already returns the full `result` dict (it only
*stores* the `capabilities` sub-object on `self.server_capabilities`,
discarding the rest) — so **no D2 changes are needed**: D3's own caller of
`initialize()` reads `result.get("serverInfo", {})` itself, once per pass,
and stamps every `Provenance` this pass produces with
`provider=server_info.get("name", "basedpyright")`,
`version=server_info.get("version", "unknown")`. This is a real, live,
per-pass version, not a cached/hardcoded string — if the user upgrades
`basedpyright` between passes, the next pass's writes reflect it
automatically.

### 2. Mapping a pyright `Location` back to an ACIE symbol id — `at_start()` does not work

D3's most safety-critical step is: given a pyright `textDocument/definition`
response (a file + 0-indexed line/character), find the ACIE `Symbol` it
names, so the `Relation` D3 writes has a valid `target` (an ACIE symbol
id — never a raw path/position). `resolve.py`'s existing position
resolution (`SymbolStore.at_start`) matches a symbol whose own **start**
position exactly equals the query point — but that assumes the query
point already *is* a symbol's recorded start.

Live-verified this session (real `didOpen` + `textDocument/definition` at
the call site of `extract_symbols(...)` in `indexer.py`) that this
assumption is false for pyright: pyright's response range starts at the
**name identifier** token —

```json
{"uri": ".../extract_symbols.py",
 "range": {"start": {"line": 27, "character": 4}, "end": {"line": 27, "character": 19}}}
```

— i.e. `def·extract·_symbols(...)`'s `e`, at column 4. `extract_symbols.py`'s
own `_build_symbol` (line 89-90) instead records `start_col` from
`node.start_point` of the **whole** `function_definition`/`class_definition`
node — column 0, at the `def`/`class` keyword. These two columns *never*
coincide for any def/class ACIE tracks (`def ` is 4 chars, `class ` is 6),
so `at_start()` would silently fail to resolve **every** real pyright
definition result. This is a genuine schema gap, not a query bug —
`SymbolStore` has no containment query today (`at_start` is exact-point
only). D3's spec includes a new `SymbolStore.at_position()` query to fix
this (design decision 4 below).

`site_line`/`site_col` for `calls`/`inherits` relations were separately
confirmed (via `extract_relations.py`, e.g. lines 174-175, 334-335) to
already be the **name-node's own** `start_point` — the call/base
identifier token, 1-indexed row + 0-indexed col — so the *query* position
D3 sends to pyright (`line=site_line-1, character=site_col`) needs no
special-casing; only the *response*-side mapping needed fixing.

## Scope of D3 (narrow, deliberately not D4/D5/D6)

One new module, `src/acie/daemon/lsp_enrichment.py`
(`run_enrichment_pass(...)`), plus two small, surgical extensions to
existing modules:

- `src/acie/storage/symbol_store.py`: new `SymbolStore.at_position(path,
  line, col) -> Symbol | None` (design decision 4).
- `src/acie/indexer.py`: new public `unresolved_deferred_sites(...)`
  helper (design decision 3) — a **read-only classification** refactor of
  the existing `_resolve_deferred` candidate lookup, not a change to
  indexing behavior; `index_file`'s own output is untouched.

Explicitly does **not** touch `runtime.py` / `dispatch.py` / `mcp_server.py`
/ `bootstrap.py` — no daemon trigger wiring (that's D6), no `acie scan` CLI
command (D5), no MCP tool surface. Does not touch `lsp_client.py` /
`lsp_protocol.py` (D2's contract already covers everything D3 needs — see
"Provenance version source" above). Does not touch the `overrides`
predicate — the locked breakdown scopes D3 to "call+inherits sites" only;
overrides enrichment, if ever wanted, is a future ticket, not silently
folded in here.

### What D3 does **not** do (left for D4, on purpose)

`RelationStore.upsert()` is keyed on `(source, target, predicate,
site_file, site_line, site_col)` and unconditionally overwrites on
conflict. D3 only ever targets a site that is *currently*
`AMBIGUOUS`-confidence or has *no* live relation at all (an "unresolved"
site, see design decision 3) — an `EXTRACTED` relation's key never appears
in either category, so D3's own writes structurally cannot regress an
`EXTRACTED` fact to a lower confidence. What D3 does **not** attempt is
cleaning up an `AMBIGUOUS` site's *other*, now-superseded candidate rows
once one candidate is confidently resolved to `INFERRED` — e.g. if a call
site has two `AMBIGUOUS` rows (targets `A` and `B`) and pyright resolves it
to `A`, D3's write upgrades the `A` row to `INFERRED` in place but leaves
the stale `B` row exactly as it was. Retiring/tombstoning a site's
now-incorrect siblings once one candidate wins — the actual "upgrade
AMBIGUOUS → INFERRED **only**, never regress EXTRACTED" merge policy named
in the locked breakdown — is D4's explicit charter (`3627eece`,
`4201c082`). Folding that cleanup into D3 would absorb D4's job; this spec
deliberately leaves the stale-sibling rows in place for D4 to define and
implement a real policy for (this is the one "obviously imperfect but
intentional" corner of this design — a genuine gap, not a documented
`# shortcut:`, since fixing it is a whole separate slice's charter rather
than a narrower ceiling D3 itself could raise later).

## Design decisions

1. **`run_enrichment_pass(repo_root, repo_id, process_registry, write_queue,
   walk_repo, symbol_store, relation_store, observed_at_fn=...)`** is the
   single new entry point, in `lsp_enrichment.py`. Mirrors
   `BootstrapCoordinator`'s established injection shape exactly:
   `walk_repo: Callable[[str], Iterable[tuple[str, str]]]` (same
   `(path, source_text)` pairs `bootstrap.py` already uses — reused, not
   reinvented), `write_queue: WriteQueue` for writes,
   `process_registry: PyrightProcessRegistry` (D1) to obtain a live
   process. `symbol_store`/`relation_store` are **read-only** fresh-per-call
   instances the caller (eventually D6) opens against the repo's DB,
   matching `dispatch.py`'s established "fresh-per-call read path"
   convention (`write_queue.py`'s own docstring) — D3 never opens its own
   write connection directly; every actual write goes through
   `write_queue.submit(repo_id, job)`, one job per resolved relation,
   exactly `bootstrap.py`'s `_make_index_job` shape
   (`job(conn) -> RelationStore(conn=conn).upsert(relation)`).

2. **One pass = one bounded `LspClient` conversation, not a long-lived
   one.** `run_enrichment_pass` calls `process_registry.ensure_process
   (repo_root)`, wraps the result in a **fresh** `LspClient(process)`,
   calls `initialize(repo_root)` once, does all `didOpen`/`definition`/
   (`references`) work for this pass, then `close()`. `LspClient.close()`
   sends the real LSP `shutdown`/`exit` sequence, which makes the actual
   `basedpyright-langserver` child process exit itself (verified: D2's
   `close()` never touches `Popen` directly, but the LSP `exit`
   notification is what a real server acts on) — so the *next* enrichment
   pass's `ensure_process()` call correctly finds no live process (D1's
   `_live_process_for` already treats a dead entry as not-live and
   respawns) and gets a fresh client/process pair. This sidesteps
   `LspClient.initialize()`'s "may only be called once" constraint
   entirely (a fresh client is a fresh conversation) without touching D1
   or D2. `ensure_process` returning `None` (missing binary, spawn
   failure — D1's own degrade path) makes the whole pass a no-op: log
   once, return an empty result, matching D1/D2's "opportunistic,
   never load-bearing" north star (ARCHITECTURE.md).
   `# shortcut:` re-spawning the ~3GB-memory basedpyright process once per
   pass, rather than keeping one long-lived `LspClient` across passes
   (which would need real coordination with D1's idle-timeout and is D6's
   trigger-cadence concern, not D3's) — upgrade trigger: enrichment-pass
   spawn latency becomes a measured bottleneck once D6 wires a real
   trigger cadence.

3. **Discovering candidate sites — two disjoint categories, both
   read-only, both restricted to `calls`+`inherits`:**
   - **`AMBIGUOUS` sites**: already persisted. For each file,
     `relation_store.list_by_site_file(path, predicates={"calls",
     "inherits"})`, filtered in Python to `confidence == Confidence
     .AMBIGUOUS`. No new storage query needed.
   - **Unresolved sites**: *not* persisted anywhere today — confirmed by
     reading `indexer.py`'s `_resolve_deferred`: a `DeferredImportCall`/
     `DeferredImportInherit` whose candidate lookup returns zero matches
     "simply produces no edge... same as an undefined name" (its own
     docstring) — the deferred item itself is discarded the moment
     `index_file` returns, nothing records that the miss ever happened.
     D3 therefore re-derives them by re-running the exact same pure
     extraction pyright's already-committed pipeline uses:
     `extract_relations_with_deferred_edges(path, source_text,
     observed_at)` per file (from `walk_repo`'s own `(path, source_text)`
     pairs — no new file I/O seam needed), keeping only
     `deferred_calls`/`deferred_inherits` (never `deferred_overrides` —
     out of scope, see above), then classifying each against the
     **current** `symbol_store` via the new `indexer.
     unresolved_deferred_sites(deferred_calls, deferred_inherits,
     symbol_store) -> UnresolvedSites` helper. This is a small,
     behavior-preserving refactor of `indexer.py`: `_resolve_deferred`'s
     existing per-item candidate computation
     (`module_path_matches`-filtered `find_by_qualname_and_kind` lookup)
     is factored into a shared `_candidates_for(item, symbol_store, kind)`
     that both the existing resolver and the new classifier call — no
     duplicated logic (Coding Standard 16), no change to `index_file`'s
     own resolved-relation output (every existing indexer test must still
     pass unmodified). `unresolved_deferred_sites` returns items whose
     `_candidates_for` is empty — the same "genuinely no match" case
     `_resolve_deferred` already silently drops, just retained instead of
     discarded.
   Both categories converge into one worklist of `(source, site_file,
   site_line, site_col, predicate)` tuples before any LSP traffic starts —
   `AMBIGUOUS` rows already carry `source`/predicate directly;
   `DeferredImportCall`/`DeferredImportInherit` carry `source` and imply
   their own predicate (`calls`/`inherits` respectively, matching the
   caller that classified them).

4. **`SymbolStore.at_position(path, line, col) -> Symbol | None`** (new):
   the smallest-span live symbol at `path` whose `[start_line,start_col]`–
   `[end_line,end_col]` span contains `(line, col)`, using SQLite's
   row-value comparison (`(start_line, start_col) <= (?, ?) AND
   (end_line, end_col) >= (?, ?)`, supported since SQLite 3.15 —
   confirmed available via `sqlite3.sqlite_version` in this project's
   Python before relying on it), ordered by span size ascending
   (`end_line - start_line`, then `end_col - start_col`) so a method is
   preferred over its enclosing class, which is preferred over the module
   symbol, `LIMIT 1`. `extract_symbols.py`'s one-level nesting (module →
   class → method, no deeper — confirmed by its own loop structure, which
   only descends into a class body once) means no genuine tie is possible
   today; a final deterministic `id ASC` tie-break is added anyway as
   cheap insurance, not because a real case is known to need it. Named to
   parallel `at_start`'s existing convention, not to replace it —
   `at_start` keeps its own caller (`resolve.py`) unchanged.

5. **Response-to-target mapping (`lsp_enrichment.py`, pure-ish helper):**
   given a `textDocument/definition` result (verified live: a plain JSON
   array of `{"uri", "range": {"start": {...}}}` `Location` objects — never
   `LocationLink`, at least for this basedpyright version, from every
   real call tested this session):
   - **Zero or more-than-one `Location`** → skip this site entirely, write
     nothing. Pyright itself did not commit to a single answer — D3 never
     picks arbitrarily among several, and never fabricates certainty
     pyright's own response doesn't have. (This mirrors D3's read-only
     discovery step, which is likewise conservative: it only ever
     considers a site pyright *could* disambiguate, never invents a
     resolution ACIE alone couldn't verify.)
   - **Exactly one `Location`** → convert its `uri` to a path (`urllib
     .parse.urlparse` + `urllib.request.url2pathname` on the URI's `.path`
     — stdlib-only, no new dependency, mirroring D2's own "hand-roll it,
     zero new runtime deps" precedent) and take it relative to
     `repo_root`. A path that resolves *outside* `repo_root` (a `..`-
     prefixed relative path — i.e. the definition is in the stdlib, a
     third-party package, or anywhere ACIE doesn't index) is expected and
     common, not an error: skip silently, write nothing, leave the site's
     existing confidence exactly as it was.
   - A path inside `repo_root` is looked up via the new `symbol_store
     .at_position(relative_path, location.range.start.line + 1,
     location.range.start.character)` (`+1`: LSP lines are 0-indexed,
     ACIE's `start_line` is 1-indexed — same convention `extract_symbols.py`
     itself already uses in the other direction). `None` (file not yet
     indexed, or the position lands somewhere ACIE's tree-sitter extractor
     doesn't track, e.g. a nested/lambda scope) → skip, write nothing.
     A real `Symbol` → build the `Relation` (decision 6).

6. **The write**: `Relation(source=item.source, target=symbol.id,
   predicate=<"calls"|"inherits", from the worklist item>,
   site_file=item.site_file, site_line=item.site_line,
   site_col=item.site_col, confidence=Confidence.INFERRED,
   provenance=Provenance(provider=server_info["name"],
   version=server_info["version"], observed_at=observed_at_fn()))`,
   submitted as one `write_queue.submit(repo_id, job)` per resolved site
   (not batched into one giant transaction — matches `bootstrap.py`'s own
   one-job-per-unit granularity, and keeps a single bad write from
   blocking the rest of the pass). The pass does not block on each job's
   `Future` before issuing the next LSP request (write-queue jobs and LSP
   requests are two independent pipelines) but does wait for the last
   `Future` submitted before `run_enrichment_pass` returns, so a caller
   (D6, eventually) can know the pass — writes included — has actually
   drained, not just that LSP traffic finished.

7. **`didOpen` once per file per pass, `didChange`/`didClose` never sent.**
   D3 opens a `textDocument/didOpen` (with the file's full current text,
   `languageId: "python"`, a constant `version: 1` — never revised within
   a pass since D3 never edits) for every distinct `site_file` the
   worklist touches, before issuing any `definition` request against that
   file, and tracks already-opened URIs in a per-pass `set()` so a file
   with multiple candidate sites is opened exactly once. No `didClose` is
   sent for any file — the whole conversation (and, per decision 2, the
   whole server process) tears down together at `close()`; there is
   nothing a per-file `didClose` would additionally clean up here.

8. **Per-request failure handling — every site is independent.** Each
   `send_request("textDocument/definition", ...)`'s `.result(timeout=...)`
   call is wrapped individually: `LspError` / `TimeoutError` /
   `ConnectionError` (D2 decision 9's deliberately-propagated failure
   vocabulary) are caught right here, logged at `WARNING` naming the site,
   and treated as "skip this one site" — never as a reason to abort the
   rest of the pass. A `ConnectionError` specifically (the reader thread
   observed EOF, per D2 decision 8 — i.e. the whole server process died
   mid-pass) is the one exception: once observed, the pass stops issuing
   further requests immediately (every subsequent site would fail
   identically) but still returns normally with whatever was resolved so
   far, rather than raising out of `run_enrichment_pass` — matching D1/D2's
   shared "opportunistic enrichment is never load-bearing; a failure here
   must never surface as a caller-visible daemon error" principle.

9. **Capability guard.** Before issuing any `didOpen`/`definition` traffic,
   check `"definitionProvider" in (client.server_capabilities or {})` (a
   truthy value — this session's live `initialize()` returned `true`,
   matching the D2 review's own five-capability confirmation). A server
   that doesn't advertise it (a future/alternate LSP backend, or a
   degraded `basedpyright` build) makes the whole pass a same-as-D1's-
   missing-binary no-op: log once, `close()`, return an empty result — no
   attempt to query anyway and swallow whatever error results.

10. **Serial, not pipelined.** One `definition` request in flight at a
    time — issue, block on its `Future`, handle the result, move to the
    next site. D2's design supports concurrent in-flight requests (`_pending`
    is keyed by request id, resolved independently of issue order), so
    pipelining is possible, but v1 keeps this simple and deterministic.
    `# shortcut:` serial requests — upgrade trigger: measured enrichment-
    pass wall-clock time becomes a real problem for a large repo's full
    unresolved+`AMBIGUOUS` site count, at which point bound a small
    concurrent-request pool rather than fully serializing.

11. **`textDocument/references` is not used by v1's resolution path.**
    Live-verified this session that it works end-to-end against real
    `basedpyright` (a real call returned 4 real locations) and returns the
    same `list[Location]` shape as `definition` — so it's available and
    proven, not just assumed — but resolving an *outbound* `calls`/
    `inherits` edge is a go-to-definition question, not a find-usages one;
    nothing in this pass's design needs it. Documented here (rather than
    silently omitted) because the locked breakdown's own wording names
    both requests as available traffic D3 issues — v1 issues only
    `definition`; `references` is confirmed reachable for whichever future
    slice needs it (e.g. a reverse-direction enrichment, out of scope
    here) without re-verifying the request/response contract from
    scratch.

12. **Closes D2's carried-forward smoke-test obligation** (memory
    `f5f0d909`, reconfirmed still open as of the D2 review `4201c082`):
    live-verified this session, with real `didOpen` against 3 real files
    plus real `definition` and `references` requests (not just
    `initialize()`), instrumenting `LspClient._dispatch_message` directly
    to observe every server-initiated request. **Zero** server-initiated
    requests occurred; observed notifications were exactly
    `window/logMessage`, `pyright/beginProgress`, `pyright/reportProgress`,
    `textDocument/publishDiagnostics`, `pyright/endProgress` — all already
    handled by D2's existing "notification: log DEBUG, drop" branch. This
    is now three independent sessions (D2's own smoke test, the D2 review's
    repeat, and this one) confirming basedpyright never sends a
    server-initiated request under default `initialize` capabilities, even
    under real document-open/query traffic — D2's generic empty-result
    auto-reply (its own named `# shortcut:`) remains formally unexercised
    by real production traffic, but is no longer an unverified assumption
    about *whether* it would ever fire; it is now a confirmed-idle safety
    net. No code change follows from this — it's a closed verification
    item, recorded here so a future session doesn't reopen it a fourth
    time.

13. **`observed_at_fn`** is injected (defaulting to
    `lambda: datetime.now(timezone.utc).isoformat()`, matching
    `bootstrap.py`'s own inline convention) rather than hardcoded, purely
    for test determinism — no other reason; every other D3 seam
    (`walk_repo`, `process_registry`, `write_queue`) is injected because
    `bootstrap.py`'s and D1's own established patterns already demand it
    for production-vs-test wiring, not because D3 invents a new
    convention.

14. **No `ARCHITECTURE.md` change** (no MCP tool surface, no schema
    change to `relations_live`/`symbols_live` — `at_position` is a new
    *query*, not a new column). `DAEMON.md`'s existing "LSP/pyright
    Enrichment Subprocess Lifecycle" section gets one new paragraph (after
    D2's), documenting `run_enrichment_pass` the same way D1/D2's own
    paragraphs describe their modules — same "extend in place" convention
    C2–C6 and D1/D2 already used, not a new top-level section.

## Files to touch

1. `src/acie/daemon/lsp_enrichment.py` (new) — `run_enrichment_pass`,
   the site-worklist builder, the response-to-target mapper, the URI→path
   helper.
2. `src/acie/storage/symbol_store.py` — add `at_position()`.
3. `src/acie/indexer.py` — factor `_candidates_for` out of
   `_resolve_deferred`; add public `unresolved_deferred_sites()` and its
   small `UnresolvedSites` result type (calls-list + inherits-list).
4. `tests/storage/test_symbol_store.py` — new `at_position` tests:
   exact single-symbol containment, method-preferred-over-class nesting,
   module-fallback when no def/class contains the point, no-match path.
5. `tests/test_indexer.py` (or wherever `_resolve_deferred`'s behavior is
   already covered) — new tests asserting `unresolved_deferred_sites`
   returns exactly the zero-candidate items, and that `index_file`'s own
   resolved-relation output is byte-identical to before the refactor for
   every existing fixture.
6. `tests/daemon/test_lsp_enrichment.py` (new) — the bulk of the new
   coverage, all against an injected fake conforming to `LspClient`'s
   public shape (`send_request`/`send_notification`/`server_capabilities`,
   pre-programmed `Future` results) rather than a real subprocess: covers
   the `AMBIGUOUS`-only case, the unresolved-only case, the "pyright
   returns 0/2+ Locations → skip" case, the "target outside repo_root →
   skip" case, the "target position has no ACIE symbol → skip" case, the
   missing-binary/no-`definitionProvider` no-op cases, the
   `ConnectionError`-stops-the-pass-but-returns-cleanly case, and a
   provenance-stamping assertion (`provider`/`version` taken from the
   injected `initialize()` result's `serverInfo`, not hardcoded).
7. `DAEMON.md` — one new paragraph.

## Workflow constraints carried into the spec

- One slice per session, stop before commit for review (memory `9b020543`).
- Full suite must pass except the 3 known pre-existing failures (baseline
  reconfirmed this session: 692 passed, 3 failed).
- No new runtime dependencies — URI→path conversion is stdlib
  (`urllib.parse`/`urllib.request`), matching D2's zero-new-deps precedent.
- Do not touch `runtime.py`/`dispatch.py`/`mcp_server.py`/`bootstrap.py` —
  that daemon-trigger wiring is D6's job, not D3's.
- Do not implement D4's stale-sibling-cleanup/merge-policy logic (see
  "What D3 does not do" above) — leave it for D4.
- Standing dogfooding ground rule (wayfinder map `5d8fa498`'s Notes) still
  applies: live `mcp__acie__*` tools stay v0-pinned throughout.

## Verification note

This spec was written against the actual current D1 (`pyright_process.py`)
and D2 (`lsp_protocol.py`, `lsp_client.py`) source, `write_queue.py`,
`bootstrap.py`, `indexer.py`, `extract_relations.py`, `extract_symbols.py`,
`resolve.py`, `symbol_store.py`, and `relation_store.py` (all read directly
this session), plus three separate live smoke tests run for real against
this repo's own `.venv/basedpyright` (not simulated, not trusted from
memory or LSP-spec docs alone):

1. A real `initialize()` call, printing the *full* raw result — confirming
   `serverInfo: {"name": "basedpyright", "version": "1.39.10"}` is present
   and resolves the provenance-version open question.
2. A real `didOpen` + `textDocument/definition` call against a genuine
   call site in this repo (`indexer.py`'s call to `extract_symbols`) —
   confirming the response shape (`list[Location]`) and, critically, that
   the returned position (name-token column 4) does **not** match
   `at_start()`'s assumption (keyword-node column 0), motivating the new
   `at_position()` containment query.
3. A real `didOpen` (3 files) + `definition` + `references` sequence with
   `LspClient._dispatch_message` instrumented to observe every message
   shape — confirming zero server-initiated requests occurred, closing
   `f5f0d909`'s carried-forward obligation.
