# Structured Validation for `position` and `edge_ref` Dict Inputs

Status: spec-and-plan only, written for external implementation (same role
split as the D/H-series specs — this document was **not** implemented in
the session that wrote it; a fresh session/agent should implement it, and
another fresh session should review the resulting diff against this spec
before commit).

Triggered by the 2026-09-07 live 10-tool reliability sweep (SALTMDB memory
`cafd7284`), specifically findings #2 and #3 (memories `3d93c426` and
`a1c8ccad`). Fixes the two confirmed input-validation bugs from that
report. Companion spec `MCP-TOOL-DOCSTRINGS-SPEC.md` fixes the report's
finding #1 (a documentation gap, not a code bug — see that spec for why)
and adds the caller-facing documentation of `position`/`edge_ref`'s
required shapes that this spec's validation now enforces at runtime. The
two specs touch non-overlapping files and can be implemented/reviewed
independently, in either order.

## Confirmed bugs (read directly from source this session, not re-guessed
from the report)

### Bug 1 — `explain`'s `edge_ref` leaks a raw `KeyError`

`src/acie/tools/explain.py:127-135`, `_edge_entries`:

```python
def _edge_entries(relation_store: RelationStore, edge_ref: dict) -> list[tuple[tuple, dict]]:
    key = {
        "source": edge_ref["source_symbol_id"],
        "target": edge_ref["target_symbol_id"],
        "predicate": edge_ref["predicate"],
        "site_file": edge_ref["site_file"],
        "site_line": edge_ref["site_line"],
        "site_col": edge_ref["site_col"],
    }
```

Any `edge_ref` missing one of these six keys raises a bare `KeyError` here.
`src/acie/daemon/dispatch.py:108-111` only maps `AcieToolError` subclasses
to their structured wire code; everything else falls through to
`except Exception` and becomes `INTERNAL_ERROR` with `str(exc)` as the
message — for a `KeyError`, `str(exc)` is just the missing key's repr
(`"'source_symbol_id'"`), which is exactly the raw, unhelpful text the live
report observed. `ARCHITECTURE.md`'s own "Structured error codes"
cross-cutting rule documents `INVALID_ARGUMENT` as the correct code for
"the exactly-one-of-selector tools ... given neither or both of their two
selectors" — this bug is the same class of contract violation (malformed
selector shape) one level deeper, and should raise the same
`InvalidArgumentError` the neither/both check already does one line above
this function (`explain.py:66-67`), not leak an implementation-internal
exception type.

### Bug 2 — `position`'s dict fields leak the same way

`src/acie/tools/resolve.py`, `resolve_symbol_or_position`:

```python
path, line, column = position["file"], position["line"], position["column"]
```

Same failure mode, same missing-key `KeyError` → generic `INTERNAL_ERROR`
leak, shared by both `get_definition` and `find_references` (both call
this one helper — see the module docstring's own note that this is
"byte-identical resolution semantics" shared via extraction). The live
report's own repro: guessing the field name `col` instead of `column`
produced a raw `KeyError: 'column'` rather than a clean validation error.

