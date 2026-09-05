# Capability F, Slice F1 — Cross-Module Attribute-Call Resolution

Status: spec-and-plan only, written for external implementation (same role
split as C/D/E — this document was **not** implemented in the session that
wrote it; a fresh session should implement it, and another fresh session
should review the resulting diff against this spec before commit).

**Implementation method: TDD.** The implementing session must use this
workspace's `tdd` skill (red-green-refactor, one vertical slice at a time,
testing at the public seams named below — `extract_relations_with_deferred_edges`,
`index_file`/`unresolved_deferred_sites`) rather than writing the
implementation first and tests after. This is a standing instruction for
this slice, not a suggestion.

## What this is, and why it's a new capability letter, not a reopened one

This is not from the locked v1 A–D capability breakdown (SALTMDB memory
`3627eece`) or Capability E's recurring-trigger work — it originates
directly from a live dogfooding finding made against the just-updated
`~/.mcp/ACIE` install (SALTMDB memory `00e6f064`, 2026-09-05). No existing
open capability naturally owns it: Capability A closed on
inheritance/overrides, B on affected-tests, C on architecture queries, D on
pyright/LSP enrichment, E on recurring re-enrichment triggers — none of
those is "core tree-sitter `calls`-relation extraction correctness," which
is what this slice actually fixes. Per the precedent Capability E's own
spec already set (`8096afab`/E1's header: "new Capability E ..., not D7 —
re-opening a closed capability"), this gets its own new letter, **F**,
rather than being awkwardly folded into A (whose own scope was specifically
inheritance/overrides, not calls) or D (whose scope is the LSP enrichment
layer, not the tree-sitter static layer this bug lives in).

## The bug this closes

`mcp__acie__find_references` / `explain` against `src/acie/scan.py:run_scan#function`
report zero incoming `calls` relations of any confidence — despite
`src/acie/cli.py` (`from acie import scan` at the top of the file, then
`scan.run_scan(path)` inside `_scan()`) being a real, currently-shipping
caller. Root-caused in `src/acie/adapters/python/extract_relations.py`'s
`_call_and_reference_relations` (lines 188–220): the `attribute`-call branch
only ever produces an edge for the literal `self.<method>()` form. Any other
attribute-access call — including `<name>.<attr>()` where `<name>` is a
plain identifier imported via `from <module_path> import <name>` — falls
through with no `else`/`elif` arm at all. The function's own docstring
(lines 128–136) claims such forms "stay explicitly deferred," but no
`DeferredImportCall`-equivalent is ever constructed for this shape — the
call is silently dropped, not deferred, which is a docstring/code mismatch,
not a documented limitation.

## What this session verified live against actual source (not guessed)

### 1. `import_alias_map` already carries exactly the information needed — the gap is purely in how the `attribute` branch consumes it

`_call_and_reference_relations`'s bare-call branch (lines 190–206) already
does `elif name in import_alias_map: deferred.append(DeferredImportCall(...))`
for a bare `helper()` call where `from pkg.other import helper`.
`import_alias_map` is built once per file (per the module docstring at line
811) and maps *any* `from X import Y` name — including `from acie import
scan`, where `scan` denotes a submodule, not a plain function/class — to
its source `module_path`. The attribute branch has full access to this same
map (it's a parameter of the enclosing function) but never consults it for
anything but the `self` case.

### 2. `DeferredImportCall`'s existing one-step resolution (`name` directly in `module_path`) cannot be reused unmodified — this needs a two-step lookup, precedented by `DeferredImportOverride`

Read `ir/relation.py` and `indexer.py` in full. `DeferredImportCall`
resolves `name` as a symbol imported *directly* from `module_path` (`from
pkg.other import helper` → look up `helper` as a top-level function in
whatever file `pkg.other` resolves to). That is the wrong resolution for
`scan.run_scan()`: `scan` is not a function imported from `acie` — it
**is** the submodule `acie.scan`, and `run_scan` needs to be looked up as a
top-level symbol *inside that submodule's own file*, not inside `acie`
itself. This is structurally identical to `DeferredImportOverride`'s
already-solved two-step problem (`_resolve_deferred_overrides`,
`indexer.py` lines 161–225): resolve the base name to a class symbol first,
then look up the method *within that specific class's file*. F1's case
swaps "class → method" for "module → top-level symbol," reusing the
identical shape, not inventing a new one.

### 3. `module_paths.path_to_dotted`/`module_path_matches` (already built for C1's `architecture` tool) is the exact primitive this needs, with zero new SymbolStore surface

`module_path_matches(candidate_file_path, imported_module_path)` already
answers "does this dotted string resolve to this file?", suffix-tolerant
against an arbitrary src-layout (module docstring, `module_paths.py` lines
18–27). For `scan.run_scan()`, the submodule's dotted path is simply
`f"{module_path}.{name}"` (`"acie" + "." + "scan"` = `"acie.scan"`) —
verified against `src/acie/scan.py`'s own `path_to_dotted` derivation
(`"src/acie/scan.py"` → `"src.acie.scan"`, which `.endswith(".acie.scan")`
via the suffix rule). Resolution becomes: `[s for s in
symbol_store.find_by_qualname_and_kind(qualname=attribute_name,
kind="function") if module_path_matches(s.path, f"{module_path}.{name}")]`
— the exact same `find_by_qualname_and_kind` + `module_path_matches` pair
`_candidates_for`/`_resolve_deferred_overrides` already use, no new storage
method, no new dependency direction (`indexer.py` already imports
`module_path_matches`; `module_paths.py`'s own docstring already names
`indexer.py` as one of its two intended callers).

### 4. `unresolved_deferred_sites` must also be updated, or attribute-form calls silently skip D3's pyright enrichment fallback

`indexer.py`'s `unresolved_deferred_sites` feeds `lsp_enrichment.py`'s
`run_enrichment_pass` (`lsp_enrichment.py` lines 128–130): every call site
this function reports as unresolved becomes a candidate `textDocument/definition`
site for pyright. Today `unresolved_deferred_sites` computes "unresolved"
via `_candidates_for(item, symbol_store, kind="function")`, which — if left
unmodified and simply pointed at an attribute-carrying `DeferredImportCall`
— would incorrectly test `module_path_matches(candidate.path,
item.module_path)` (`"acie"`, not `"acie.scan"`), the wrong dotted string
for this shape. This needs the same two-step check the resolver itself
uses, factored into one shared helper, so a genuinely-unresolvable
attribute call (e.g. the submodule really is external, or not yet indexed)
still gets a shot at pyright enrichment exactly like an unresolved bare
deferred call does today — F1 does not silently narrow D3's existing
enrichment coverage.

### 5. The existing regression test for "attribute call on a non-imported name" already codifies the *correct* unchanged behavior — confirmed this fix cannot break it

`tests/adapters/python/test_extract_relations.py::test_non_self_attribute_call_still_produces_no_edge`
(line 444) uses `other.callee()` where `other` is a plain method
**parameter** (`def caller(self, other): other.callee()`) — never a
`from`-imported name, so it is never in `import_alias_map` regardless of
this fix. This test must and will continue to pass completely unmodified:
F1's new branch is gated on `object_name in import_alias_map`, which
`other` never satisfies. Confirmed by reading the test directly, not
inferred from its name alone.

### Baseline reconfirmed this session

`.venv/bin/pytest -q` → **764 passed, 3 failed** (the same pre-existing
election-port/MCP-transport flake tracked since slice A1, unchanged:
`test_daemon_start_spawns_a_daemon_and_stop_shuts_it_down`,
`test_daemon_stop_actually_terminates_the_os_process`,
`test_serve_mcp_exposes_and_routes_the_ten_tools`).

## Scope (narrow, deliberately not F2)

One new optional field on the existing `DeferredImportCall` dataclass, one
new `elif` arm in `_call_and_reference_relations`'s attribute branch, one
new resolver function in `indexer.py` mirroring
`_resolve_deferred_overrides`'s existing two-step shape, and a matching
update to `unresolved_deferred_sites`. No new predicate, no new MCP tool, no
schema/storage change (`Relation`'s existing `(source, target, predicate,
site_file, site_line, site_col)` primary key and `calls` predicate are
reused as-is — this produces ordinary `calls` edges, EXTRACTED or AMBIGUOUS
exactly like every other resolved call site, indistinguishable at query
time from one the bare-call path resolved).

### What F1 deliberately does not do (named explicitly, not silently dropped)

- **`ClassName.method()` through an imported class is not built here.**
  The extraction-side AST shape is identical (an `attribute` call on a
  plain imported identifier), but the resolution is a *different* two-step
  (base name → **class** symbol, then `attribute_name` → a **method**
  qualname within that specific class's file, mirroring
  `_resolve_deferred_overrides`'s own base→method lookup rather than F1's
  module→top-level-symbol lookup) — a second, structurally distinct
  resolver, not a one-line variant of F1's. No live call site in this
  codebase currently hits this shape (confirmed: the only attribute-call
  gap this session found live was the submodule form). Left as a named,
  natural follow-on (F2) rather than built speculatively now.
- **Multi-level dotted attribute chains** (`pkg.sub.func()`, where the
  `attribute` call's `object` field is itself an `attribute` node, e.g.
  `import pkg.sub` then `pkg.sub.func()`) are out of scope — the extraction
  branch this fix touches only ever matches `object_node.type ==
  "identifier"` (unchanged condition), so a nested-attribute object falls
  through exactly as it does today, silently producing no edge. Not
  regressed, not fixed — a `import pkg.sub`-style plain import never
  populates `import_alias_map` in the first place (see
  `test_call_to_a_name_from_a_plain_import_statement_is_not_deferred`),
  so this case was never reachable through F1's new branch regardless.
- **Instance-attribute calls on a locally-constructed object**
  (`x = Foo(); x.method()`) remain unresolved, same as today — `x` is never
  in `import_alias_map` (it's an assignment target, not an import), so
  F1's new `elif` branch is never reached for it. Genuinely needs real
  type inference (pyright's job, not tree-sitter's), unchanged philosophy.
- **`references` (assignment RHS) gets no attribute-form treatment.**
  Matches the existing, already-accepted asymmetry ("Only `calls` gets this
  treatment ... `references` is untouched," `_call_and_reference_relations`
  docstring, unchanged by F1) — no live need surfaced, not built
  speculatively.
- **No change to `DeferredImportInherit`/`DeferredImportOverride`.** Both
  already only ever originate from a class's `superclasses` field
  (necessarily a bare identifier in a base-class list, never an attribute
  expression) — there is no `Base.attr` class-list shape in Python syntax
  for this fix to apply to.

## Design decisions

1. **Add an optional `attribute` field to `DeferredImportCall`
   (`ir/relation.py`).**

   ```python
   @dataclass(frozen=True)
   class DeferredImportCall:
       """A bare `name(...)` call, OR a `name.attribute(...)` call where
       `name` is imported (`from module_path import name`) in this file --
       extract_relations (single-file, pure) can go no further than naming
       the calling symbol and the imported name/module it came from (plus,
       for the attribute form, the attribute name itself). Cross-file
       resolution against the repo-wide symbol index happens in
       indexer.py: `attribute is None` resolves `name` as a symbol imported
       directly from `module_path`; `attribute is not None` resolves `name`
       as a SUBMODULE of `module_path` first, then `attribute` as a
       top-level symbol within that submodule's own file (mirroring
       DeferredImportOverride's base-class-then-method two-step). One that
       stays unresolved either way (target file not yet indexed, or
       genuinely external, or `name` doesn't actually denote a submodule)
       simply produces no edge, same as an undefined name today.
       """

       source: str
       module_path: str
       name: str
       site_file: str
       site_line: int
       site_col: int
       provenance: Provenance
       attribute: str | None = None
   ```

   Placed last with a default so every existing call site constructing a
   plain bare-call `DeferredImportCall` (extraction's own bare-call branch,
   every existing test) needs no change at all — purely additive.

2. **`_call_and_reference_relations`'s attribute branch
   (`extract_relations.py`, lines 207–220) gains one `elif`.**

   ```python
   elif function_node is not None and function_node.type == "attribute":
       object_node = function_node.child_by_field_name("object")
       attribute_node = function_node.child_by_field_name("attribute")
       if object_node is not None and object_node.type == "identifier" and attribute_node is not None:
           object_name = object_node.text.decode("utf-8")
           if object_name == "self" and current_class is not None:
               candidates = methods_by_class.get(current_class.qualname, {}).get(
                   attribute_node.text.decode("utf-8"), []
               )
               resolve(attribute_node, source=current_source, candidates=candidates, predicate="calls")
           elif object_name in import_alias_map:
               deferred.append(
                   DeferredImportCall(
                       source=current_source.id,
                       module_path=import_alias_map[object_name],
                       name=object_name,
                       attribute=attribute_node.text.decode("utf-8"),
                       site_file=path,
                       site_line=attribute_node.start_point.row + 1,
                       site_col=attribute_node.start_point.column,
                       provenance=provenance,
                   )
               )
   ```

   Site position is `attribute_node`'s own start (the `run_scan` token
   after the dot), matching the existing `self.method()` branch's own
   choice (`resolve(attribute_node, ...)`) rather than the object's
   position — consistent site-location convention across both attribute
   sub-cases. `object_name == "self"` is checked before the
   `import_alias_map` branch so a shadowing edge case (a file that somehow
   both has a `self`-named import and a class using literal `self`) can
   never resolve ambiguously between the two branches — `self` inside a
   method body always means "this instance," never an import, so checking
   it first is strictly correct, not just ordering-convenient.

3. **New `indexer.py` resolver, `_resolve_deferred_attribute_calls`,
   mirroring `_resolve_deferred_overrides`'s two-step shape.**

   ```python
   def _attribute_call_candidates(item: DeferredImportCall, symbol_store: SymbolStore) -> list[Symbol]:
       """Shared by resolution and unresolved-site detection (finding 4) --
       mirrors _candidates_for's role for the plain-name case. `item.attribute`
       must be non-None; the submodule dotted path is module_path + "." + name.
       """
       submodule_dotted = f"{item.module_path}.{item.name}"
       return [
           symbol
           for symbol in symbol_store.find_by_qualname_and_kind(qualname=item.attribute, kind="function")
           if module_path_matches(symbol.path, submodule_dotted)
       ]

   def _resolve_deferred_attribute_calls(
       deferred_items: list[DeferredImportCall], symbol_store: SymbolStore
   ) -> list[Relation]:
       relations: list[Relation] = []
       for item in deferred_items:
           candidates = _attribute_call_candidates(item, symbol_store)
           if not candidates:
               continue
           confidence = Confidence.EXTRACTED if len(candidates) == 1 else Confidence.AMBIGUOUS
           for candidate in candidates:
               relations.append(
                   Relation(
                       source=item.source, target=candidate.id, predicate="calls",
                       site_file=item.site_file, site_line=item.site_line, site_col=item.site_col,
                       confidence=confidence, provenance=item.provenance,
                   )
               )
       return relations
   ```

   `index_file` partitions `deferred_calls` before resolving, so the
   existing `_resolve_deferred`/`_candidates_for` path (unchanged) never
   sees an attribute-form item and vice versa:

   ```python
   plain_deferred_calls = [c for c in deferred_calls if c.attribute is None]
   attribute_deferred_calls = [c for c in deferred_calls if c.attribute is not None]
   new_relations = new_relations + _resolve_deferred(
       plain_deferred_calls, symbol_store, kind="function", predicate="calls"
   )
   new_relations = new_relations + _resolve_deferred_attribute_calls(
       attribute_deferred_calls, symbol_store
   )
   ```

4. **`unresolved_deferred_sites` uses the same partition + the new shared
   `_attribute_call_candidates` helper (finding 4).**

   ```python
   def unresolved_deferred_sites(
       deferred_calls: list[DeferredImportCall],
       deferred_inherits: list[DeferredImportInherit],
       symbol_store: SymbolStore,
   ) -> UnresolvedSites:
       plain_calls = [c for c in deferred_calls if c.attribute is None]
       attribute_calls = [c for c in deferred_calls if c.attribute is not None]
       return UnresolvedSites(
           calls=(
               [item for item in plain_calls if not _candidates_for(item, symbol_store, kind="function")]
               + [item for item in attribute_calls if not _attribute_call_candidates(item, symbol_store)]
           ),
           inherits=[item for item in deferred_inherits if not _candidates_for(item, symbol_store, kind="class")],
       )
   ```

   `_Site`/`lsp_enrichment.py` itself needs no change — it already consumes
   `UnresolvedSites.calls` generically by `(source, site_file, site_line,
   site_col)`, with no awareness of *why* an item is unresolved.

## Files to touch

1. `src/acie/ir/relation.py` — add `DeferredImportCall.attribute: str | None = None` (design decision 1).
2. `src/acie/adapters/python/extract_relations.py` — restructure the
   attribute-call branch of `_call_and_reference_relations` (design
   decision 2); no signature change to `extract_relations`/
   `extract_relations_with_deferred_edges` (still a 4-tuple; the new
   information rides inside the existing `deferred_calls` list).
3. `src/acie/indexer.py` — add `_attribute_call_candidates`,
   `_resolve_deferred_attribute_calls` (design decision 3); update
   `index_file`'s deferred-calls resolution to partition-and-combine
   (design decision 3); update `unresolved_deferred_sites` (design
   decision 4).
4. `tests/adapters/python/test_extract_relations.py` — new scenarios:
   `scan.run_scan()`-shaped attribute call on a `from`-imported submodule
   name produces a `DeferredImportCall` with `attribute="run_scan"`,
   `name="scan"`, `module_path` equal to the package it was imported from
   (mirrors `test_call_to_a_name_imported_from_another_module_is_deferred_not_dropped`'s
   own shape/assertions, extended with the new field); confirm
   `extract_relations` (the public, non-deferred entry point) still omits
   these entirely, matching
   `test_extract_relations_public_function_omits_deferred_calls_entirely`'s
   existing convention; confirm
   `test_non_self_attribute_call_still_produces_no_edge` (finding 5) passes
   completely unmodified, as an explicit regression proof, not just by
   omission.
5. `tests/test_indexer.py` — new scenarios mirroring the existing
   deferred-override two-step tests: a submodule-attribute call resolves
   across files once both files are indexed (order-independence, same as
   every other deferred-edge case already tested here); an attribute call
   on an imported name that is *not* actually a submodule (i.e., a plain
   function/class import used as if it were a module — `from pkg import
   helper` then `helper.something()`) produces no edge, not a false
   candidate; `unresolved_deferred_sites` reports an attribute-form item
   as unresolved before its target file is indexed, and no longer reports
   it once the target file is indexed (mirrors the existing plain-call
   unresolved/resolved pair of tests).
6. `tests/daemon/test_lsp_enrichment.py` — one new scenario confirming an
   unresolved attribute-form site reaches `run_enrichment_pass`'s
   candidate-site set exactly like an unresolved plain deferred call does
   today (regression proof for finding 4 — this is the one path where a
   silent gap here would be easy to miss without a direct test, since
   `lsp_enrichment.py` itself needs no code change).
7. `ARCHITECTURE.md` — no change. The `calls` predicate and its
   EXTRACTED/AMBIGUOUS confidence semantics are unchanged; this slice
   extends *coverage* of an existing predicate, the same category of
   change C1–C6/D1–D6/E1 already made without touching this document's
   "Canonical IR / Data Model" section (that section documents the
   predicate vocabulary and confidence taxonomy, not exhaustive per-syntax-form
   extraction coverage).
8. `DAEMON.md` — no change. This slice touches only the tree-sitter static
   extraction layer (`extract_relations.py`/`indexer.py`), never the
   daemon's process/transport/trigger layer this document describes.

## Workflow constraints carried into the spec

- **Implement with the `tdd` skill (red-green-refactor, one vertical slice
  at a time, testing at the public seams named above)** — restated from
  this document's top since it is a standing instruction for this slice,
  not implicit from precedent.
- omp configuration per the standing, permanently-locked decision (SALTMDB
  memory `ce5ea55e`): implementer and advisor both `gpt-5.6-luna:max`;
  `terra` retained only for prewalk/plan/slow, not this slice's ordinary
  implementation work.
- One slice per session, stop before commit for review (memory `9b020543`).
- Full suite must pass except the 3 known pre-existing failures (baseline
  reconfirmed this session: 764 passed, 3 failed).
- No new runtime dependencies.
- Do not build F2 (`ClassName.method()` resolution through an imported
  class) or multi-level dotted-attribute-chain resolution — both named
  explicitly out of scope above, not silently dropped.
- Do not touch `DeferredImportInherit`/`DeferredImportOverride`, the
  `references` predicate, `runtime.py`, `scan.py`, `mcp_server.py`, or any
  MCP tool surface — this slice is confined to the tree-sitter extraction
  layer and its one indexer-side resolution/unresolved-detection path.
- Never change application code to accommodate an ACIE result beyond what
  this spec itself already specifies — the repository source and this
  spec's own verified findings are what drove this design, not the other
  way around (acie-usage skill's standing rule, restated here since this
  slice's entire motivation is an ACIE dogfooding finding).

## Verification note

This spec was written against the actual current source this session:
`extract_relations.py` (full read of `_call_and_reference_relations` and
its module docstring, plus `_index_top_level_symbols`/`_inherits_relations`/
`_resolve_deferred_overrides`-equivalent context for the two-step precedent),
`ir/relation.py` (full read — all three `DeferredImport*` dataclasses and
their docstrings), `indexer.py` (full read — `index_file`,
`_candidates_for`, `unresolved_deferred_sites`, `_resolve_deferred`,
`_resolve_deferred_overrides`, `_relation_key`), `module_paths.py` (full
read — `path_to_dotted`/`module_path_matches` and their own documented
suffix-tolerance rationale), `storage/symbol_store.py`
(`find_by_qualname_and_kind`'s exact signature and docstring, confirming it
is already documented as existing specifically for this cross-file
resolution purpose), `lsp_enrichment.py` (confirmed `unresolved_deferred_sites`'s
consumption at lines 128–130, confirming no `_Site`-construction change is
needed), `tests/adapters/python/test_extract_relations.py` (full grep +
targeted read of every `self`/attribute/deferred-call test, confirming
`test_non_self_attribute_call_still_produces_no_edge`'s exact scenario
does not overlap this fix's new branch), and a live re-run of
`.venv/bin/pytest -q` this session (764 passed, 3 failed, matching the
baseline named above) — not inferred from an older spec's stated count.

This spec was also verified against a real, live discrepancy — not a
hypothetical — via `mcp__acie__find_references`/`explain` against the
freshly-updated `~/.mcp/ACIE` live install (index_generation 708) failing
to surface `cli.py`'s real `scan.run_scan(path)` call, cross-checked
against `cli.py` source directly (`grep -n "run_scan" src/acie/cli.py`)
before concluding the tool result, not the source, was wrong. Recorded as
SALTMDB memory `00e6f064`, linked to this spec once written.
