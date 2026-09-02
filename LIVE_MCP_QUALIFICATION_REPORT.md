# ACIE Live MCP Qualification Report

**Date:** 2026-09-01  
**Target:** the ACIE MCP server connected to this Codex session  
**Index generation:** 78 throughout the qualification  
**Mode:** direct `mcp__acie__*` requests only

## Executive summary

ACIE's currently implemented query tools are genuinely useful and their normal paths are well evidenced. Symbol navigation, definitions, same-file references, imports, structural search, local call/dependency graphs, basic impact analysis, provenance, and pagination all produced correct results against the live ACIE index.

This qualification added **480 direct live MCP requests** without editing source, configuration, daemon state, or index state:

| Area | Direct requests | Result |
|---|---:|---|
| Correctness, contracts, and pagination | 83 | 38/41 main assertions passed; 26/26 deeper topology/source assertions passed |
| Input-boundary probe | 30 | 6 healthy behaviors; 24 public input-contract gaps found |
| Semantic-scope probes | 6 | Confirmed method/class support and documented same-file/nested-scope limits |
| Latency measurement | 361 | 0 request errors; low medians with occasional shared-path tail latency |

The headline is therefore:

- **Normal valid use:** strong live evidence; the query core is ready for dogfooding.
- **Robustness against malformed or invalid client input:** not production-hardened yet. The audit found consistent `INTERNAL_ERROR` leakage and missing numeric validation.
- **Freshness after edits:** still unavailable because the v0 filesystem watcher/reindex trigger is not implemented. This is a v0 implementation gap, not a query-tool failure.

## Scope and method

All requests were made through the registered ACIE MCP tools. No pytest run, subprocess test harness, direct database access, or source/configuration edit was used. Read-only inspection of clean source files supplied an independent ground-truth oracle for sampled accuracy checks.

The active worktree already contained unrelated changes to `pyproject.toml`, `uv.lock`, and `HANDOVER-segfault-investigation.md`; none were touched. The source-grounded samples used clean Python modules.

### What was exercised

1. Every one of the eight MCP tools in normal/terse and `full=true` modes.
2. Every alternate public selector: symbol IDs versus source positions; symbol IDs versus edge references.
3. Exact known results, filters, empty results, missing symbols/files/edges, malformed Tree-sitter query syntax, and stale cursor generation.
4. Full pagination traversal wherever the live data had multiple rows.
5. Call and dependency graph directions, depth clamps, node caps, a multi-hop branching graph, and multi-hop impact.
6. Public validation boundaries: omitted/both selectors, malformed cursors, zero/negative limits, and non-positive graph/impact limits.
7. One discarded warm-up then 20 serial calls per tool, five mixed-tool concurrent waves, and a 50-sample follow-up for lookup, graph, and impact tails.

### Conditions deliberately not changed

The test did **not** stop/restart the active daemon, edit a source file, create a second index generation, or create a disposable registered repository. Those actions would either interrupt this live session or test an unimplemented freshness path. Consequently, cold daemon startup, reindex race behavior, historical `explain` pagination, deletion tombstones, and multi-repo isolation remain separate lifecycle tests.

## Functional and accuracy results

### Cross-tool query core

| Tool | Live evidence | Result |
|---|---|---|
| `find_symbol` | Exact lookup, kind/path filtering, empty result, terse/full provenance, 426-symbol traversal across 9 pages | Pass for valid inputs |
| `get_definition` | Symbol-ID and actual call-site position both resolved `create_mcp_server`; missing symbol/position returned `SYMBOL_NOT_FOUND` | Pass for valid inputs |
| `find_references` | Found definition plus exact local call at `mcp_server.py:77:4`; ID and position selectors agreed; 2 pages were non-overlapping | Pass for valid inputs |
| `list_imports` | Matched all 8 imports in `mcp_server.py`; 4 pages were non-overlapping; nonexistent file gave a clean empty result | Pass for valid inputs |
| `structural_search` | Found all 5 function definitions in `mcp_server.py`; 5 pages were non-overlapping; malformed query returned `INVALID_PATTERN` | Pass for valid inputs |
| `graph` | Correct local call edges in both directions; dependency graph contained all 8 import edges; cap/depth behavior worked on valid positive values | Pass for valid inputs |
| `impact_analysis` | Found the known caller of `create_mcp_server`; a branching CLI example found two direct callers plus `main` at depth 2 | Pass for valid inputs |
| `explain` | Returned provenance/history for both a symbol and exact composite call edge; missing edge/symbol gave typed errors | Pass for valid inputs |

All eight tools showed the expected confidence/provenance information in `full=true` mode. `explain` correctly showed those fields even without `full=true`, because observation history is its purpose.

### Independent source-grounded accuracy checks

The following checks used direct source text as an oracle, then queried the connected MCP server:

