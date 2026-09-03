# ACIE v0 Architecture Specification

This document consolidates a structured, decision-by-decision planning process into a single reference. Each section below reflects a locked architectural decision; where something was deliberately left open, it is called out under "Not Yet Specified" rather than resolved here.

## Overview

ACIE ("Agent Code Intelligence Engine") is a standalone, deterministic, repository-aware code-intelligence MCP (Model Context Protocol) server for AI coding agents (Claude Code, Codex, and other MCP-capable agents). It gives an AI coding agent structural understanding of a codebase — symbol lookup, definitions, references, imports, structural (AST-pattern) search, dependency/call graphs, and impact/blast-radius analysis — without relying on embeddings or an LLM for its core facts.

ACIE is completely standalone and must never depend on SALTMDB, a separate, unrelated persistent-agent-memory project. SALTMDB is persistent, non-reconstructable agent memory: things an agent decided or was told, which can't be re-derived from source. ACIE is deterministic, reconstructable intelligence derived from source code itself — delete ACIE's index and re-run it against the same source tree, and you get the same answer back. That is the core distinction. An agent may use both projects independently; neither project's code knows about the other.

## Design Principles

- **Determinism is the north star.** Tree-sitter's AST-based parsing is the deterministic baseline and must work completely standalone with zero optional dependencies. LSP (Language Server Protocol) output is always an opportunistic, versioned, cache-stamped enrichment layer on top of the tree-sitter baseline — it is never a correctness dependency for the core graph. If no LSP server is available, ACIE still works, just with less precision on semantic (as opposed to purely structural) facts.
- **No embeddings or LLM calls are required for core functionality**, and they should be avoided wherever possible. "Structural search" specifically means AST-pattern search using tree-sitter's own native `.scm` query language — not semantic/fuzzy/embedding-based search, and not ast-grep (a different, separately-versioned tool) — to avoid maintaining a second independently-versioned parser per language.
- **Exactly one ACIE daemon runs per computer.** It owns the write queue(s) — one dedicated writer thread and queue per repo, see `DAEMON.md` — and all `.acie`/`~/.acie` on-disk state. Every agent-spawned MCP server process is a client of this one daemon, never a direct writer to the SQLite files. This mirrors the daemon pattern already used by the separate, unrelated SALTMDB project on this machine — cited as prior art/precedent only, not a dependency.
- **State layout**: `<repo>/.acie/config.json` is user-owned, hand-editable, committable configuration (language overrides, ignore rules). `~/.acie/repos/<repo-id>/{index.sqlite, manifest.json, cache/}` is derived/generated state — one SQLite file per repository, never a single shared global database — keyed by a canonical repo identity resolved from the repo's `.git` common directory, so that multiple git worktrees of the same repository resolve to the same `<repo-id>` instead of duplicating state or colliding.
- **Hook installation is opt-in and composable.** Whether installing git-side hooks or agent-side tool-use hooks, ACIE must never silently overwrite a user's existing hook tooling (e.g. husky, pre-commit, lefthook).
- ACIE's unified-IR-with-field-level-provenance design (see "Provenance & Confidence Semantics") is genuinely novel — no surveyed code-intelligence tool does exactly this (see "Prior Art Surveyed").

## Scope: v0 / v1 / Later

**v0 (MVP)** ships a tree-sitter-only baseline, deliberately sidestepping the determinism-vs-LSP tension entirely until the graph works end-to-end with zero LSP dependency:

- Symbol lookup, get-definition, find-references, list-imports
- AST-pattern structural search (native tree-sitter `.scm` queries)
- Dependency graph (file/module level)
- Call graph (tree-sitter baseline precision)
- Basic impact / blast-radius analysis

**v1** adds: LSP-backed precise cross-file resolution, inheritance/implementation relationships, affected-test discovery, architecture-level queries.

