"""architecture: module/package-aggregation MCP tool (v1 capability C,
wayfinder ticket 47d8cd0d).

**Slices C1-C3 only** -- `.acie/config.json` layering rules (C4/C5) and
cycle detection (C6) are not yet built (see wayfinder map 5d8fa498's
"Decisions so far" / memory 3627eece for the full C1-C6 breakdown).

## The gap C1 closes

`imports` relation edges (see extract_relations.py's `_import_relations`)
keep their target as the raw dotted-name string from the import statement
forever -- nothing in ACIE resolves that string to a file today (the
existing DeferredImportCall/DeferredImportInherit/DeferredImportOverride
machinery resolves *calls*/*inherits*/*overrides*, not plain `imports`
edges, and explicitly disclaims module->file mapping). Classifying an
`imports` edge as internal (this repo) vs external (a third-party/stdlib
dependency) -- the basis for the architecture tool's package graph and its
`external_dependency_count` -- needs that resolution, so C1 builds it as a
one-time index rather than a per-lookup scan.

## C2: the `architecture` tool itself, file granularity only

`architecture(symbol_store, relation_store, index_meta_store, root=None,
granularity="file", node_cap=100, full=False)` -- the ticket resolution
(wayfinder map 5d8fa498) locked this exact signature; `granularity` is
`"file"` (C2) or `"package"` (C3, directory-based rollup, this section).
Unlike graph/impact_analysis/affected_tests, this is not a BFS from an
anchor node: it's a rollup view of the whole (optionally scoped) repo, so
there is no `depth_clamp` parameter, matching the locked signature.

Seam decisions:

1. **`root` is a path-prefix scope, not a BFS anchor** (confirmed with the
   user via AskUserQuestion -- a genuine fork, not decided by precedent:
   graph.py's own root is a symbol_id anchor for a traversal, but nothing
   about this ticket's locked signature settled which reading `architecture`
   should use). `root=None` means the whole repo; `root="pkg/sub"` includes
   every file whose path is `"pkg/sub"` itself or starts with `"pkg/sub/"`
   -- a real path-segment boundary check (`pkg/subx/...` does NOT match
   root `"pkg/sub"`), not a bare `str.startswith`. This also extends
   cleanly to C3's package granularity later (a package root is just
   another path prefix), which the BFS-anchor alternative would not have.
   Filtering happens in Python over every `kind="module"` symbol rather
   than pushing `root` into `SymbolStore.search`'s `path_glob` (SQLite
   GLOB) -- `path_glob` treats `*`/`?`/`[`/`]` as wildcards, which a real
   repo path could legitimately contain, so a hand-rolled prefix check
   avoids misinterpreting those characters.
2. **Nodes are real module symbols, not synthetic file records.** Every
   live `kind="module"` symbol in scope becomes a node via the same
   `render_symbol` helper every other tool uses (`full=True` reveals
   confidence/provenance, same interface as everywhere else -- module
   symbols are always `EXTRACTED`, tree-sitter's module-level parse being
   unambiguous, so this is close to a no-op in practice, same status
   `list_imports` already documents for its own `full`). An
   `external_dependency_count` field is added per node: the number of that
   file's `imports` relations whose target dotted-name has *no* candidate
   in `build_dotted_name_index` at all (genuinely external, not merely
   out-of-scope -- see decision 4).
3. **Edges are deduped file-to-file pairs, not one edge per import
   statement.** This is the "aggregation" the ticket names: two separate
   `from pkg.foo import a, b` names (two symbol-level `imports` relations)
   between the same two files collapse to one `{source, target}` edge.
   `full` has no effect on edges -- there is no per-edge confidence to
   reveal (aggregation already discarded the individual site info; adding
   it back would reopen the one-edge-per-statement question decision 3
   just closed).
4. **An import resolving to a file outside the current `root`/`node_cap`
   scope produces no edge, and does NOT count toward
   `external_dependency_count`.** It genuinely resolved within the repo --
   it's just not part of this particular (possibly scoped, possibly
   capped) view. Silently dropping it (rather than inventing a third
   "in-repo-but-out-of-view" bucket) keeps the two counts honest:
   `external_dependency_count` means "resolves to nothing in this repo",
   full stop; an edge means "both ends are nodes you can actually see in
   this response". Flagged as a known simplification, not treated as a
   design gap needing its own follow-up ticket.
5. **An import resolving ambiguously (`build_dotted_name_index` returns
   more than one candidate) produces an edge to every candidate**, sorted
   for determinism. C1 explicitly left this fold-vs-fan-out choice to C2;
   fanning out (rather than picking one arbitrarily or dropping the edge
   entirely) is the only option that doesn't silently hide or guess at
   which of several same-named files a real import actually resolves to.
6. **`node_cap` truncates the (root-scoped) node list itself**, sorted by
   symbol id ascending (which sorts by path first, same ordering
   `SymbolStore.search` already returns) -- there is no traversal to bound
   the way graph/impact_analysis/affected_tests bound a frontier; capping
   the node list is capping the whole computation here. `truncated=True`
   iff the in-scope node count exceeds `node_cap`, mirroring the other
   graph-shaped tools' `truncated` semantics as closely as this
   non-traversal shape allows.
7. **`root` and `granularity` are echoed in the envelope** (`{
   index_generation, root, granularity, nodes, edges, node_cap,
   truncated}`) -- same rationale as `affected_tests` echoing `root`: unlike
   `graph`'s symbol-id root (which is directly visible as one of the
   returned nodes), a path-prefix `root` is not otherwise recoverable from
   `nodes` alone (an empty-scope result looks identical for any
   non-matching prefix), so the caller needs it echoed back to know what
   was actually queried.

## C3: `granularity="package"`, directory-based rollup

Package nodes are one level up from C2's file nodes: every distinct
directory among in-scope files becomes one node, with per-file edges and
`external_dependency_count` rolled up onto their containing directory.
`root`'s path-prefix scope is unchanged -- it still filters files first
(decision 1 above), exactly as C2 already anticipated ("a package root is
just another path prefix").

8. **A file's package is its own immediate containing directory**
   (`pkg/sub/mod.py` -> `pkg/sub`), not every ancestor up to `root` and not
   a search for the nearest `__init__.py`. This needs no filesystem access
   beyond the file's own path and treats namespace packages (no
   `__init__.py`, valid since Python 3.3) identically to regular ones --
   ACIE has no other place that special-cases `__init__.py` presence, so
   inventing one here would be new machinery for a distinction nothing
   downstream needs yet. A repo-root file with no directory component
   (`mod.py`) rolls up to the empty-string package `""`.
9. **Package nodes are synthetic dicts (`{path, kind: "package",
   external_dependency_count}`), never `render_symbol`.** Unlike C2's file
   nodes, no real Symbol represents "this directory" -- reusing an
   `__init__.py`'s own module symbol (when one exists) would misreport that
   file's own line range as if it were the whole package's, and silently
   break for namespace packages that have no `__init__.py` at all. `full`
   therefore has no effect at package granularity (there is no
   confidence/provenance to reveal), the same no-op status decision 3 gives
   edges at file granularity.
10. **An edge whose source and target directory are equal (an import
    between two files in the same package, including a file importing
    itself) is dropped, not rendered as a self-loop** -- this is decision
    4's own-file exclusion (`candidate_path != module.path`), generalized
    one level: the rollup already merged those files into one node, so an
    edge between them would be a self-loop carrying no information.
11. **`node_cap` truncates the *directory* count, not the underlying file
    count** -- two files sharing one directory are one node against the
    cap, matching decision 6's "capping the node list caps the whole
    computation" for this shape's actual unit of aggregation. Directories
    are sorted lexicographically and the first `node_cap` are kept
    (`truncated=True` iff the full in-scope directory count exceeds it).
    Files whose directory was truncated out of view are excluded from
    *every* downstream computation for the directories that remain in
    view: an import into a truncated-away directory produces no edge and
    does not increment the source directory's `external_dependency_count`
    -- it resolved in-repo, it's just outside this capped view, the exact
    same reasoning as decision 4's root-scope exclusion, now extended to
    the node_cap boundary too (a single "is the target directory in view"
    check serves both cases uniformly).
"""

