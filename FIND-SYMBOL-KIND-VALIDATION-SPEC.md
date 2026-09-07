# find_symbol's `kind` Filter Silently Accepts Bogus Values Instead of Raising INVALID_ARGUMENT

Status: spec-and-plan only, written for external implementation (same role
split as the D/H-series and MCP-*-SPEC docs — this document was **not**
implemented in the session that wrote it; a fresh session/agent should
implement it, and another fresh session should review the resulting diff
against this spec before commit).

Triggered by the 2026-09-07 third same-day ACIE reliability sweep (SALTMDB
memory `8791b995`): `find_symbol(kind="bogus_kind_xyz")` (not a real
symbol-kind value) silently returned `total_count: 0` instead of raising
`INVALID_ARGUMENT`, inconsistent with `min_confidence`, which correctly
raises `INVALID_ARGUMENT: min_confidence must be one of [...]` for an
equally-bogus value on the same tool. A typo'd `kind` is indistinguishable
from "no matches" today.

This spec touches only `src/acie/tools/find_symbol.py` and
`tests/tools/test_find_symbol.py`. Companion spec
`STRUCTURAL-SEARCH-FILES-DOCSTRING-SPEC.md` fixes a separate, unrelated
finding from the same sweep (`structural_search`'s `files` docstring). The
two specs touch entirely non-overlapping files and can be implemented and
reviewed independently, in parallel, in either order.

## Root cause, read directly from source this session (not re-guessed from
the report)

`src/acie/tools/find_symbol.py`:

```python
matches = symbol_store.search(qualname_substring=name, kind=kind, path_glob=path_glob)
matches = filter_by_min_confidence(matches, min_confidence)
```

`kind` is passed straight into `SymbolStore.search` (`src/acie/storage/
symbol_store.py:161-186`), which uses it as a raw SQL equality filter
(`kind = ?`) with no validation anywhere in the chain — an unrecognized
value just matches zero rows, exactly like a correct-but-unmatched value
would. `min_confidence`, by contrast, goes through
`filter_by_min_confidence` (`src/acie/tools/confidence.py`), which
constructs a real `Confidence` enum member from the input and raises
`InvalidArgumentError` (`InvalidArgumentError: min_confidence must be one
of [...], got ...`) if that fails. `kind` has no equivalent enum or
validation step anywhere in the codebase.

**No `Kind`/`SymbolKind` enum currently exists.** `Symbol.kind`
(`src/acie/ir/symbol.py:43`) is typed as a plain `str`, unlike
`Symbol.confidence`, which is typed `Confidence` (a real `str, Enum`
subclass). Confirmed via `grep` across `src/acie/` for every `kind="..."`
literal the codebase itself ever constructs: exactly four values are
produced anywhere — `"module"` (`extract_symbols.py`, the one symbol built
per file), `"class"` and `"method"` (`indexer.py`, `daemon/
lsp_enrichment.py`), and `"function"` (`indexer.py`). No fifth value exists
in the implementation today.

## Locked decisions

1. **Fix shape: validate `kind` against the known set of values the indexer
   actually produces, raise `InvalidArgumentError` naming what's valid** —
   same remediation pattern `min_confidence` already uses via
   `filter_by_min_confidence`/`Confidence(...)`, applied to `kind`. `None`
   stays a no-op (matches every other optional filter on this tool —
   `path_glob` also has no "valid glob" validation, and `kind=None` means
   "don't filter," not "match no kind").

2. **Do not change `Symbol.kind`'s type, `SymbolStore.search`'s signature,
   or the storage schema.** Widening this into a full `SymbolKind` enum
   replacing `Symbol.kind: str` throughout the IR (extract_symbols.py,
   indexer.py, lsp_enrichment.py, symbol_store.py's SQL layer, every
   `kind="..."` literal and serialization path) is a materially larger,
   separate refactor with no observed need beyond this one query-filter
   validation gap — out of scope here per this project's own "don't build
   speculative generality ahead of an observed need" norm (see `errors.py`'s
   docstring: "Only the codes an actual tool implementation currently
   raises get a class here"). This spec adds a **validation-only** check at
   the one place a bogus value currently produces a confusing result
   (`find_symbol`'s `kind` filter), scoped as narrowly as the bug itself.

3. **Where the valid-value set lives**: a module-level constant local to
   `src/acie/tools/find_symbol.py`,
   `_VALID_KINDS = frozenset({"module", "class", "function", "method"})`
   (the four literal values confirmed above) — not a new shared module.
   `kind` has exactly one caller today (`find_symbol` is the only tool with
   a `kind` parameter — confirmed via `grep` across `src/acie/tools/*.py`),
   so this follows the same "wait for a second caller before extracting a
   shared module" precedent this codebase already applies elsewhere (e.g.
   `structural_search.py`'s own `_LANGUAGE`/`_PROVENANCE_VERSION`
   duplication-not-yet-extracted comment) — do not preemptively create a
   shared `tools/kind.py` or move this constant into `ir/symbol.py` for a
   caller that doesn't exist yet.

