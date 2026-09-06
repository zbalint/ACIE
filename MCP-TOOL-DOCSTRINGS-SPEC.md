# MCP Tool-Facing Docstrings — Every Tool Ships With No Caller-Visible Description

Status: spec-and-plan only, written for external implementation (same role
split as the D/H-series specs — this document was **not** implemented in
the session that wrote it; a fresh session/agent should implement it, and
another fresh session should review the resulting diff against this spec
before commit).

Triggered by the 2026-09-07 live 10-tool reliability sweep (SALTMDB memory
`cafd7284`, full report reproduced in memory `271e36d7`). That report's
finding #1 called `structural_search` "completely broken — 0% success
rate" after every pattern it tried (including a bare `import os` and a
bare `$X`) failed with `INVALID_PATTERN`, cross-checked against the real
`ast-grep` CLI (which accepted the same patterns).

## Corrected diagnosis (verified against source this session, not assumed)

`structural_search` is **not** broken. `ARCHITECTURE.md`'s MCP Tool
Surface table (line 188) already documents the actual contract:

> `pattern` is a required tree-sitter-native `.scm` query string —
> explicitly not an ast-grep pattern (see "Design Principles"). Returns
> `INVALID_PATTERN` if the pattern doesn't parse.

`src/acie/tools/structural_search.py:81` confirms the implementation
matches this exactly: `Query(_LANGUAGE, pattern)` is a raw
`tree_sitter.Query` construction, not an ast-grep pattern compiler.
`import os` and `$X` are both invalid tree-sitter `.scm` syntax (tree-sitter
queries are s-expressions like `(import_statement) @import`, not literal
source snippets with `$X`-style metavariables — that's ast-grep's own
pattern language, a different tool the report's session cross-checked
against by mistake). `INVALID_PATTERN` on both inputs is the tool
behaving exactly as designed.

So why did a careful reviewer — who independently verified byte-exact
coordinates elsewhere in the same report — conclude the tool was
"completely broken"? Because **nothing in the tool's actual MCP-facing
description says any of this.** Traced to root cause in
`src/acie/mcp_server.py:60`:

```python
call.__doc__ = tool.__doc__ or f"Run ACIE's {method} code-intelligence query."
```

The description every MCP client sees for a tool is `fn.__doc__` verbatim
(confirmed in the vendored `mcp` package,
`mcp/server/mcpserver/tools/base.py:84`: `func_doc = description or
fn.__doc__ or ""` — the whole raw docstring becomes the tool's single
`description` field, no per-parameter doc extraction). Checked all 10 tool
functions directly via `ast.get_docstring` this session:

```
find_symbol       NO DOCSTRING       impact_analysis   NO DOCSTRING
get_definition    NO DOCSTRING       explain           NO DOCSTRING
find_references   NO DOCSTRING       affected_tests    NO DOCSTRING
list_imports      NO DOCSTRING       architecture      NO DOCSTRING
graph             NO DOCSTRING       structural_search NO DOCSTRING
```

**All ten** tool functions have zero docstring. Every single one of ACIE's
MCP tools currently falls back to the generic placeholder — `"Run ACIE's
<method> code-intelligence query."` — with no parameter shapes, no
mutually-exclusive-selector notes, no error codes, and (specific to
`structural_search`) no statement of which pattern language it accepts.
This is a systemic gap, not specific to `structural_search`; that tool is
simply the one where the missing contract happens to be unguessable
instead of merely inconvenient. ARCHITECTURE.md already has accurate,
correct prose for every one of these tools (the "MCP Tool Surface"
section) — it just never made it onto the function a caller actually
queries.

This spec's fix eliminates the specific confusion that produced the live
report's finding #1, and closes the same gap for the other 9 tools before
it produces an equally-confusing false bug report against one of them.

## Locked decisions

1. **Fix location: a real docstring on each of the 10 tool functions in
   `src/acie/tools/*.py`**, not `mcp_server.py`'s fallback string and not a
   new out-of-band description registry. `mcp_server.py:60` already prefers
   `tool.__doc__` when present — no change needed there. Rejected
   alternative: a separate `TOOL_DESCRIPTIONS: dict[str, str]` keyed by
   method name in `mcp_server.py` — this would duplicate
   `ARCHITECTURE.md`'s content a second time, arguably a third if the
   function itself later gained its own docstring too; one canonical
   docstring per function is the only version that can't drift out of sync
   with itself.