from acie.module_paths import path_to_dotted
from acie.storage.index_meta_store import IndexMetaStore
from acie.storage.relation_store import RelationStore
from acie.storage.symbol_store import SymbolStore
from acie.tools.errors import InvalidArgumentError
from acie.tools.render import render_symbol

_DEFAULT_NODE_CAP = 100


def build_dotted_name_index(symbol_store: SymbolStore) -> dict[str, list[str]]:
    """One-time dotted_name -> file_path(s) index over every live
    `kind='module'` symbol, keyed by every dotted *suffix* of each module's
    own `path_to_dotted` derivation (not just its full path) -- this
    generalizes `module_paths.module_path_matches`'s pairwise suffix check
    (already used by indexer.py's cross-file deferred-import resolution)
    into a queryable index, so `from acie.daemon.discovery import x`
    resolves against a candidate registered from `src/acie/daemon/
    discovery.py` without ACIE ever knowing "src/" is a source root -- the
    same source-root-agnostic behavior, just indexed instead of scanned.

    `index.get(dotted_name)` returns the list of candidate file paths for
    that dotted name: absent/empty means external (no match in this repo);
    one entry is an unambiguous internal hit; more than one means the
    dotted name is genuinely ambiguous within this repo (e.g. two
    same-named modules under different roots), mirroring
    indexer.py's own _resolve_deferred ambiguity semantics. Folding
    ambiguous hits into a single internal/external classification is left
    to the `architecture` tool itself (C2) -- this index only reports what
    it finds.

    Reuses `SymbolStore.search` (qualname_substring="" matches every live
    row via SQL LIKE '%%', narrowed to kind="module") rather than adding a
    new store method for "list every module symbol" -- module symbols'
    qualname is always "" (see extract_symbols.py), so this already
    returns exactly the intended set with zero storage-layer changes.
    """
    index: dict[str, list[str]] = {}
    for symbol in symbol_store.search(qualname_substring="", kind="module"):
        parts = path_to_dotted(symbol.path).split(".")
        for start in range(len(parts)):
            suffix = ".".join(parts[start:])
            index.setdefault(suffix, []).append(symbol.path)
    return index


