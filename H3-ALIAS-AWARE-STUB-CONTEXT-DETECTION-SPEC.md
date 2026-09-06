# H3 Follow-Up — Alias-Aware Protocol/ABC/abstractmethod Matching

Status: spec-and-plan only, written for external implementation (same role
split as D1–D4/C6 — this document was **not** implemented in the session
that wrote it; a fresh session/agent should implement it, and another
fresh session should review the resulting diff against this spec before
commit).

Second of two known scope gaps flagged in H3's implementation review
(`fbda1c06`, "Warnings" section) as non-blocking limitations, not required
by the locked H3 spec. The first gap (qualified/generic base classes
producing zero `inherits` edge) was fixed directly this session in
`extract_relations.py` (`_terminal_name`/`_top_level_base_names`/
`_inherits_relations`) — see commit pending on `master`, 818/818 tests
passing. This document covers the second, harder gap: **alias-only
Protocol/ABC/abstractmethod name matching**.

## The gap

`extract_symbols.py`'s stub-context detection (`_class_contexts`,
`_base_names`, `_has_abc_meta`, `_has_abstractmethod_decorator` — all in
`src/acie/adapters/python/extract_symbols.py`) checks for the **literal**
names `"Protocol"`, `"ABC"`, `"ABCMeta"`, `"abstractmethod"` via
`_terminal_name()`. A qualified reference (`class Foo(typing.Protocol):`)
already resolves correctly today — `_terminal_name` on an `attribute` node
returns just the trailing attribute text ("Protocol"), discarding the
qualifier. The actual gap is narrower: an **aliased** `from`-import used
directly as a base or decorator —

```python
from typing import Protocol as P
class Foo(P):              # not detected as Protocol context
    def bar(self) -> int: ...

from abc import abstractmethod as amethod
class Base(ABC):
    @amethod                # not detected as an abstractmethod
    def bar(self) -> int: ...
```

— silently fails stub detection: `bar` is never marked `is_stub=True`, so
H3's `_resolve_protocol_stub_calls` never fires for it. This is a
false-negative gap (misses real stub cases), not a false-positive risk.

## What this session verified live against actual source (not guessed)

