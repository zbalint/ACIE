# Capability G, Slice G1 — Import-Statement Extraction Completeness (Relative, Aliased, Function-Local Imports)

Status: spec-and-plan only, written for external implementation (same role
split as C/D/E/F1 — this document was **not** implemented in the session
that wrote it; a fresh session should implement it, and another fresh
session should review the resulting diff against this spec before commit).

**Implementation method: TDD.** The implementing session must use this
workspace's `tdd` skill (red-green-refactor, one vertical slice at a time,
testing at the public seams named below — `extract_relations`,
`extract_relations_with_deferred_edges`) rather than writing the
implementation first and tests after. This is a standing instruction for
this slice, not a suggestion.

## What this is, and why it's a new capability letter, not a reopened one

Per the precedent E1 and F1 already set (a bug that doesn't naturally belong
to any existing letter's own scope gets a new one, rather than being
awkwardly folded into a neighbor): Capability A owns inheritance/overrides
*resolution* semantics, F owns cross-module *attribute-call resolution*
built on top of an assumed-correct `import_alias_map`, C owns `architecture`
query behavior. None of those owns "does `_import_relations` correctly
extract every import statement a real Python file can contain" — the
`imports` relation and `import_alias_map` are both built once, upstream of
A2/A3/F1/C1's own consumption of them, and this bug lives entirely in that
one shared extraction step. New letter, **G**.

## The bug this closes

Found during the first external/adversarial ground-truth validation pass
against SALTMDB (`b79e087e` item 4; SALTMDB memory `20556be1`, linked here).
`_import_relations` (`extract_relations.py`, function starting at line 809)
silently drops three common, idiomatic Python import forms — not as a named
deferral, as an unconditional miss:

1. **Relative imports** (`from . import x`, `from .. import x`, `from .sub import x`) —
   entirely absent from both the `imports` relation list and `import_alias_map`.
2. **Aliased imports** (`from pkg import x as y`, and — found by this
   session, extending the validation memory's own scope, see "Additional
   scope" below — the plain `import pkg as alias` form too) — absent from
   both, even when the underlying module is otherwise a perfectly ordinary
   absolute import.
3. **Function-local imports** (an `import`/`from`-import statement inside a
   function or method body, not at module top level) — absent from both,
   even when absolute and non-aliased, purely because of where the
   statement sits in the tree.

Because `import_alias_map` is consumed as-is by `_call_and_reference_relations`
(F1's cross-module attribute-call resolution), `_inherits_relations` (A2),
and `_overrides_relations` (A3) — none of which re-derive imports
themselves — this single upstream gap silently degrades all three
downstream capabilities for any file using these import styles, without any
of them being individually "wrong." It also explains why `architecture`'s
file-dependency graph (C1) omits real edges for files that only reference
another module via a relative/aliased/local import (confirmed independently
in `20556be1`: `write.py`/`duplicates.py` → `tags.py`/`validation.py`/
`lifecycle.py`/`search_primitives.py` in SALTMDB, real heavy coupling,
zero graph edges).

## What this session verified live against actual source (not guessed)

### 1. Root cause, confirmed against the live tree-sitter-python grammar (0.25.0), not assumed from the memory's prose

`_import_relations` (`extract_relations.py:809`) has three independent
filters that each silently `continue`/drop instead of handling a real node
shape:

- **Line 832**: `for child in root.named_children` — `root` is always
  `tree.root_node`, the whole module (`_extract`, line 59-61 calls
  `_import_relations(tree.root_node, ...)`). A statement nested inside a
  `function_definition`'s `block` is never a *direct* child of the module
  node, so it is never visited at all, regardless of the two checks below.
  Confirmed by parsing `def f():\n    from pkg import x\n` — the
  `import_from_statement` is three levels down
  (`function_definition` → `block` → `import_from_statement`), invisible to
  a flat `named_children` loop.
- **Line 840**: `if module_node.type != "dotted_name": continue` — for
  `from . import x` / `from .. import x` / `from .sub import x`, the
  `module_name` field's node is a `relative_import` (containing an
  `import_prefix` child holding the dots, and, only when a submodule
  follows the dots, a nested `dotted_name` child) — never itself a bare
  `dotted_name` — so every relative import hits this `continue`
  unconditionally. Confirmed against three parsed samples (`from . import
  lifecycle`, `from .. import foo`, `from .sub import x`).
- **Lines 843-847**: `imported_names = [... for name_node in
  child.children_by_field_name("name") if name_node.type == "dotted_name"]`
  — an aliased name (`from pkg import mod as alias`) parses as an
  `aliased_import` node (wrapping its own `dotted_name` + `as` + bound-name
  `identifier` children), not a `dotted_name` directly, so the list
  comprehension's `if` filters it out entirely — not just under the wrong
  key, absent from the list. Confirmed against `from pkg import a as b, c`:
  the comprehension yields only `["c"]`, silently dropping `a as b` from the
  same statement. The identical `aliased_import` shape applies to a plain
  `import pkg as alias` statement's own `name` field (line 834-837's
  `import_statement` branch has the identical, narrower bug — see
  "Additional scope" below).