def architecture(
    symbol_store: SymbolStore,
    relation_store: RelationStore,
    index_meta_store: IndexMetaStore,
    root: str | None = None,
    granularity: str = "file",
    node_cap: int = _DEFAULT_NODE_CAP,
    full: bool = False,
) -> dict:
    if granularity not in ("file", "package"):
        raise InvalidArgumentError(f"granularity must be one of ['file', 'package'], got {granularity!r}")
    # Same non-positive-cap guard as graph.py/impact_analysis.py/
    # affected_tests.py (LIVE_MCP_QUALIFICATION_REPORT.md, 2026-09-01).
    if node_cap <= 0:
        raise InvalidArgumentError(f"node_cap must be a positive integer, got {node_cap!r}")

    index_generation = index_meta_store.current_generation()
    dotted_name_index = build_dotted_name_index(symbol_store)

    all_modules = symbol_store.search(qualname_substring="", kind="module")
    in_scope = [module for module in all_modules if _in_scope(module.path, root)]

    if granularity == "file":
        nodes, edges, truncated = _file_granularity(in_scope, relation_store, dotted_name_index, node_cap, full)
    else:
        nodes, edges, truncated = _package_granularity(in_scope, relation_store, dotted_name_index, node_cap)

    return {
        "index_generation": index_generation,
        "root": root,
        "granularity": granularity,
        "nodes": nodes,
        "edges": edges,
        "node_cap": node_cap,
        "truncated": truncated,
    }