4. **Validate before calling `symbol_store.search`**, at the top of
   `find_symbol`, mirroring where `min_confidence` conceptually applies
   (even though `min_confidence` is technically filtered after the query,
   `kind` must be checked before the query since it's used as a SQL
   parameter, not a post-filter):

   ```python
   if kind is not None and kind not in _VALID_KINDS:
       raise InvalidArgumentError(f"kind must be one of {sorted(_VALID_KINDS)}, got {kind!r}")
   ```

   Message wording mirrors `filter_by_min_confidence`'s existing
   `InvalidArgumentError` message shape (names what's valid, names what was
   given) — match that style exactly, don't invent a new phrasing
   convention for this one parameter.

5. **No change to `SymbolStore.search`.** The validation belongs in the
   tool layer (`find_symbol.py`), same layering `filter_by_min_confidence`
   already uses — `SymbolStore` stays a dumb SQL-filter layer with no
   knowledge of which values are "valid" at the tool-input level, consistent
   with its existing `kind=None`-means-"no filter" contract serving other
   internal callers too (`indexer.py`'s `find_by_qualname_and_kind`,
   `architecture.py`'s `kind="module"` calls) that must not be affected by
   this MCP-input-facing validation.

## Test plan (write first, red before green)

New tests in `tests/tools/test_find_symbol.py` (existing file, uses the
`_symbol(...)` fixture helper and `_stores_with_generation` already defined
there):

- `find_symbol(kind="bogus_kind_xyz")` raises `InvalidArgumentError` naming
  the four valid kinds and the bogus value given — mirrors the existing
  `min_confidence` invalid-value test's assertion style in this same file
  (check for that test and match its exact assertion pattern, e.g.
  `pytest.raises(InvalidArgumentError, match=...)` if that's the existing
  convention, or a plain `str(exc.value)` substring check otherwise).
- `find_symbol(kind="function")` (a real, valid value) still filters
  correctly and returns only matching symbols — regression proving the
  fix doesn't break the existing, working case.
- `find_symbol(kind=None)` (the default) still returns all matches
  regardless of kind — regression proving `None` stays a no-op.
- Regression: an existing or new test confirms `kind="module"`,
  `kind="class"`, and `kind="method"` are all still accepted (not just
  `"function"`) — covers the full known-valid set, not just one member.

All new/changed tests must go red before the fix, green after (TDD — the
`bogus_kind_xyz` test is red before the validation is added; the others are
regression tests for behavior this spec deliberately keeps unchanged, write
them anyway to lock the fix's boundary).

## Files to touch

- `src/acie/tools/find_symbol.py` — add `_VALID_KINDS` constant and the
  validation check at the top of `find_symbol` (Decisions 3-4).
- `tests/tools/test_find_symbol.py` — new validation and regression test
  cases (Test Plan).
- No change to `src/acie/storage/symbol_store.py`, `src/acie/ir/symbol.py`,
  `src/acie/indexer.py`, `src/acie/daemon/lsp_enrichment.py`, or
  `src/acie/tools/architecture.py` (Decisions 2, 5).
- `src/acie/tools/errors.py` — no change needed; `InvalidArgumentError`
  already exists and is exactly the right code (same one `min_confidence`
  already raises on this same tool).

## Verification note

Run `.venv/bin/python -m pytest -q` (the project's actual venv — plain
`python3 -m pytest` fails to collect, `acie` isn't installed on system
Python) before and after, from this spec's own dedicated worktree. Confirm
the suite's failure count doesn't grow beyond whatever pre-existing
baseline is observed fresh at implementation time (recent baselines on
master have been clean at 852-855 passed; the `test_daemon_stop_actually_
terminates_the_os_process`/`test_runtime_isolates_divergent_worktree_
indexes_and_storage` pair has intermittently appeared and disappeared
across sessions as pre-existing OS-process/worktree-runtime flakiness
unrelated to this spec's scope — if either reappears, don't treat it as
caused by this change).