### 2. Downstream consumers need zero changes — the fix is fully contained to `_import_relations`

`_call_and_reference_relations` (F1's `elif object_name in
import_alias_map`), `_inherits_relations` (A2's `elif base_name in
import_alias_map`), and `_overrides_relations` (A3's same check) all treat
`import_alias_map` as an opaque `{bound_name: absolute_dotted_module_path}`
mapping — none of them care how an entry got there. Once G1 populates the
map correctly for all three missing forms (with the *resolved absolute*
dotted path, not a relative string — see decision 2 below), every
downstream consumer picks up the fix automatically, with no code change of
its own. Confirmed by reading all three functions in full: each does a bare
`in`/`.get()` lookup against the map parameter, never inspects the file's
own import statements again.

### 3. `module_paths.path_to_dotted` is the exact primitive needed to resolve a relative import's dots against the importing file's own path — reused, not reinvented

`path_to_dotted(file_path)` (`module_paths.py:31`) already derives a file's
own fully-qualified dotted name (`/`→`.`, `.py`/`__init__.py` suffix
stripped), suffix-tolerant to source-root layout by design (per its own
module docstring, already the primitive both F1's `module_path_matches`
callers and this fix need). Resolving `from . import x` against
`src/saltmdb/domain/services/memory_service/duplicates.py` is: derive the
*importing file's own containing package* (drop the module's own last
dotted segment, unless the file is itself an `__init__.py`, in which case
`path_to_dotted` already stops at the package), then walk up
`(level - 1)` further segments for `..`/`...`, then append any submodule
text after the dots. Verified by hand against
`duplicates.py:24`'s real statement: package =
`src.saltmdb.domain.services.memory_service`, level 1, no submodule →
`from . import lifecycle` resolves to `imports` target
`src.saltmdb.domain.services.memory_service.lifecycle` — an ordinary
absolute dotted string, resolvable by every existing downstream consumer
with zero changes (decision 2).

### 4. Existing tests already codify the *current* (broken) absence, not a deliberately-designed behavior — confirmed no test asserts relative/aliased/local imports are dropped on purpose

Grepped `tests/adapters/python/test_extract_relations.py` for every
`test_module_imports_*` scenario (lines 33-71): all three
(`test_module_imports_a_plain_module`, `test_module_imports_a_name_from_a_module`,
`test_module_imports_multiple_names_from_a_module`) cover only plain
absolute, non-aliased, module-level forms. No test exercises relative,
aliased, or function-local imports at all — this is an untested gap, not a
guarded contract this fix could regress.

### Baseline reconfirmed this session

`.venv/bin/pytest -q` → matches F1's own recorded baseline (764 passed, 3
pre-existing flakes unrelated to this file — election-port/MCP-transport,
tracked since slice A1).

## Scope

### In scope

1. Relative imports (`from . import x`, `from .. import x`, `from .sub.mod
   import x, y as z`), any depth of leading dots, with or without a
   submodule component, with or without per-name aliasing — resolved to an
   absolute dotted `imports` target and (for aliased/non-aliased `from`-names)
   an `import_alias_map` entry, exactly like an absolute `from`-import
   produces today.
2. Aliased `from`-imports (`from pkg import mod as alias`) — `imports`
   target uses the *actual* imported dotted name (`pkg.mod`, matching what
   a non-aliased `from pkg import mod` already produces — the alias is a
   local binding, not a different import), and `import_alias_map` is keyed
   by the *bound* name (`alias`, not `mod`) → `pkg` (the module the aliased
   name lives in), matching the existing non-aliased entry's own key/value
   shape (`alias_map[name] = module_dotted`).
3. Function-local imports (inside a `function_definition` or method body,
   any nesting depth) — same statement-level handling as module-level,
   attributed to `module.id` as the relation's `source`, matching every
   existing import relation's current (already module-level-only)
   attribution — see decision 3's explicit non-goal about per-function
   scoping.

### Additional scope (found this session, not named in `20556be1`, same root cause and same code path)

4. **Plain aliased imports** (`import pkg as alias`, `import pkg.sub as
   alias`) — the `import_statement` branch (line 833-837) has the identical
   `name_node.type != "dotted_name"` filter, hits the same `aliased_import`
   node shape, and silently drops the entire `imports` relation for the
   statement (not just an alias_map entry — the existing module docstring
   already explains a plain import never contributes to `import_alias_map`
   by design, so only the `imports` relation itself is missing here, not an
   alias-map entry). Included because it is the same tree-sitter node type,
   same file, same one-line root cause as finding 2, and leaving it
   unfixed would be knowingly shipping a visible sibling gap in
   `list_imports`/`architecture`'s import-edge fidelity for no reason. Named
   explicitly here, not silently folded in, per this workspace's "state the
   boundary, don't expand scope invisibly" rule.

### Out of scope (named explicitly, not silently dropped)

- **Per-scope alias shadowing.** `import_alias_map` remains a single flat,
  whole-file map, exactly as it is today for module-level imports (a
  same-named import in two different functions, or a local variable that
  happens to shadow an outer import's bound name, already has no
  scope-awareness in the existing design — this slice extends the same flat
  model to function-local imports, it does not introduce scoping semantics
  that don't exist anywhere else in this file).
- **Star imports** (`from pkg import *`) — a distinct grammar node
  (`wildcard_import`, not `dotted_name`/`aliased_import`) genuinely cannot
  populate a `{bound_name: module}` map (there is no enumerable bound name),
  and is already silently skipped by the existing `children_by_field_name("name")`
  loop (a wildcard import has no `name`-field children at all). Unchanged,
  not a regression risk — confirmed this shape was never assumed handled.
- **`__future__`, conditional (`if`/`try`), and `TYPE_CHECKING`-guarded
  imports.** These parse as ordinary `import_from_statement`/`import_statement`
  nodes wrapped in an `if_statement`/`try_statement` ancestor rather than
  a `function_definition` — decision 3's recursive walk (below) will pick
  these up incidentally as a side effect of walking into nested blocks
  generally, since the fix does not special-case `function_definition`
  specifically, but this was not a targeted goal and is not separately
  tested; noted so the implementer isn't surprised by it, not claimed as a
  designed feature.

## Design decisions

1. **`_import_relations`'s single flat loop becomes a recursive walk over
   the whole tree, collecting `import_statement`/`import_from_statement`
   nodes at any depth**, mirroring the existing recursive-walk shape
   `_call_and_reference_relations` already uses (`walk(node)` calling
   itself over `node.named_children`) rather than inventing a new traversal
   style in the same module.

   ```python
   def _import_relations(
       root, *, module: Symbol, path: str, provenance: Provenance
   ) -> tuple[list[Relation], dict[str, str]]:
       relations: list[Relation] = []
       alias_map: dict[str, str] = {}

       def walk(node) -> None:
           if node.type == "import_statement":
               _handle_import_statement(node, module, path, provenance, relations)
           elif node.type == "import_from_statement":
               _handle_import_from_statement(
                   node, module=module, path=path, provenance=provenance,
                   relations=relations, alias_map=alias_map,
               )
           for child in node.named_children:
               walk(child)

       walk(root)
       return relations, alias_map
   ```

   Two new private helpers (`_handle_import_statement`,
   `_handle_import_from_statement`) hold the per-statement logic below,
   keeping `walk` itself a thin dispatcher — matches this module's existing
   convention of small, single-purpose private functions (`_defines_relations`,
   `_inherits_relations`, etc.) rather than one large function growing a new
   responsibility inline.

2. **A new small helper resolves a relative import's dots against the
   importing file's own path, producing an absolute dotted string** — the
   one genuinely new piece of logic, everything else in this fix is
   AST-shape handling:

   ```python
   def _relative_import_base(path: str, level: int) -> str | None:
       """The absolute dotted package `from <dots> import ...` (no
       submodule) resolves against, given this file's own repo-relative
       `path` and the relative import's dot-count `level` (1 for `.`, 2 for
       `..`, etc.). Returns None if `level` walks above the file's own
       top-level package (a malformed/unresolvable relative import in this
       repo's layout) -- callers must skip the statement entirely in that
       case, matching this module's existing skip-on-unresolvable
       convention (e.g. the plain `import_statement` branch's own
       `if name_node.type != "dotted_name": continue`).
       """
       is_init = path.endswith("/__init__.py") or path == "__init__.py"
       dotted = path_to_dotted(path)
       own_package_parts = dotted.split(".") if is_init else dotted.split(".")[:-1]
       pops = level - 1
       if pops > len(own_package_parts):
           return None
       remaining = own_package_parts[: len(own_package_parts) - pops] if pops else own_package_parts
       return ".".join(remaining)
   ```

   Imported from `module_paths.py` (`from acie.module_paths import
   path_to_dotted`) — `extract_relations.py` does not currently import from
   `module_paths.py`; this is a new, narrow import of one existing function,
   not a new dependency direction (`module_paths.py`'s own docstring already
   describes itself as shared, dependency-free utility code, and
   `extract_relations.py`, unlike `indexer.py`, has no existing reverse
   dependency on it to create a cycle with).

3. **`_handle_import_from_statement` builds each target/alias-map entry
   uniformly, whether the module name is a plain `dotted_name` or a
   `relative_import`, and whether each name is a plain `dotted_name` or an
   `aliased_import`:**

   ```python
   def _handle_import_from_statement(
       child, *, module, path, provenance, relations, alias_map
   ) -> None:
       module_node = child.child_by_field_name("module_name")
       base = _module_base(module_node, path)
       if base is None:
           return
       for name_node in child.children_by_field_name("name"):
           if name_node.type == "dotted_name":
               imported_name = name_node.text.decode("utf-8")
               bound_name = imported_name
           elif name_node.type == "aliased_import":
               imported_name = name_node.named_children[0].text.decode("utf-8")
               bound_name = name_node.children_by_field_name("alias")[0].text.decode("utf-8") \
                   if name_node.child_by_field_name("alias") else None
               # actual field name confirmed against the grammar before
               # implementation -- see verification note; aliased_import's
               # bound-name identifier is its own field, not a bare
               # positional child, mirrored on the existing aliased-import
               # handling this fix adds to _handle_import_statement too.
           else:
               continue
           target = f"{base}.{imported_name}" if base else imported_name
           relations.append(_imports_relation(module, target, child, path, provenance))
           alias_map[bound_name] = base

   def _module_base(module_node, path: str) -> str | None:
       if module_node.type == "dotted_name":
           return module_node.text.decode("utf-8")
       if module_node.type == "relative_import":
           prefix = module_node.child_by_field_name("import_prefix")
           level = sum(1 for c in prefix.children if c.type == ".")
           submodule_node = module_node.child_by_field_name("dotted_name")
           own_base = _relative_import_base(path, level)
           if own_base is None:
               return None
           if submodule_node is None:
               return own_base
           submodule = submodule_node.text.decode("utf-8")
           return f"{own_base}.{submodule}" if own_base else submodule
       return None
   ```

   `_imports_relation` factors the existing `Relation(...)` construction
   (currently inlined in the loop body, lines 853-865) into a one-line-call
   helper shared by both `_handle_import_statement` and
   `_handle_import_from_statement`, since both now build the identical
   shape from a `(module, target, site_node, path, provenance)` tuple —
   this is a real second call site for that construction (not a
   speculative single-use extraction), matching this workspace's "extend,
   don't duplicate" rule.

   **Exact field names for `aliased_import`'s bound-name identifier and
   `relative_import`'s dot-count/submodule fields must be confirmed against
   `tree-sitter-python`'s actual grammar node-types.json (or a REPL parse,
   as this spec's author did informally) before writing this code for
   real** — the snippet above is written from a live parse this session
   (see verification note) but the implementer must re-derive the exact
   field name for the alias identifier (`field_name_for_child`-style lookup
   or `node-types.json`) rather than trust this snippet's field-name guess
   byte-for-byte; the *shape* (three children: original dotted name, `as`
   token, bound identifier) is confirmed, the *field name* for the third
   child was not independently re-verified via the grammar's own
   `node-types.json` in this session, only inferred from a `.children`
   dump — flag this explicitly per the acie-usage skill's trust-boundary
   rule, since this spec's own confidence here is lower than everywhere
   else in this document.

4. **`_handle_import_statement` gains the identical `aliased_import`
   handling** (additional scope, item 4) — target is the alias node's
   original dotted name (`os`, `pkg.sub`), never the bound alias, matching
   plain-import's existing target convention (`os`, not the local name);
   no `alias_map` entry, per the existing, unchanged, explicitly-documented
   design ("a plain `import module` ... never belongs in this map").

## Files to touch

1. `src/acie/adapters/python/extract_relations.py` — restructure
   `_import_relations` into a recursive walk (decision 1); add
   `_relative_import_base`, `_module_base`, `_imports_relation` helpers
   (decisions 2-3); extend the existing `import_statement`/
   `import_from_statement` branches for aliasing (decisions 3-4). New
   top-of-file import: `from acie.module_paths import path_to_dotted`.
   No signature change to `_import_relations`, `extract_relations`, or
   `extract_relations_with_deferred_edges` — still returns the same
   `(relations, alias_map)` / 4-tuple shapes.
2. `tests/adapters/python/test_extract_relations.py` — new scenarios,
   mirroring the existing `test_module_imports_*` trio's exact
   assertion style (source → `imports` list → target/site_line/confidence):
   - `from . import x` (module-level, no submodule, no alias) → target
     resolves to the importing file's own package + `.x`.
   - `from .. import x` and `from .sub import x, y as z` — multi-level and
     submodule-plus-mixed-aliasing in one statement.
   - `from pkg import mod as alias` → one `imports` relation targeting
     `pkg.mod` (not `pkg.alias`), and `import_alias_map["alias"] == "pkg"`
     (exposed via a case that also exercises F1's existing `elif object_name
     in import_alias_map` branch end-to-end, e.g. `alias.func()`, to prove
     the fix is not merely producing the right map shape but actually
     unblocks F1's existing consumer with no change to F1's own code).
   - `import pkg as alias` (additional scope, item 4) → one `imports`
     relation targeting `pkg`, no `alias_map` entry.
   - A `from pkg import x` statement inside a function body → `imports`
     relation present, `source == module.id`, identical shape to a
     module-level import of the same statement text (proves attribution is
     unaffected by nesting depth, per decision 1's non-goal on per-scope
     attribution).
   - A relative import in a file positioned such that the requested level
     would walk above the file's own top-level package →  produces no
     `imports` relation and no `alias_map` entry (graceful skip, decision 2's
     `None` return path), not a crash — this is the one new "malformed
     input" branch this fix introduces and needs its own explicit test,
     unlike every other case which mirrors an already-tested happy path.
   - Regression proof: all three existing `test_module_imports_*` tests
     pass completely unmodified.
3. `tests/adapters/python/test_extract_relations.py` (or a new
   cross-capability regression test alongside F1's existing attribute-call
   tests) — one end-to-end case per import style (relative, aliased,
   function-local) feeding directly into `_call_and_reference_relations`'s
   existing `DeferredImportCall` attribute branch, proving G1 alone (zero
   changes to F1's own code) unblocks all three `20556be1` findings. This
   is the single most important regression proof in this slice: it is what
   turns "the extraction map is now correct" into "the real user-visible
   bug from the validation report is fixed."
4. `ARCHITECTURE.md` — no change. Same reasoning as F1's own file list: the
   `imports` predicate and its confidence semantics are unchanged; this
   slice extends *coverage*, not the data model.
5. `DAEMON.md` — no change, same reasoning as F1.

## Workflow constraints carried into the spec

- **Implement with the `tdd` skill** — restated since it's a standing
  instruction for this slice, not implicit from precedent.
- One slice per session, stop before commit for review (memory `9b020543`).
- Full suite must pass except the 3 known pre-existing failures (baseline
  reconfirmed this session: 764 passed, 3 failed).
- No new runtime dependencies. The one new intra-package import
  (`acie.module_paths.path_to_dotted`) is the only new dependency edge, and
  it is one-directional (`extract_relations.py` → `module_paths.py`, no
  existing edge the other way to create a cycle with — confirmed by reading
  `module_paths.py`'s own docstring, which already names its two existing
  callers as `indexer.py` and `acie.tools.architecture`, neither of which
  imports `extract_relations.py`).
- Do not build the mixin/MRO self-call resolution gap (`febf3e07`) or the
  `architecture(root=...)` absolute-path bug (`5021d1a9`) as part of this
  slice — both are separate, independently-specced fixes (see companion
  specs), deliberately not bundled here even though all three were found in
  the same validation pass.
- Never change SALTMDB (or any other target codebase) to accommodate an
  ACIE finding — the fix belongs entirely in ACIE's own extraction code,
  per the acie-usage skill's standing rule.
- Flag, do not silently resolve, the one lower-confidence detail named in
  design decision 3 (the exact grammar field name for `aliased_import`'s
  bound identifier) — re-derive it against the grammar directly before
  writing the real implementation, rather than trusting this spec's
  from-memory field name.

## Verification note

This spec was written against the actual current source this session: full
read of `_import_relations` and its module-level call site in `_extract`
(`extract_relations.py` lines 45-90, 809-866); full read of
`_call_and_reference_relations`, `_inherits_relations`, `_overrides_relations`
to confirm each consumes `import_alias_map` as an opaque map with no
re-derivation of its own (no code change needed in any of the three); full
read of `module_paths.py` (`path_to_dotted`/`module_path_matches` and their
documented suffix-tolerance rationale, confirming no existing reverse
dependency from `module_paths.py` back onto `extract_relations.py`); grep of
every `test_module_imports_*` scenario in
`tests/adapters/python/test_extract_relations.py` (lines 33-71), confirming
no existing test asserts the currently-broken behavior on purpose; and five
live parses of the real tree-sitter-python 0.25.0 grammar this session
(`from . import lifecycle`, `from .. import foo`, `from pkg import mod as
alias`, `from pkg import a as b, c`, `def f(): from pkg import x`, plus
`import os as o` / `import pkg.sub as s` for the additional-scope item) —
not inferred from the SALTMDB validation memory's prose alone, independently
re-confirmed node-by-node against this session's own parser output.

This spec was also verified against the real, live discrepancy the
validation pass recorded — not a hypothetical: SALTMDB memory `20556be1`'s
four concrete file:line examples (`duplicates.py:24/307`, `write.py:19-20`
+ five call sites, `tools.py:6` + two call sites, `__main__.py:59/62`) were
re-read against this session's own understanding of the grammar to confirm
the proposed fix's resolved dotted paths would actually match what
`find_references`/`get_definition` need to succeed on each one, not just
that `_import_relations` would stop raising/skipping.