def _file_granularity(in_scope, relation_store, dotted_name_index, node_cap, full):
    truncated = len(in_scope) > node_cap
    scoped_modules = in_scope[:node_cap]
    node_paths = {module.path for module in scoped_modules}

    edges: set[tuple[str, str]] = set()
    external_dependency_count: dict[str, int] = {}
    for module in scoped_modules:
        count = 0
        for relation in relation_store.list_by_source(module.id, predicates={"imports"}):
            candidates = _resolve_import_target(relation.target, dotted_name_index)
            if not candidates:
                count += 1
                continue
            for candidate_path in candidates:
                if candidate_path != module.path and candidate_path in node_paths:
                    edges.add((module.path, candidate_path))
        external_dependency_count[module.path] = count

    nodes = []
    for module in scoped_modules:
        item = render_symbol(module, full=full)
        item["external_dependency_count"] = external_dependency_count[module.path]
        nodes.append(item)

    return nodes, [{"source": s, "target": t} for s, t in sorted(edges)], truncated


def _package_granularity(in_scope, relation_store, dotted_name_index, node_cap):
    module_dir = {module.path: _package_dir(module.path) for module in in_scope}
    all_dirs = sorted(set(module_dir.values()))
    truncated = len(all_dirs) > node_cap
    scoped_dirs = set(all_dirs[:node_cap])
    scoped_modules = [module for module in in_scope if module_dir[module.path] in scoped_dirs]

    edges: set[tuple[str, str]] = set()
    external_dependency_count: dict[str, int] = {d: 0 for d in scoped_dirs}
    for module in scoped_modules:
        source_dir = module_dir[module.path]
        for relation in relation_store.list_by_source(module.id, predicates={"imports"}):
            candidates = _resolve_import_target(relation.target, dotted_name_index)
            if not candidates:
                external_dependency_count[source_dir] += 1
                continue
            for candidate_path in candidates:
                candidate_dir = _package_dir(candidate_path)
                if candidate_dir != source_dir and candidate_dir in scoped_dirs:
                    edges.add((source_dir, candidate_dir))

    nodes = [
        {"path": d, "kind": "package", "external_dependency_count": external_dependency_count[d]}
        for d in sorted(scoped_dirs)
    ]
    return nodes, [{"source": s, "target": t} for s, t in sorted(edges)], truncated


def _package_dir(path: str) -> str:
    """A file's package for granularity="package" rollup: its own immediate
    containing directory (`pkg/sub/mod.py` -> `pkg/sub`), not every
    ancestor up to `root` and not a search for the nearest `__init__.py`
    (see architecture()'s module docstring, C3 decision 8). A repo-root
    file with no directory component rolls up to `""`.
    """
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _in_scope(path: str, root: str | None) -> bool:
    if root is None:
        return True
    root = root.rstrip("/")
    return path == root or path.startswith(f"{root}/")


def _resolve_import_target(target: str, dotted_name_index: dict[str, list[str]]) -> list[str]:
    """Resolves an `imports` relation's raw dotted-name target against C1's
    index, closing the gap flagged in follow-up memory 9df2ddc0: extract_
    relations.py's `_import_relations` builds a `from module import name`
    target as `f"{module_dotted}.{name}"` unconditionally, whether `name`
    is a submodule file (`module_dotted.name` IS a real file's dotted path
    -- the plain lookup already resolves it) or a function/class *defined
    inside* `module_dotted`'s own file (the far more common case -- the
    real file is one segment up, so the plain lookup misses and would
    misclassify a genuine internal import as external).

    On a miss, retries once against the dotted name with its last segment
    stripped (`module_dotted.name` -> `module_dotted`); a hit there means
    `name` is a symbol inside that file, so it resolves internally too.

    # shortcut: this single-segment-stripped retry cannot tell a `from x
    # import y` miss (where the fallback is exactly the right fix) apart
    # from a plain `import external.pkg.thing` whose true target is
    # genuinely external and just happens to share a dotted suffix with an
    # internal file one segment shorter -- Relation no longer carries which
    # import-statement grammar produced it, only the finished target
    # string. Real risk is low (the *whole* stripped suffix must coincide,
    # not just the last token) and this mirrors C1's own "index only
    # reports what it finds, folding is the caller's judgment call"
    # stance; upgrade trigger: a real repo demonstrating a false-internal
    # classification from this fallback.
    """
    candidates = dotted_name_index.get(target, [])
    if candidates:
        return candidates
    if "." not in target:
        return []
    return dotted_name_index.get(target.rsplit(".", 1)[0], [])