- `extract_symbols.py` currently does **zero** import-statement parsing —
  confirmed by reading the full file (`grep -n "^from\|^import\|import_statement"`
  returns only this module's own top-level imports, nothing tree-walking).
  It is a deliberately pure, single-file, no-cross-reference function; its
  public signature is `extract_symbols(path, source_text, observed_at)`.
- `extract_relations.py` already builds an alias map via `_import_relations`
  → `_handle_import_from_statement`, but its `alias_map: dict[str, str]`
  stores **`bound_name -> source module`** (e.g. `from typing import
  Protocol as P` → `alias_map["P"] = "typing"`), never the pre-alias
  original name ("Protocol"). This map cannot answer "what was P before
  aliasing?" as-is, and is consumed by several existing call sites
  (`DeferredImportInherit`/`DeferredImportOverride`/`DeferredImportCall`/
  `DeferredImportSelfCall` construction, `_is_pytest_fixture_decorator`)
  that all expect the "module" contract — none of the value semantics can
  change without touching every one of those.
- `extract_relations.py` has an existing, explicit precedent for this file
  boundary: `_unwrap_decorated`'s own docstring states it is "kept local
  rather than imported across the module boundary, same as this file's
  other small tree-walking helpers" — i.e. this codebase already prefers a
  small duplicated helper over cross-module coupling at this seam.
- `_is_pytest_fixture_decorator` (`extract_relations.py`) has the *exact
  same* alias blind spot for its own decorator-name check (`from pytest
  import fixture as fix` is not recognized) — noted here for awareness,
  **explicitly out of scope for this fix** (a different call site, a
  different file, not part of what H3's review flagged).

## Locked decisions (confirmed by user, 2026-09-06)

1. **Mechanism: self-contained local alias walk in `extract_symbols.py`,
   not a shared/threaded map.** `extract_symbols.py` gains its own small,
   private import-alias helper — walks `import_from_statement` nodes
   (mirroring the shape of `extract_relations.py`'s
   `_handle_import_from_statement`, but scoped down to only what's needed:
   `bound_name -> original imported name`, no module tracking, no relation
   building) and resolves `_terminal_name(base)` through it before the
   `"Protocol"`/`"ABC"`/`"ABCMeta"` membership checks and before
   `_has_abstractmethod_decorator`'s `"abstractmethod"` check. **No public
   signature change** to `extract_symbols()` — it stays `(path, source_text,
   observed_at)`, no caller (`indexer.py`) changes needed. Rejected
   alternative: threading a shared alias map in from `extract_relations.py`
   — real logic reuse, but changes `extract_symbols`'s public signature and
   pipeline call order in `indexer.py`, a materially bigger architectural
   change for a narrow-impact bug fix.
2. **No import-source verification.** The alias resolves purely by bound
   name, regardless of which module it was imported from (a hypothetical
   `from somewhere_else import Whatever as Protocol` would still trip the
   Protocol-context check, exactly as an unaliased, unimported same-named
   local class already does today via the unaliased path). Matches the
   existing risk tier exactly — requiring module verification only on the
   aliased path would be a strictly *more* verified, inconsistent bar than
   the unaliased path already clears. This mirrors H3's own Decision 6
   precedent (`6525407d`): accept name-match risk without building
   semantic-source verification machinery for a case unobserved as an
   actual false-positive in practice.
3. **Scope: exactly the four existing literal-name checks** —
   `"Protocol"`, `"ABC"`, `"ABCMeta"`, `"abstractmethod"` — get
   alias-resolved. `_is_pytest_fixture_decorator`'s sibling gap
   (`extract_relations.py`) is explicitly NOT touched by this fix.

## Design sketch (non-binding on exact code shape, binding on behavior)

Something along these lines inside `extract_symbols.py`:

```python
def _local_import_aliases(root) -> dict[str, str]:
    """bound_name -> original imported name, from-imports only, this file's
    own minimal mirror of extract_relations._handle_import_from_statement --
    kept local per this codebase's small-helper convention (see
    _unwrap_decorated), scoped to name resolution only (no module tracking,
    no relation building -- this function builds neither).
    """
    aliases: dict[str, str] = {}

    def walk(node) -> None:
        if node.type == "import_from_statement":
            for name_node in node.children_by_field_name("name"):
                if name_node.type != "aliased_import":
                    continue
                imported = name_node.child_by_field_name("name")
                alias = name_node.child_by_field_name("alias")
                if imported is not None and imported.type == "dotted_name" and alias is not None:
                    aliases[alias.text.decode("utf-8")] = imported.text.decode("utf-8")
        for child in node.named_children:
            walk(child)

    walk(root)
    return aliases
```

Then in `_class_contexts`/`_base_names`'s callers and
`_has_abstractmethod_decorator`, resolve each candidate name through
`aliases.get(name, name)` before comparing against the four literal
targets. Exact plumbing (where the alias dict gets built once and threaded
through `_class_contexts`/`_is_stub_method`/`_has_abstractmethod_decorator`
without changing `extract_symbols`'s own public signature) is an
implementation detail for the implementing session — `extract_symbols`
already computes `class_contexts = _class_contexts(root)` once up front
(line 37), so `_local_import_aliases(root)` can be computed alongside it at
that same call site and threaded down as an internal parameter, same
pattern as `class_contexts` itself.

## Test plan (write first, red before green)

- `from typing import Protocol as P; class Foo(P): def bar(self) -> int: ...`
  → `bar` symbol has `is_stub=True`.
- `from abc import abstractmethod as am; class Base(ABC):` with `@am` on a
  stub-bodied method → `is_stub=True`.
- `from abc import ABCMeta as Meta; class Base(metaclass=Meta):` with
  `@abstractmethod` on a stub-bodied method → `is_stub=True` (keyword-arg
  metaclass path, not base-list path — confirm `_has_abc_meta`'s existing
  `metaclass=` handling gets the same alias treatment).
- Regression: existing unaliased Protocol/ABC/abstractmethod tests in
  `tests/adapters/python/test_extract_symbols.py` must keep passing
  unchanged.
- Negative control: `from typing import Protocol as P` present in the file,
  but an unrelated method decorated `@P` or subclassing an unrelated `P`
  that does NOT trace back to a real `aliased_import` — should not exist as
  a realistic case given tree-sitter's grammar, but confirm no accidental
  over-match against a same-named local class/decorator that happens to
  collide with an alias bound elsewhere in the file.

## Files to touch

- `src/acie/adapters/python/extract_symbols.py` — the alias-walk helper and
  its threading into `_class_contexts`/`_is_stub_method`/
  `_has_abstractmethod_decorator`.
- `tests/adapters/python/test_extract_symbols.py` — new tests per the plan
  above.
- No other file should need to change (`indexer.py`, `extract_relations.py`,
  `lsp_enrichment.py`, `merge_policy.py`, `symbol_store.py` are all
  untouched by this fix per Decision 1).

## Verification note

Run `.venv/bin/python -m pytest -q` (the project's actual venv — plain
`python3 -m pytest` fails to collect, `acie` isn't installed on system
Python) before and after; baseline at spec-write time is 818/818 passing.