2. **Content per docstring**: one-sentence purpose, then each parameter's
   shape/meaning (spelling out any structured `dict` parameter's exact
   keys — `position={file, line, column}` for `get_definition`/
   `find_references`; `edge_ref={source_symbol_id, target_symbol_id,
   predicate, site_file, site_line, site_col}` for `explain`), which
   selectors are mutually exclusive, and which structured error codes the
   tool can raise (per the "Structured error codes" cross-cutting rule in
   `ARCHITECTURE.md`). Source this from `ARCHITECTURE.md`'s existing
   per-tool table rows and cross-cutting rules — condense, don't
   re-derive from scratch; that table is already correct and reviewed.
3. **`structural_search` gets one extra, mandatory sentence**: an explicit
   statement that `pattern` is tree-sitter's own `.scm` query syntax, is
   **not** an ast-grep pattern, and a one-line syntax example (e.g.
   `(import_statement) @import` or `(call) @call`) — this is the exact
   fact whose absence produced the live false-positive bug report.
4. **Module docstrings stay as they are.** Several tool files
   (`structural_search.py`, `explain.py`, `resolve.py`) already carry a
   rich module-level docstring documenting internal design rationale,
   confirmed seams, and rejected alternatives for whoever edits that file
   next. That content is for implementers, not MCP callers, and is not
   duplicated onto the function docstring — the function docstring is a
   distinct, caller-facing artifact with a narrower job (what does an
   agent calling this tool blind need to know before its first call).
   Where a function docstring needs a fact already stated in its module
   docstring (e.g. `structural_search`'s pattern-language contract), state
   it again concisely in the function docstring rather than only pointing
   up at the module docstring — a caller-facing description should be
   self-contained, not require reading a second location.
5. **Regression guard**: add one small test to the existing
   `tests/test_mcp_server.py` that iterates
   `acie.daemon.dispatch.DISPATCH_TABLE` and asserts every
   registered tool function has a non-empty `__doc__`. This is a cheap,
   permanent guard against a future 11th tool shipping the same silent gap
   — mirrors this project's own established practice of turning every
   live-discovered contract gap into a permanent test (see
   `LIVE_MCP_QUALIFICATION_REPORT.md`'s cursor/limit hardening, itself now
   covered by `tests/tools/test_pagination.py`).

## Docstrings to write (content, not exact wording — match this codebase's
existing prose style; see `resolve.py`'s docstring for the target register)

- **`find_symbol`** (`src/acie/tools/find_symbol.py`): substring match on
  `name` (required); optional `kind` enum filter, `path_glob`, and
  `min_confidence`. Ordered by symbol id.
- **`get_definition`** (`get_definition.py`): resolves to a symbol via
  exactly one of `symbol_id` or `position` (`{file, line, column}` —
  `line` is 1-indexed, `column` is 0-indexed; confirmed against
  `extract_symbols.py:288`'s `start_line=node.start_point.row + 1` vs.
  `start_col` staying tree-sitter's raw 0-indexed column). `position` also
  triggers a tier-4 lazy staleness check on that file. Raises
  `INVALID_ARGUMENT` if neither or both selectors are given,
  `SYMBOL_NOT_FOUND` if neither resolution step matches. Accepts
  `min_confidence`.
- **`find_references`** (`find_references.py`): same
  `symbol_id`/`position` shape and errors as `get_definition`; returns
  every reference site instead of the definition.
- **`list_imports`** (`list_imports.py`): `file` required; always
  `EXTRACTED` confidence, so `min_confidence`/`full`-gated confidence
  fields are not meaningful here. Triggers a tier-4 staleness check on
  `file`.
- **`structural_search`** (`structural_search.py`): `pattern` is a
  **tree-sitter-native `.scm` query string, not an ast-grep pattern** —
  state this explicitly, with an example. `files` is caller-supplied
  `{path: source_text}` (ACIE's IR stores no source text, so there is
  nothing indexed to query against — this is why this is the one tool
  that takes explicit source rather than reading from the index).
  Optional `path_glob`. Raises `INVALID_PATTERN` if the pattern fails to
  parse.
- **`graph`** (`graph.py`): `root` (symbol_id) plus `graph_type`
  (`call`|`dependency`) and `direction` (`upstream`|`downstream`) —
  name the valid enum values explicitly in the docstring, since a caller
  guessing them today only discovers valid values from the error message
  after a failed call (worth surfacing up front). Node-cap/depth-clamp
  shortcut, not cursor pagination — envelope has no `results`/
  `total_count`/`next_cursor`.
- **`impact_analysis`** (`impact_analysis.py`): `root` (symbol_id);
  fixed `{calls, imports, overrides}` predicate set, not
  `graph_type`-selected. Returns capped affected-symbol list plus
  `impact_summary` confidence-tier counts.
- **`explain`** (`explain.py`): exactly one of `symbol_id` or `edge_ref`.
  **Spell out `edge_ref`'s full 6-key shape in the docstring** —
  `{source_symbol_id, target_symbol_id, predicate, site_file, site_line,
  site_col}` — this is the exact fact whose absence produced live report
  finding #3 (companion `MCP-STRUCTURED-INPUT-VALIDATION-SPEC.md` fixes
  the raised-exception side of that same finding). Full observation
  history, newest-first, confidence/provenance always shown regardless of
  `full`. Never raises `STALE_INDEX_GENERATION`.
- **`affected_tests`** (`affected_tests.py`): `root` (symbol_id); narrower
  `{calls, overrides}` predicate set (a test covers a symbol by calling or
  overriding it, not importing it); pytest-convention test identification,
  not real coverage data.
- **`architecture`** (`architecture.py`): `root` is an optional
  path-prefix scope (`None` = whole repo). `granularity`
  (`file`|`package`) — name both valid values. Returns nodes/edges,
  optional `.acie/config.json`-driven `layer_violations`, and
  unconditional Tarjan `cycles`.

## Test plan

- New regression test asserting every `DISPATCH_TABLE` entry has a
  non-empty `__doc__` (Decision 5).
- No existing test should need to change — this is docstring-only, no
  behavior change. Full suite must stay green at its current baseline.
- Manual/spot check (not an automated test, just a sanity check before
  calling this done): render each new docstring through
  `create_mcp_server()`'s actual tool registration and confirm
  `call.__doc__` now reflects the new text end to end, not just the
  bare function's own `__doc__` in isolation — `_daemon_tool`'s wrapper
  (`mcp_server.py:60`) is what actually gets exposed, so verify the
  wrapper picks it up correctly (it already does by construction; this is
  a confirmation step, not expected to need any code change there).

## Files to touch

- `src/acie/tools/find_symbol.py`, `get_definition.py`,
  `find_references.py`, `list_imports.py`, `structural_search.py`,
  `graph.py`, `impact_analysis.py`, `explain.py`, `affected_tests.py`,
  `architecture.py` — add one docstring each to the tool's top-level
  function.
- `tests/test_mcp_server.py` — new test covering Decision 5.
- No change needed to `src/acie/mcp_server.py`, `src/acie/daemon/*`, or
  `ARCHITECTURE.md` (ARCHITECTURE.md's existing table is the accurate
  source this spec condenses from — no correction needed there).

## Verification note

Run `.venv/bin/python -m pytest -q` (the project's actual venv — plain
`python3 -m pytest` fails to collect, `acie` isn't installed on system
Python) before and after. Baseline at spec-write time: **834 passed, 2
failed** (`tests/daemon/test_runtime.py::
test_runtime_isolates_divergent_worktree_indexes_and_storage` and
`tests/test_cli.py::test_daemon_stop_actually_terminates_the_os_process`
— both pre-existing, OS-process/worktree-runtime tests unrelated to this
spec's scope; confirm they're still the *only* two failures afterward,
don't investigate or fix them as part of this spec).

## Relationship to the companion spec

`MCP-STRUCTURED-INPUT-VALIDATION-SPEC.md` fixes the other two confirmed
bugs from the same live report (raw `KeyError` leaks on malformed
`position`/`edge_ref` input). The two specs touch non-overlapping files
and can be implemented and reviewed independently, in either order.