- **20/20 exact symbol assertions passed:** four top-level functions in `mcp_server.py`, eight in `cli.py`, the `BootstrapCoordinator` class plus four methods, and three dispatch functions all returned the expected symbol ID, kind, path, and start line.
- `list_imports` returned exactly the eight imports present in `mcp_server.py`: `inspect`, `json`, `os`, `collections.abc.Callable`, `mcp.server.mcpserver.MCPServer`, `mcp_types.CallToolResult`, `acie.daemon.client.request_daemon`, and `acie.daemon.dispatch.DISPATCH_TABLE`.
- `find_references(create_mcp_server)` returned exactly the module definition and `run_stdio_server`'s real call at line 77.
- A Tree-sitter function query returned the five source function definitions in `mcp_server.py`, including nested `call`; a class query returned exactly `WriteQueue` and `_RepoWriter`.
- The multi-hop local CLI call graph agreed with source: `_serve_mcp` and `_daemon_start` call `_ensure_daemon`, and `main` reaches both callers at depth 2. Impact analysis found exactly those three affected symbols at that scope.
- An impossible symbol produced no false-positive lookup result.

This is strong **sampled** accuracy evidence, not a global percentage across arbitrary Python repositories. It deliberately does not claim precision/recall for dynamic Python, different repository layouts, or cross-file resolution.

### Pagination and generation consistency

| Endpoint | Limit | Pages | Unique results | Outcome |
|---|---:|---:|---:|---|
| `find_symbol("_")` | 50 | 9 | 426 | Exact declared total; terminal cursor was null |
| `find_references(create_mcp_server)` | 1 | 2 | 2 | Definition and caller did not overlap |
| `list_imports(mcp_server.py)` | 2 | 4 | 8 | Exact import total; no overlap |
| `structural_search(function_definition)` | 1 | 5 | 5 | Exact syntax-match total; no overlap |

For the five generation-pinned flat tools, a syntactically valid cursor stamped with generation 77 correctly produced `STALE_INDEX_GENERATION` at live generation 78.

## Usability assessment

ACIE is already a productive first stop for these tasks:

- Locate the exact declaration and source span of a known symbol.
- Jump from a real call site to its same-file definition.
- Enumerate a file's imports without reading the whole file.
- Search source structure with native Tree-sitter queries.
- Trace a local call path and find a lower-bound local blast radius.
- Obtain `EXTRACTED` confidence and Tree-sitter provenance rather than treating every answer as equally certain.

For day-to-day work, the effective workflow is:

1. Start with ACIE for symbols, definitions, local references, structural candidates, and local impact.
2. Read only the returned source spans needed for semantic understanding.
3. Use `rg` as the complement for arbitrary text, configuration, comments, and cross-file imported calls.

ACIE is not yet a replacement for source reading or grep. In particular, `request_daemon`'s live references showed only its definition and same-file `daemon_is_running` caller; direct uses imported into `mcp_server.py` and `cli.py` were absent. That is the documented same-file relation-extraction boundary, so impact is a useful **lower bound**, not a complete safety guarantee.

Likewise, nested `call` is structurally searchable but not indexed as a navigable symbol. This is a known scope limit rather than a false result.

## Public input-contract findings

The normal query core works, but invalid-input handling needs a dedicated hardening pass.

| Finding | Live behavior | Root cause confirmed by source inspection | Priority |
|---|---|---|---|
| Malformed cursor | `find_symbol` returned `INTERNAL_ERROR` with raw base64 decoder text | Shared `decode_cursor()` does not convert base64/JSON/shape failures into an ACIE typed error; dispatcher generically wraps them | High API-quality issue |
| Zero limit | All six cursor-bearing tools raised `INTERNAL_ERROR: list index out of range` on nonempty results | `page = remaining[:limit]` leaves an empty page, then `page[-1]` is used while building a cursor | High API-quality issue |
| Negative limit | `find_symbol`, references, imports, and structural search returned inconsistent sliced pages; definition and explain instead crashed | No lower-bound validation before Python slicing/cursor construction | High API-quality issue |
| Exactly-one selector validation | Omitted or simultaneous selectors for definition/references/explain returned precise messages but generic `INTERNAL_ERROR` | Tools raise `ValueError`, which dispatcher deliberately maps to generic internal error | Medium API-quality issue |
| Invalid graph type/direction | Precise message, but generic `INTERNAL_ERROR` | Same `ValueError` to generic dispatcher path | Medium API-quality issue |
| Non-positive `node_cap` / `depth_clamp` | Graph and impact accept zero/negative values and return a root node despite a zero cap | Root is seeded before cap validation; no public numeric validation exists | Medium API-quality issue |

The malformed-cursor issue is shared by six cursor-bearing tools (`find_symbol`, `get_definition`, `find_references`, `list_imports`, `structural_search`, and `explain`) because they all call the same decoder. The live request directly confirmed it for `find_symbol`; the installed runtime source confirms the shared path.