**Later** (explicitly deferred, not yet designed): revision-graph diffing between Git commits (tracking a symbol's identity continuity across renames/moves over time), additional language adapters beyond Python, framework-specific entities (e.g. HTTP endpoint detection).

**v0 source language: Python only.** A pair of languages (one nominally-typed, one structurally-typed) was considered to stress-test the IR design early, but this was scoped down to Python-only for v0 to simplify the LSP server surface to one family (pyright/pylsp) and ship an end-to-end working graph faster. Multi-language support is deferred to a not-yet-designed language-adapter plugin interface (see "Not Yet Specified").

**v0 core implementation language: Python.** Chosen over Rust/Go/TypeScript for contributor-tooling parity with the separate SALTMDB daemon already in this workspace (also Python/venv-based), and because LSP's JSON-RPC wire protocol is language-agnostic anyway, so there's no technical reason ACIE's own daemon needs to be in a systems language. `py-tree-sitter` plus `pyright`/`pylsp` cover v0's needs. Trade-off accepted: this gives up some of what a Rust implementation would offer (single-binary distribution, raw performance).

**v0 agent-hook integration scope**: only Claude Code and Codex (see "Agent Hook Integration") — Cursor was surveyed but dropped from v0 because the user has no Cursor subscription to validate against; Agy (Antigravity) was deferred because its edit-tool hook schema is unverified. This narrows, but does not contradict, ACIE's general target-audience framing ("Claude Code, Codex, and other MCP-capable agents") — that framing describes ACIE's overall audience, v0's hook-integration scope is just narrower for now.

## System Architecture

Exactly one daemon process runs per computer, single write queue, sole owner of all `.acie`/`~/.acie` state. Agent-spawned MCP server processes are thin clients to this daemon — they never touch the SQLite files directly. This avoids naive multi-process SQLite write contention/corruption, and avoids piling up N separate per-repo daemon processes on a machine that works across many repos.

On-disk layout:

```
<repo>/.acie/config.json               # in-repo, user-owned, committable config
~/.acie/repos/<repo-id>/index.sqlite   # one SQLite file per repo (derived state)
~/.acie/repos/<repo-id>/manifest.json  # per-repo manifest (schema/IR version tracking)
~/.acie/repos/<repo-id>/cache/         # per-repo cache directory
```

`<repo-id>` is derived from the repo's `.git` common directory, so that git worktrees of one logical repository share one `<repo-id>` rather than duplicating or colliding.

Rejected alternatives: a pure in-repo `.acie/index.sqlite` (risk of accidental commits, breaks on read-only checkouts/CI); a single global SQLite database shared across every repo the daemon serves (fragile single point of failure, blast radius spans every repo).

## Daemon & MCP-Server Design

The detailed design for how the daemon and each agent-spawned MCP-server process actually talk to each other — process lifecycle/auto-spawn, IPC transport and wire framing, the RPC request/response envelope, dispatch of the 9 MCP tools, repo/session identity, bootstrap-indexing behavior, per-repo write-queue concurrency, the auth-token stance, shutdown semantics, and the CLI subcommand surface — is locked in **[`DAEMON.md`](./DAEMON.md)**, not restated here. That document assumes every principle above (one daemon per computer, this on-disk layout, the 4-tier indexing precedence below, the `notify-hook` contract) rather than re-deciding it.

## Canonical IR / Data Model

**Symbol-ID grammar**:

```
<repo-relative-path>:<dotted.qualname>#<kind-tag>@<ordinal>
```

- No `@ordinal` suffix is present on a symbol's first/only definition — `@2`, `@3`, etc. are appended only when an actual naming collision occurs (e.g. two definitions at the same qualname due to redefinition/overloading), disambiguated by definition order within scope.
- v0 kind-tag vocabulary (Python-only): `module | class | function | method | variable`.
- The delimiter characters `:`, `#`, `@` were chosen specifically because they cannot appear in a Python dotted-qualname or in a POSIX-style repo-relative path. A path or qualname that happens to contain one of these three characters is an explicit, accepted out-of-scope limitation for v0, not handled by any escape-grammar mechanism.
- This ID format is deliberately visually distinct from SALTMDB's UUIDs and from git SHAs, so an ID's origin is unambiguous at a glance.
- IDs are opaque, human-readable strings — not hashed.
- Every MCP response carries an index-generation stamp, so a stale symbol/edge ID from before a reindex produces an explicit error rather than being silently re-resolved to a different (possibly wrong) current symbol.

**Storage architecture**: two database tables per entity kind (Symbols and Relations each get this treatment) — one live table that is UPDATE-in-place on every re-observation, plus one append-only history table that is INSERT-only. The single-table-with-a-flag alternative (marking old rows as historical via a flag column) was explicitly rejected because flipping a flag on an existing row is itself a mutation, which breaks append-only history purity.

- History rows are appended only when something actually changes on reindex (not on every reparse), to bound table growth.
- A deletion (e.g. a symbol removed from source) hard-deletes the corresponding live-table row and writes a tombstone observation into the history table.
- No source code text is stored in the IR — only spans (file plus line/column ranges).

**Provenance/confidence columns**: normalized SQL columns everywhere (e.g. a `confidence` column constrained to an enum of allowed values) — explicitly not a JSON blob column — on both Symbols and Relations. See "Provenance & Confidence Semantics" for the exact taxonomy.

**Relations use an extended composite primary key**:

```
(source, target, predicate, site_file, site_line, site_col)
```

This is wider than an originally-considered simpler `(source, target, predicate)` triple, specifically so that multiple distinct call sites between the same two symbols (e.g. function A calls function B from three different lines) each get their own live row, rather than collapsing into one row and making `find_references` lossy.

**v0 predicate vocabulary** (the relation "kinds" ACIE tracks): `imports | calls | references | defines | inherits`.

*(v1, in progress, adds `overrides` -- source = the overriding method, target = its immediate overridden base method; EXTRACTED when unambiguous, AMBIGUOUS when more than one immediate-base candidate exists. Locked in the v1 design-spec wayfinder map, not restated here since this section documents v0's own historically-locked vocabulary; see that map's ticket resolution for the full rationale.)*

Exact column-level DDL/type details beyond what's stated above were worked out during the design process but are not fully reproduced here — exact DDL is to be finalized during implementation against this design, not invented for this document.

## Symbol Identity & Reconciliation

Question this decision answers: what makes a symbol observed in one reindex "the same" symbol observed in the next reindex, across renames, file moves, etc.?

**Chosen model: pure deterministic recompute** (the same approach SCIP/Sourcegraph uses) — a symbol's ID is a function of only its current source position and qualified name at each reindex. There is no persistent surrogate UUID assigned to a symbol, and no cross-reindex fuzzy-matching heuristic.

A rename or a file move is, at the identity level, simply: delete the old symbol ID, create a new symbol ID. ACIE v0 makes no attempt to track "this is the same symbol, just renamed."

**Explicitly rejected alternative**: a persistent surrogate ID combined with a fuzzy-matching pass (using techniques like git's rename detection, function-body-hash similarity, or position-continuity heuristics). This was rejected for two reasons: it reopens exactly the same confidence/fuzziness problem that the prior-art research found no surveyed code-intelligence tool solves well (see "Prior Art Surveyed"); and full identity continuity across history duplicates work that the already-deferred "revision graph diffing between Git commits" later-scope capability will need to do properly anyway — building a half-version of it now would be wasted/conflicting effort.

This identity model was explicitly user-flagged as "changeable later" but locked as v0's working answer.

Redefinitions/overloads at the same qualname are disambiguated by the `@ordinal` suffix described in "Canonical IR / Data Model" above, assigned by definition order within scope, appended only on an actual collision.

## Provenance & Confidence Semantics

Question this decision answers: how does ACIE represent how sure it is about a given fact (a symbol or a relation/edge), and which tool/version produced it?

**Confidence taxonomy**: a single shared 3-value categorical enum used identically on both Symbols and Relations (not separate taxonomies per entity type, not an ordinal low/medium/high/very-high scale like CodeQL's, and explicitly not a numeric 0.0–1.0 float score — a float was rejected because ACIE has no calibrated-probability source to back it; a float would just be hardcoded constants dressed up as a probability).

- `EXTRACTED` — tree-sitter parsed this fact with no ambiguity at all: a normal definition, an explicit import statement, a direct call by name.
- `INFERRED` — a semantic/indirection-following resolution that could plausibly be wrong (e.g. an LSP "go to definition" that resolves through an alias or re-export). This value is reserved but unused in v0 — it is present in the schema from day one specifically so that v1 (which adds LSP) is an additive change to the schema, not a migration.
- `AMBIGUOUS` — multiple candidate targets exist and tree-sitter alone cannot disambiguate between them. This is producible by tree-sitter alone starting from v0 day one (does not require LSP).

**Provenance is a rich struct**, not a bare enum/string: `{provider, version, observed_at}` — recording which tool produced the fact (e.g. "tree-sitter"), that tool's version, and when the observation was made.

**Multi-provider reconciliation**: when multiple providers (e.g. tree-sitter and, later, an LSP server) observe the same real-world fact, ACIE stores it as replace-in-place — there is exactly one live edge/symbol row per real reference site at any time, with its confidence/provenance upgraded in place as better evidence becomes available. The full observation history (every prior observation of that same fact, including from lower-confidence providers) is retained in the append-only history table and is queryable on demand — it is never surfaced as duplicate parallel "live" edges.

**Ambiguous multi-target relations**: when a single reference site has multiple candidate targets that can't be disambiguated, ACIE creates one edge per candidate target (each tagged `AMBIGUOUS`) rather than a single edge carrying a list of candidates.

**Query-time exposure**: the terse/default response mode from MCP tools hides confidence and provenance fields entirely. Passing `full=true` on a query reveals confidence/provenance per returned item. Default filtering behavior is unrestricted — i.e. `AMBIGUOUS` facts are not silently excluded by default. For confidence-sensitive aggregate counting (e.g. blast-radius / impact analysis), rather than silently filtering by confidence, ACIE instead reports counts broken out by confidence tier (e.g. "12 EXTRACTED, 3 AMBIGUOUS affected symbols" rather than a single blended number).

**`min_confidence` filter** (implemented 2026-09-02, `src/acie/tools/confidence.py`): an optional parameter on `find_symbol`, `get_definition`, and `find_references` only — the 3 tools whose results carry genuinely graded per-item confidence. `list_imports`/`structural_search` don't accept it (their results are always `EXTRACTED`, so it would be a documented no-op); `graph`/`impact_analysis`/`explain` don't accept it either (mid-traversal filtering on `graph` could silently disconnect reachable nodes, `impact_analysis` already reports its own tier-broken-out `impact_summary` instead of filtering, and `explain`'s whole point is showing every observation regardless of confidence). Because the taxonomy above is deliberately non-ordinal but a "min_confidence" filter parameter, by its name, implies an ordering, this filter defines its own filter-only rank — `EXTRACTED` (most certain) `< INFERRED < AMBIGUOUS` (least certain) — distinct from the taxonomy's own semantics: `min_confidence=X` keeps only results at least as certain as `X`. In v0 practice this is close to a binary EXTRACTED-only/everything switch, since `INFERRED` is unproduced until v1's LSP layer.

## Incremental Indexing

Question this decision answers: what triggers ACIE to re-index changed files, and in what order/precedence?

Layered, cheapest-and-most-reliable-first, each tier a strict fallback for what the tier above might miss:

1. **Filesystem watcher (the backbone)** — a daemon-managed OS-level filesystem watcher (inotify on Linux / FSEvents on macOS / ReadDirectoryChangesW on Windows, via Python's `watchdog` library) watches each actively-indexed repo's tree, respecting the repo's ignore rules (a shared, `pathspec`-based predicate that composes every `.gitignore` in the repo — root plus nested, with real git precedence — used identically by bootstrap's own walk so the two never disagree about scope), with a ~500ms debounce/coalescing window collapsing a burst of rapid successive events for the same file into one reindex. This catches every change regardless of what tool produced it, with zero cooperation required from any editor/agent/tool — it is the mechanism that makes ACIE work correctly even with no other integration installed at all. Staleness is detected via a hybrid mtime-then-hash check: a file's on-disk modification time is compared first (cheap, no disk read), and its content is only hashed and compared when the mtime differs from what's recorded — an mtime-only touch with unchanged content (e.g. a save that rewrites identical bytes) updates the recorded mtime but never triggers a reindex.
2. **Git hooks** (`post-commit`, `post-merge`, `post-checkout`, `post-rewrite`) — rather than trust each hook type's own (inconsistent, sometimes absent) old/new-SHA calling convention, the daemon tracks its own `last_indexed_head_sha` per repo and diffs that against the repo's current `HEAD` itself on every git-hook notification, so all four hook types funnel through one uniform mechanism and the hook script the user installs needs zero argument parsing.
3. **Agent tool-use hooks** — an optional low-latency accelerant (see "Agent Hook Integration") that notifies the daemon of the exact file an AI coding agent just edited, the instant the edit happens, rather than waiting for the filesystem watcher's debounce window.
4. **Lazy staleness check at query time** — the final safety-net fallback: if a query touches a file whose on-disk state doesn't match what's indexed (and none of tiers 1–3 caught it), the daemon detects and corrects this at query time. Implemented 2026-09-02 (`src/acie/daemon/staleness.py`, `runtime.py`'s `ensure_fresh`), scoped to the 3 tools that name exactly one file cheaply: `list_imports(file=...)` and `get_definition`/`find_references` when called with `position={file, ...}` (not `symbol_id`, which names no file). `find_symbol`/`graph`/`impact_analysis`/`explain` aren't covered (no single file named up front, or — for `explain` — staleness is beside the point since it shows history on purpose); `structural_search` needs no tier-4 check at all since it already reads its `files` mapping live off disk on every call. The check reuses tier 1's own `make_reindex_job` (same hybrid mtime-then-hash logic), runs synchronously via the repo's write queue bounded to a 2-second timeout, and never fails or delays the query past that budget — on timeout or any error it logs a warning and answers with whatever the index currently has. See "Incremental Indexing Wiring" in `DAEMON.md`.

As stated in Design Principles: hook installation (whether git-side or agent-side) must be opt-in and composable, and must never silently overwrite a user's existing hook tooling (husky, pre-commit, lefthook, etc.).

## Agent Hook Integration

Question this decision answers: given that Claude Code and Codex both expose some form of tool-use hook mechanism, how should ACIE actually integrate with each one as the tier-3 indexing accelerant described above?

**Agent hook survey findings**: Claude Code has a first-class, mature native "hooks" feature (configured in `settings.json`, with events like `PreToolUse`/`PostToolUse`, tool-name matchers, and a JSON payload on stdin including the exact file path with zero parsing needed). Codex also has a hook mechanism, but it delivers a diff-header format that needs parsing to extract the changed file path(s), rather than handing over a clean file path directly. Cursor and Agy were also surveyed for their hook mechanisms but are out of v0's hook-integration scope (see "Scope" and "Out of Scope").

**Chosen integration contract shape**: a single CLI subcommand, `acie notify-hook --agent <name>`, is the sole public integration contract between an agent's hook system and ACIE. The internal daemon IPC/wire format behind this subcommand stays a private, freely-changeable implementation detail.

- If the ACIE daemon is not installed, not running, or unreachable, the `notify-hook` call silently no-ops with exit code 0 — a hook integration must never break or delay the agent's own tool-use flow, under any failure condition.
- This is explicitly exactly "tier 3" of the already-locked reindex-trigger precedence described in "Incremental Indexing" — it is an accelerant layered on top of the filesystem-watcher backbone, not a new or competing indexing mechanism. Non-agent edits (e.g. a human editing in a plain text editor, or a script) still rely solely on the watcher.

**Payload handling — the "smart" design**: `notify-hook` reads the calling agent's raw hook payload on stdin and does all agent-specific field-extraction logic internally, inside ACIE's own testable, versioned Python code (a JSON field pull for Claude Code; diff-header parsing for Codex) — rather than pushing fragile per-agent parsing logic (e.g. a `jq` one-liner) out into each user's own hook configuration file, where it would silently rot whenever an upstream agent's hook payload format changes. A "dumb" alternative design (`acie notify <path>`, where the user's own hook config would be responsible for extracting the path) was explicitly considered and rejected for exactly this fragility reason, especially given how easily Codex's diff-header format could change upstream.

**Fire-and-forget with a hard timeout**: the `notify-hook` call enqueues its notification with the daemon and returns immediately, using a roughly 200ms client-side timeout — this is because Claude Code's `PostToolUse` hook blocks the agent's current turn until the hook process exits, so `notify-hook` must never be slow.

**v0 shipping scope**: v0 ships documentation-only copy-paste hook configuration snippets for Claude Code and Codex (e.g., an example Claude Code `PostToolUse` `settings.json` snippet). There is deliberately no automated hook installer command in v0 — that is intentionally deferred pending real usage/dogfeeding data (see "Not Yet Specified"). Cursor is dropped entirely from v0 (no subscription available to validate against); Agy/Antigravity support is deferred pending verification of its edit-tool hook payload schema.

## MCP Tool Surface

ACIE's v0 MCP server exposes 8 tools total (a locked 7-tool MVP surface, plus 1 additional `explain` tool added afterward to cover observation-history retrieval); v1 slice B1 (wayfinder ticket df13991a) adds a 9th, `affected_tests`. Every tool response uses a shared response envelope: `{index_generation, results, total_count, truncated, next_cursor}`. `next_cursor` is an opaque string the caller round-trips as the `cursor` input to fetch the next page; it is `null`/absent once `truncated` is `false`. Passing `full=true` on any tool reveals confidence/provenance fields per result item (see "Provenance & Confidence Semantics"); the default (terse) response omits them.

```
find_symbol(name, kind?, path_glob?)                          + limit, cursor, full
get_definition(symbol_id | position: {file, line, column})    + limit, cursor, full
find_references(symbol_id | position: {file, line, column})   + limit, cursor, full
list_imports(file)                                             + limit, cursor, full
structural_search(pattern, path_glob?)                          + limit, cursor, full
graph(root: symbol_id, graph_type, direction)
impact_analysis(root: symbol_id, ...)
explain(target: symbol_id | edge_ref)                           + limit, cursor
affected_tests(root: symbol_id, ...)
```

| Tool | Notes |
|---|---|
| `find_symbol` | `name` is a required substring match; `kind` is an optional enum filter; `path_glob` optionally scopes the search to a subset of files. Accepts `min_confidence` (see "Provenance & Confidence Semantics"). |
| `get_definition` | `symbol_id` or a file position — mutually exclusive. Accepts `min_confidence`. Calling by `position` also triggers a tier-4 lazy staleness check on that file (see "Incremental Indexing"). |
| `find_references` | Same mutually-exclusive shape as `get_definition`, including `min_confidence` and the same tier-4 check when called by `position`. |
| `list_imports` | `file` required. Import edges are always `EXTRACTED` confidence (tree-sitter parses imports completely unambiguously), so `full=true` is effectively a no-op here (and `min_confidence` isn't accepted, for the same reason). Every call triggers a tier-4 lazy staleness check on `file` (see "Incremental Indexing"). |
| `structural_search` | `pattern` is a required tree-sitter-native `.scm` query string — explicitly not an ast-grep pattern (see "Design Principles"). Returns `INVALID_PATTERN` if the pattern doesn't parse. |
| `graph` | Unifies the dependency graph and call graph capabilities into one tool via `graph_type`, with a uniform `root: symbol_id` anchor and `direction: upstream \| downstream`. |
| `impact_analysis` | Kept separate from `graph` because blast-radius analysis spans both dependency and call edges simultaneously and doesn't fit cleanly into one `graph_type`. Returns both a capped list of affected symbols and a tier-broken-out `impact_summary` count. |
| `explain` | Added specifically to answer "explain this edge/symbol": shows a symbol's or relation's full multi-provider observation history. `edge_ref` is a composite key `{source_symbol_id, target_symbol_id, predicate, site_file, site_line, site_col}` — the full `RelationStore` primary key, not a bare 3-tuple; a bare `{source, target, predicate}` triple is ambiguous whenever two or more distinct call sites connect the same pair of symbols, which the extended relation PK exists specifically to keep distinct (see "Canonical IR / Data Model"). No separate opaque edge-ID scheme — edges are already addressable this way in every other tool's output (`render_relation`'s shape). `results` is a flat, newest-first array of self-contained observation snapshots (location/span, confidence, provenance `{provider, version, observed_at}` — unlike every other tool, `explain` reveals confidence/provenance unconditionally, even at the default `full=false`, because they're the substance of what the tool exists to show rather than a secondary annotation; `full` is still accepted for interface consistency with the rest of the tool surface but has no effect on `explain`'s output), no server-side diffing between entries — entry zero is always the current live fact. A target with no revisions yet still returns a single-entry array. A target that has since been deleted (tombstoned, no live row) still returns its full history rather than erroring `SYMBOL_NOT_FOUND`/`EDGE_NOT_FOUND` — entry zero becomes the most recent pre-deletion content snapshot, tagged `deleted: true` (older entries don't carry this key). Uses real opaque keyset cursor pagination, same as the other flat-list tools (not the graph tools' node-cap/depth-clamp shortcut). `index_generation` is still reported for shape-consistency, but `explain` never errors on staleness — its entire purpose is showing history across index generations, so unlike every other flat-list tool it never raises `STALE_INDEX_GENERATION` even if the index has moved on since a cursor was issued. |
| `affected_tests` | v1 slice B1 (wayfinder ticket df13991a): static call-graph reachability from `root` to pytest-convention test functions/methods, answering "which tests cover this symbol". Own node-cap/depth-clamp BFS, independent of `graph`/`impact_analysis`'s internals (same "wait for a real 2nd caller" norm). Follows a fixed, narrower predicate set than `impact_analysis`: `{calls, overrides}` only (no `imports` — a test exercises a symbol by calling or overriding it, not by importing it). No new predicate/table for test-to-symbol coverage — test identification is a query-time pattern match against the pytest convention (`test_*.py`/`*_test.py` file paths, `test_*` function/method qualnames), never presented as ground truth. Returns `{index_generation, root, affected_tests, test_summary, node_cap, depth_clamp, truncated}` — `affected_tests` carries only the test-identified nodes discovered (each with an unconditional `discovery_predicate` field, same rationale as `impact_analysis`'s), while non-test intermediate callers are still traversed through (and still count against `node_cap`) so a test reachable only via helper functions is still found. `test_summary` breaks out the same confidence-tier counts as `impact_analysis`'s `impact_summary`, surfacing AMBIGUOUS-confidence reachability (e.g. through a dynamically dispatched call) rather than hiding it. Real coverage-data ingestion (`.coverage`/`coverage.json`) and a configurable test-glob override in `.acie/config.json` are later upgrades, not v1's bar. **v1 slice B2** adds a pytest-fixture dependency-injection heuristic (`src/acie/adapters/python/extract_relations.py`): a test function's (or another fixture's) parameter matching a same-file `@pytest.fixture`-decorated function's public name synthesizes an AMBIGUOUS-confidence `calls` edge, `provenance.provider="pytest-fixture-heuristic"` — pytest's implicit by-name injection is never a real call site tree-sitter's ordinary call-extraction can see. Always AMBIGUOUS, never `EXTRACTED`, even for a single unambiguous same-file match, since this is a naming-convention heuristic, not a resolved reference. Only mandatory (no-default) parameters are matched, mirroring pytest's own `getfuncargnames` — a defaulted parameter is never a fixture request. Candidate resolution respects real class-vs-module fixture scoping (verified against a live pytest run, code review 2026-09-03): a class-level fixture shadows a same-named module-level one for its own class's members, and is never visible to a sibling class. `@pytest.fixture(name="...")` overrides the registered name (verified against pytest's own source) — the underlying function's own name stops matching once renamed. Same-file only for v1 — a fixture defined in a `conftest.py` ancestor directory (pytest's own primary fixture-discovery mechanism, never an explicit import ACIE could defer against) is not yet resolved; a cross-file version would need a persistent repo-wide fixture registry, not merely an indexer.py resolution pass, and is left as a flagged follow-up. |

Cross-cutting rules for the whole tool surface:

- **Pagination**: flat-list tools (`find_symbol`, `get_definition`, `find_references`, `list_imports`, `structural_search`, `explain`) use real opaque keyset `cursor` pagination. (Exception on the terse/full rule: `explain` always reveals confidence/provenance regardless of `full` — see its row above.) — explicitly not offset-based pagination in disguise. The envelope's `next_cursor` (opaque, base64-encoded `[index_generation, last_result_id]` in the reference implementation) is what a caller passes back as `cursor` to fetch the next page. Pagination pins to the `index_generation` seen on page 1; if the index changes generation mid-pagination, subsequent pages error with `STALE_INDEX_GENERATION` rather than silently blending results across two different index generations. Each tool orders its results by its own deterministic keyset key (`find_symbol` orders by symbol ID, ascending — later tools pick whatever key is natural for their result type, e.g. site location for `find_references`); the key need not be uniform across tools, but must be stable and total (no ties) so keyset pagination never skips or repeats a row. `total_count` is the full match count for the query, computed once and held stable across every page of the same pagination session — not a per-page or remaining-count figure.
- Graph-shaped tools (`graph`, `impact_analysis`, and `affected_tests`) take a deliberate v0 shortcut: a node-cap plus hard depth-clamp instead of true cursor pagination. `graph`'s envelope is `{index_generation, nodes, edges, node_cap, depth_clamp, truncated}` — no `results`/`total_count`/`next_cursor`. `nodes` and `edges` are both deduped (by node id / composite edge key) and cycle-safe. A `graph` node is either a resolved live symbol (full `render_symbol` shape + `resolved: true`) or, when the id doesn't resolve in `SymbolStore` (e.g. an `imports` edge's target, which is very often a raw dotted-name string rather than a symbol_id — an external import has no ACIE-tracked definition), an unresolved leaf `{id, resolved: false}`. `graph_type` maps to exactly one predicate: `call` → `calls`, `dependency` → `imports`; `inherits`/`references`/`defines` are excluded (already reachable via `find_references`/`get_definition`). `direction="downstream"` walks outbound edges from `root` (root is the edge's source); `direction="upstream"` walks inbound edges (root is the edge's target). `truncated` is `true` only when node_cap or depth_clamp actually cut off a real reachable node/edge — reaching depth_clamp with nodes still pending doesn't alone imply truncation if those nodes turn out to have no further edges. `impact_analysis` follows a wider, fixed predicate set instead of one `graph_type`-mapped predicate: `{calls, imports, overrides}` (`overrides` added in v1 slice A4, wayfinder ticket 732f8b2d — a base method's blast radius includes every subclass overriding it; `overrides` points at the immediate base only, so multi-level override chains fall out for free via the same generic BFS, no chain-walking code needed); `inherits`/`references`/`defines` stay excluded from it too (`inherits` deliberately did not join this set even though `overrides` did — the ticket resolution names `overrides` only). Its affected-symbol nodes carry the same resolved/unresolved shape as `graph`'s, plus an additional `discovery_predicate` field (unconditional, not `full`-gated, present on both resolved and unresolved leaves) naming whichever predicate first discovered that node — unlike `graph`, `impact_analysis` has no `edges` list at all, so this is the only way to tell why a node is present.
- **Tool annotations**: all 9 tools are marked read-only (standard MCP tool annotations).
- **Structured error codes**: `STALE_INDEX_GENERATION`, `SYMBOL_NOT_FOUND`, `INVALID_PATTERN`, `INDEX_NOT_READY`, and (specific to `explain`) `EDGE_NOT_FOUND` (distinct from `SYMBOL_NOT_FOUND` so a caller can tell "you gave a malformed/nonexistent pair" apart from "this pair exists but has no relation"). Public input-contract validation (added after a live qualification pass found the gap, LIVE_MCP_QUALIFICATION_REPORT.md 2026-09-01) adds three more, all raised before any store I/O: `INVALID_CURSOR` (any of the six cursor-bearing tools — a cursor that fails to decode, or that decodes but carries a `last_id` semantically wrong for that tool's ordering key, e.g. the wrong type or a non-composite value where a composite key is expected), `INVALID_LIMIT` (any of the six — `limit` is not a positive integer), and `INVALID_ARGUMENT` (the exactly-one-of-selector tools — `get_definition`, `find_references`, `explain` — given neither or both of their two selectors; `graph` given an unrecognized `graph_type`/`direction`; `graph`/`impact_analysis`/`affected_tests` given a non-positive `node_cap`/`depth_clamp`).
- MCP Resources and MCP Prompts are explicitly deferred to v1 — there is nothing parameterless worth exposing as a Resource/Prompt yet in v0.

## Prior Art Surveyed

The following existing code-intelligence tools were studied as prior art (not blindly copied): Tree-sitter, SCIP/Sourcegraph, Serena, Synaptic (github.com/ColinVaughn/Synaptic), Aider's Repo Map, ast-grep, Glean, Joern, CodeQL, Semgrep.

- **Confidence/provenance survey finding**: none of the 7 tools studied in depth for this specific question (SCIP, Glean, CodeQL, Semgrep, Joern, Serena, Aider Repo Map) attach a graded per-edge/per-fact confidence value to their base structural facts the way ACIE's design does. The closest precedents found were CodeQL's query-level `@precision` taxonomy (an ordinal low/medium/high/very-high scale attached at the query level, not per-fact) and Joern's binary `DISPATCH_TYPE`/blank-field marker. ACIE's goal of a unified IR with field-level provenance on every fact is genuinely novel among the tools surveyed, not an adaptation of an existing pattern.
- **What ACIE borrows from Synaptic** (a Rust-based, broader-scoped code-intelligence tool with an MCP server): the `token_budget`-to-node-cap mechanism for controlling response size; the terse-by-default vs. `full=true`-reveals-everything toggle; real pagination that reports both a `truncated` flag and a total count; hard depth-clamping on graph traversals; its `EXTRACTED`/`INFERRED`/`AMBIGUOUS`-style edge-confidence tagging concept; standard MCP tool annotations and structured error codes; and its (deferred-to-ACIE's-own-v1) use of MCP Resources/Prompts/completion.
- **What ACIE explicitly does not borrow from Synaptic**: Synaptic's much broader ~40-tool surface (it also does SQL analysis, vulnerability scanning, PR analysis, cross-repo federation — all out of ACIE's scope), its `speculate` tool that executes code in a worktree, and its bespoke "change-contract" system.
- **Methodology note**: citing an existing tool (including the separate SALTMDB project on this same machine) as architectural precedent required checking that tool's actual live source code, not just trusting a past decision/memory record describing an intended target state — a real example found during this project's planning was a SALTMDB decision record describing a UUIDv7/keyset-pagination migration that, on live inspection, turned out to still be unimplemented despite the record's confident description. Lesson: verify precedent against live source, not just documentation/memory.

## Not Yet Specified

Deferred to implementation time or to a future, narrower planning effort — not resolved here:

- Monorepo, multi-language-in-one-repo, generated-file, git-worktree, and temporarily-broken-repo handling. Explicitly deferred; the likely v0 stance is "single-language, single-root repos only" but this is not locked. The `repo_path`-vs-canonical-`resolve_repo_id` keying inconsistency itself is fixed (SALTMDB f4bdfc9d/decision 10's follow-up, grilled and shipped 2026-09-02): `WriteQueue`/`BootstrapCoordinator` now key their in-memory state on `resolve_repo_id`'s canonical, worktree-collapsing value, so a symlink and its realpath'd twin (two spellings of the identical worktree) share one writer thread and one bootstrap-readiness flag — and so do two genuinely distinct worktrees of one repo, since they share the same `repo_id`. `WatcherRegistry` keys on the already-canonical `resolve_repo_root` instead: the symlink/realpath'd spelling of one worktree still collapses to one filesystem watch (no duplicate `Observer` on the same directory), but two genuinely distinct worktrees intentionally each get their own — `repo_root` is the actual directory being watched, not a repo-wide identity, so leaving one worktree unwatched would be the bug, not the fix. What remains unsolved, by design, is *walk semantics* once two live worktrees genuinely share that state: whichever worktree registers with the daemon first runs bootstrap's walk-and-index pass (using its own on-disk content); a second worktree sharing the same `repo_id` sees `repo_ready()` already true/in-progress and is never separately walked, even if its checked-out branch has different content at the same paths. Both worktrees' watchers still feed live edits into the one shared index. Fixing that — deciding what "the index" means when worktrees diverge — is a real product question, not a keying bug, and stays out of v0 scope here.
- The exact LSP-availability detection/diagnostic surface (something like an `acie doctor` command) and what happens when a detected LSP server is the wrong version or behaving flakily.
- The language-adapter plugin interface shape, needed for adding a second language after the Python-only MVP.
- Schema/IR versioning and auto-rebuild-on-upgrade mechanics for `manifest.json`.
- An automated `acie install-hooks` merge-installer (intentionally deferred pending real v0 dogfeeding usage data, per the Agent Hook Integration decision above). (The daemon/MCP-server CLI surface itself — `acie serve-mcp`, `acie daemon start|stop|status`, `acie notify-hook` — is now specified in `DAEMON.md`.)
- Repository licensing/governance/naming conventions for ACIE as a standalone open-source project. Note: a `LICENSE` file already exists in the repo, but its specific terms/implications were not part of this architecture decision.

## Out of Scope

Nothing has been explicitly ruled out of ACIE's scope as of this document. This section exists as a placeholder for future scoping decisions.