Also **not yet covered by any test or check**: neither `position` nor
`edge_ref` is verified to actually be a `dict` before being subscripted.
`get_definition(position="not-a-dict")` passes today's only guard (`(symbol_id
is None) == (position is None)`, which only checks for `None`, not type) and
then raises a bare `TypeError` inside `resolve.py`'s tuple-unpacking line —
the same leak, different exception type. Same shape of bug applies to
`edge_ref`. In scope for this spec, since it's the same defect class at the
same two call sites.

### The live report's third sub-claim — investigated, not reproduced

The live report additionally claimed position-mode "failed even on the
trivial case of pointing exactly at a symbol's own definition site," with
byte-exact-verified coordinates, and concluded this "isn't a data-freshness
or indexing issue." Checked this session, with two results:

1. **This exact scenario already has a passing regression test** —
   `tests/tools/test_find_references.py:194`,
   `test_find_references_by_position_on_the_definition_resolves_via_symbol_start`
   — part of the current 834-passing baseline. `render.py` also passes
   `path` straight through unmodified (no normalization that could produce
   a stored-vs-returned mismatch), and `resolve.py`'s exact-match SQL
   queries (`symbol_store.at_start`, `relation_store.list_by_site`) have no
   other transformation applied to their inputs. No code path was found
   that would make a byte-exact, correctly-shaped position lookup fail
   against a stable index.
2. The live report's own testing conditions explicitly note **background
   reindexing was actively running throughout the sweep** (index_generation
   moved 538→540 during the session). Unlike cursor pagination (which pins
   to a generation and fails closed with `STALE_INDEX_GENERATION` if the
   index moves — confirmed working correctly elsewhere in the same
   report), a single `position`-mode call has no equivalent generation
   pin — it just reads live store state at call time. A transient
   delete-then-reinsert of a symbol's row mid-reindex (part of normal
   merge-policy diffing) landing between when the report's coordinates were
   captured and when the position-mode call executed is a plausible,
   race-shaped explanation that doesn't implicate `resolve.py`'s logic.

**Conclusion, not to be re-litigated without new evidence**: this sub-claim
does not get a code fix in this spec — there is no reproducing test and no
logic defect found. The implementing session should still add the
regression test in the Test Plan below (a stable-index round-trip: resolve
by `symbol_id`, take that result's own `path`/`start_line`/`start_col`
verbatim, call again by `position` with those exact values, assert it
resolves to the same symbol) as **new, explicit coverage of this exact
scenario** distinct from the existing find_references-only test — if it
fails, that's new evidence and the fix belongs in a follow-up spec, not a
silent scope-creep onto this one; if it passes (expected), it stands as
permanent documentation that the live report's transient failure was
environmental, not a defect.

## Locked decisions

1. **Fix shape: validate required keys explicitly, raise
   `InvalidArgumentError` naming what's missing** — extends the exact
   precedent already established in `pagination.py`
   (`decode_cursor`/`coerce_tuple_key`/`filter_since`, all added per
   `LIVE_MCP_QUALIFICATION_REPORT.md`'s 2026-09-01 finding of the same bug
   class: "a malformed cursor used to let raw base64/JSON/unpack exceptions
   escape here, which dispatch.py's generic `except Exception` then turned
   into an unhelpful INTERNAL_ERROR"). Same remediation, same error class,
   applied to the two remaining structured-dict inputs that never got it.
2. **New shared helper, not two copies**: both call sites (`resolve.py`'s
   `position`, `explain.py`'s `edge_ref`) need identical
   "is this a dict, does it have exactly these keys" validation. Per this
   codebase's own "wait for a second caller" extraction norm (already
   invoked for `pagination.py`, `resolve.py`, `pytest_conventions.py`,
   `module_paths.py` — see their module docstrings), two real call sites
   needing the identical check clears that bar on first use here, unlike
   the deliberate one-remaining-duplicate precedents in
   `structural_search.py`/`extract_symbols.py`/`extract_relations.py`
   (those are single-caller today; this is two callers from the start).
   New module: `src/acie/tools/validation.py`, one function:

   ```python
   def require_dict_keys(value: object, required: set[str], *, param_name: str) -> dict:
       """Validates `value` is a dict containing every key in `required`.

       Raises InvalidArgumentError (not a bare TypeError/KeyError) if
       `value` isn't a dict, or is missing any required key -- the shared
       fix for the bug class LIVE_MCP_QUALIFICATION_REPORT.md's cursor/
       limit hardening already fixed for cursor/limit inputs, applied here
       to position/edge_ref (see MCP-STRUCTURED-INPUT-VALIDATION-SPEC.md).
       Returns `value` unchanged (typed as dict) so call sites can use it
       directly, e.g. `position = require_dict_keys(position, {...}, param_name="position")`.
       """
   ```

   Exact error message wording is an implementation detail, but must name
   `param_name` and either "must be an object/dict" (wrong type) or the
   specific missing key name(s) (present but incomplete) — mirroring
   `InvalidCursorError`'s and `InvalidLimitError`'s existing messages,
   which always say what was wrong, not just that something was.
3. **Extra keys are not an error.** `require_dict_keys` checks that
   `required <= value.keys()`, not `value.keys() == required` — a caller
   passing extra, ignored fields (e.g. accidentally forwarding a whole
   `find_references` result item as `edge_ref`, which is a very plausible
   first-time-caller mistake per the live report's "the natural thing to
   do") should fail only if a truly required key is absent, never merely
   for carrying extra data ACIE doesn't need.
4. **Call sites**:
   - `resolve.py`, top of `resolve_symbol_or_position`'s `position` branch:
     `position = require_dict_keys(position, {"file", "line", "column"}, param_name="position")`
     before the existing `position["file"], position["line"],
     position["column"]` line (which can then stay as plain subscripting,
     now guaranteed safe).
   - `explain.py`, top of `_edge_entries`:
     `edge_ref = require_dict_keys(edge_ref, {"source_symbol_id",
     "target_symbol_id", "predicate", "site_file", "site_line",
     "site_col"}, param_name="edge_ref")` before the existing `key = {...}`
     construction.
5. **No numeric/type coercion beyond dict-shape validation.** `line`,
   `column`, `site_line`, `site_col` being non-`int` (e.g. a caller passing
   `"5"` as a string) is a different, narrower bug class the live report
   never actually hit or reported — out of scope here per this project's
   own "don't declare speculatively ahead of time, wait for a real
   observed gap" norm (see `errors.py`'s own docstring: "Only the codes an
   actual tool implementation currently raises get a class here"). If a
   future live pass finds this is a real problem, it gets its own spec.

## Test plan (write first, red before green)

New tests in `tests/tools/test_validation.py` (new file, mirrors
`tests/tools/test_pagination.py`'s per-helper-module test file convention):

- `require_dict_keys` on a plain dict with all required keys present
  (plus one extra, unrequired key) returns it unchanged.
- `require_dict_keys` on a dict missing one required key raises
  `InvalidArgumentError` naming `param_name` and the missing key.
- `require_dict_keys` on a non-dict (`"a string"`, `None`, `["a", "list"]`)
  raises `InvalidArgumentError` naming `param_name`.

Extend `tests/tools/test_get_definition.py` and
`tests/tools/test_find_references.py` (both exercise the shared
`resolve.py` helper):

- `position={"file": "pkg/mod.py", "line": 1}` (missing `column`) raises
  `InvalidArgumentError`, not `KeyError`.
- `position="not-a-dict"` raises `InvalidArgumentError`, not `TypeError`.
- New stable-index round-trip regression test per the "third sub-claim"
  investigation above: resolve a known symbol by `symbol_id`, take the
  result's own `path`/`start_line`/`start_col` from the response verbatim,
  call again by `position` built from those exact values, assert the same
  symbol comes back. (For `get_definition` specifically — the
  find_references-side equivalent already exists per the investigation
  above; add the `get_definition`-side one since only find_references
  currently has it.)

Extend `tests/tools/test_explain.py`:

- `edge_ref={"source_symbol_id": ..., "target_symbol_id": ..., "predicate": ...}`
  (missing the three `site_*` keys) raises `InvalidArgumentError`, not
  `KeyError`.
- `edge_ref="not-a-dict"` raises `InvalidArgumentError`, not `TypeError`.
- Regression: the existing `_edge_ref(relation)` test helper (full,
  correctly-shaped dict) must keep working unchanged — confirms
  `require_dict_keys`'s "extra keys are fine" behavior isn't accidentally
  tightened to exact-match.

All new/changed tests must go red before the fix, green after (TDD, not
retrofitted assertions).

## Files to touch

- `src/acie/tools/validation.py` — new file, `require_dict_keys` (Decision 2).
- `src/acie/tools/resolve.py` — one new call at the top of the `position`
  branch (Decision 4).
- `src/acie/tools/explain.py` — one new call at the top of `_edge_entries`
  (Decision 4).
- `tests/tools/test_validation.py` — new file.
- `tests/tools/test_get_definition.py`, `tests/tools/test_find_references.py`,
  `tests/tools/test_explain.py` — new test cases per the Test Plan.
- No change to `src/acie/tools/get_definition.py` or `find_references.py`
  themselves — both call `resolve_symbol_or_position` without touching
  `position` directly, so the fix in `resolve.py` covers both for free.
- No change needed to `src/acie/tools/errors.py` — `InvalidArgumentError`
  already exists and is exactly the right code (used for the sibling
  neither/both-selector check one line above each of this spec's two call
  sites).

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