These are input-contract failures, not evidence of incorrect results for valid queries. They should nevertheless be fixed before treating the MCP interface as robust against arbitrary agent-generated parameters.

## Latency

### Measurement method

Numbers below are client-observed wall time around an awaited direct live MCP request using `Date.now()`. One warm-up was discarded per tool. The primary pass used 20 serial samples for each tool (160 timed requests); a five-wave mixed concurrent test added 40 timed requests. A follow-up used 50 serial samples each for lookup, graph, and impact (150 timed requests). No measured request returned an error.

### Primary warm-cache serial results

| Tool | N | Median | p90 | p95 | Max | Mean |
|---|---:|---:|---:|---:|---:|---:|
| `find_symbol` | 20 | 33 ms | 41 ms | 101 ms | 210 ms | 44.6 ms |
| `get_definition` | 20 | 26 ms | 29 ms | 100 ms | 295 ms | 43.7 ms |
| `find_references` | 20 | 32 ms | 35 ms | 39 ms | 194 ms | 39.0 ms |
| `list_imports` | 20 | 34 ms | 53 ms | 107 ms | 285 ms | 51.1 ms |
| `structural_search` | 20 | 50 ms | 64 ms | 75 ms | 77 ms | 51.4 ms |
| `graph` | 20 | 70 ms | 106 ms | 161 ms | 634 ms | 103.4 ms |
| `impact_analysis` | 20 | 32 ms | 149 ms | 380 ms | 388 ms | 76.1 ms |
| `explain` | 20 | 36 ms | 41 ms | 49 ms | 52 ms | 36.5 ms |
| **All serial requests** | **160** | **35 ms** | **86 ms** | **149 ms** | **634 ms** | **55.7 ms** |

### Tail follow-up

The initial graph/impact outliers were not sufficient to call those tools uniquely slow. A 50-sample follow-up reproduced rare tails for simple lookup too:

| Tool | N | Median | p95 | p99 | Max | Calls >100 ms | Calls >250 ms |
|---|---:|---:|---:|---:|---:|---:|---:|
| `find_symbol` | 50 | 34 ms | 104 ms | 325 ms | 325 ms | 3 | 1 |
| `graph` | 50 | 39 ms | 189 ms | 381 ms | 381 ms | 4 | 2 |
| `impact_analysis` | 50 | 27 ms | 56 ms | 290 ms | 290 ms | 1 | 1 |

The evidence supports a **shared request-path or scheduling tail** rather than a graph-only regression. That is an inference from the repeated cross-tool pattern; server-side instrumentation would be required to attribute it to the MCP client, IPC, daemon, SQLite, or host scheduling.

### Concurrent mixed requests

Five waves launched one valid request for each of the eight tools concurrently. All 40 requests succeeded.

| Metric | Value |
|---|---:|
| Per-request median | 193 ms |
| Per-request p95 | 233 ms |
| Per-request max | 326 ms |
| Whole-wave median | 207 ms |
| Whole-wave max | 326 ms |

This is a small client-observed stress probe, not a server-throughput benchmark. It does show that a modest mixed burst completed without errors, while latency rose materially over serial calls.

### Bootstrap time already observed

The initial index database timestamps supplied earlier measured **8 minutes 4.574 seconds** from creation to last modification for this repository. That is a single initial-bootstrap observation, not a scaling benchmark.

## Known scope limits and untested lifecycle paths

These are not silently omitted from the result:

- **Automatic freshness is absent:** the v0 filesystem watcher, hooks, `notify-hook`, and lazy stale correction are not implemented. A changed file is not automatically reindexed.
- **Cross-file imported-call resolution is intentionally deferred:** current references, graph, and impact undercount imported callers.
- **Nested functions are syntax-searchable but not navigable symbols.**
- **`min_confidence` is specified architecturally but not exposed by the current live MCP schemas.**
- **Multi-generation history, deletion tombstones, and actual `explain` pagination** require a controlled reindex and were not possible at this static generation.
- **Cold start, daemon-unavailable handling, daemon restart recovery, multi-repo isolation, external repositories, and larger corpus scaling** require a deliberately isolated lifecycle test and were not performed against this active session.

## Conclusion and next priorities

The evidence supports using ACIE now for same-file code navigation and local change reasoning. Its normal results are fast at the median, internally coherent, provenance-bearing, and accurate in the sampled source ground truth.

Before declaring the MCP surface robust, prioritize:

1. Public parameter validation and a typed invalid-argument error path.
2. Safe cursor decoding that cannot leak base64/JSON exceptions as `INTERNAL_ERROR`.
3. Positive validation for `limit`, `node_cap`, and `depth_clamp`.
4. The v0 filesystem watcher/reindex lifecycle, followed by a controlled edit/delete/rename and multi-generation history qualification.
5. Server-side timing instrumentation if the observed shared latency tail matters in real dogfooding.

No code was changed as part of this qualification; this report records live evidence and a clean, prioritized follow-up target.
