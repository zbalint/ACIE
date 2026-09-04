"""architecture: module/package-aggregation MCP tool (v1 capability C,
wayfinder ticket 47d8cd0d).

**Slices C1-C6** -- Capability C is complete: C6 adds the unconditional
`cycles` field (file-granularity iterative Tarjan SCC detection).

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
(C5, below, adds one more parameter, `repo_root`, on top of this locked
signature -- a dispatch-injected seam like `structural_search`'s `files`,
not part of the ticket's original locked shape and never exposed on the
public MCP schema; see the C5 section's own opening paragraph.)
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
   This includes the degenerate case of a file importing from (a name
   defined in) itself: `source == target` is a real self-loop edge, kept
   rather than suppressed -- an earlier draft silently dropped it via a
   `candidate_path != module.path` guard with no recorded rationale for
   the exclusion (caught by review on commit 260caec); self-imports are
   rare but real, and dropping them would misrepresent this file-to-file
   aggregation view and hide a genuine one-node cycle from C6's future
   cycle detection. `full` has no effect on edges -- there is no per-edge
   confidence to reveal (aggregation already discarded the individual site
   info; adding it back would reopen the one-edge-per-statement question
   decision 3 just closed).
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
   truncated, layering_enabled, layer_violations, cycles}` as of C6 --
   nodes/edges/truncated are C2/C3's own; layering_enabled/layer_violations
   are C5's; cycles is C6's) -- same rationale as `affected_tests` echoing
   `root`: unlike
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
10. **An edge whose source and target directory are equal (any import
    between two files that roll up to the same package node -- including a
    single file importing itself) is dropped at package granularity, not
    rendered as a self-loop.** Unlike file granularity (decision 3), which
    keeps a genuine same-file self-import as a real `source == target`
    edge, package granularity's rollup has already merged every file under
    that directory into one node -- an edge between two files it just
    fused together would be a self-loop carrying no information at *this*
    granularity, even though the same underlying relation is informative
    one level down. A deliberately different answer per granularity, not
    an inconsistency: what counts as real information depends on what the
    node represents.
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

## C5: layering-violation detection

`architecture()` gains `repo_root: str | None = None` (dispatch.py injects
it the same way it already resolves one for `structural_search`'s `files`
seam) and two new envelope fields, `layering_enabled: bool` and
`layer_violations: list[dict]`. Like `files`, `repo_root` is a
dispatch-only seam, not a client-facing input: `mcp_server.py`'s
`_DAEMON_INJECTED_PARAMETERS` set names it alongside `symbol_store`/
`relation_store`/`index_meta_store`/`files`/`observed_at`, so the public
MCP schema `_daemon_tool` derives for `architecture` never exposes it
(review finding, this session -- the first cut of this slice added
`repo_root` to `architecture()`'s own signature but missed updating this
separate exclusion set, so it briefly leaked onto the public schema as a
client-settable parameter that dispatch.py would silently overwrite
anyway; a regression test now asserts the public schema is exactly
`{root, granularity, node_cap, full}`, `tests/test_mcp_server.py`).
Layering is opt-in per `acie.layer_config`
(C4): no `.acie/config.json`, or no `repo_root` supplied at all (a direct
caller that doesn't pass one, as opposed to dispatch.py's real injection),
means `layering_enabled=False` and `layer_violations=[]` -- not an error,
the same "absence is a legitimate state" stance C4's own loader already
takes. A malformed `.acie/config.json` raises `InvalidConfigError`
(`errors.py`'s new `INVALID_CONFIG` code, the exact slot C4's own docstring
predicted this tool would need to decide).

12. **Violation detection always classifies at file granularity,
    regardless of the `granularity` the caller requested for `nodes`/
    `edges`.** Resolves follow-up memory `b75c92b3`: `classify_layers` was
    only ever validated against file-shaped paths, and a naturally-
    authored glob like `"pkg/api/*"` does not match the bare directory
    string `"pkg/api"` itself (ordinary shell-glob semantics), so most
    real configs would silently fail to classify a package-granularity
    node. Recomputing file-level edges via `_compute_file_edges` (factored
    out of `_file_granularity` so both share the one relation-walk loop,
    not two copies of it) sidesteps the gap entirely rather than teaching
    `classify_layers` a second path shape. This is also the only level at
    which a violation can name the actual offending file -- memory
    `95ced07b`'s other concern, that a package-level violation can't cite
    a file at all, since package nodes carry no file-membership list.
13. **Violation detection ignores `node_cap` and is computed over the
    full (still `root`-scoped) `in_scope` set, not the node_cap-truncated
    slice either granularity branch renders.** `node_cap` bounds response
    *size* for rendered nodes/edges, a display concern; a layering policy
    check silently missing a real violation because the caller asked for
    a small page of nodes would be a worse failure mode than the extra
    work of walking every in-scope file. `root` scoping still applies,
    since `in_scope` is already root-filtered before either granularity
    branch runs.
14. **One violation entry per disallowed `(from_layer, to_layer)`
    combination, fanning out over every classification pairing when
    either edge endpoint matches more than one declared layer** -- the
    same "report every real combination, never silently fold to one
    guess" stance `classify_layers` (C4) and `_resolve_import_target`'s
    ambiguous-import fan-out (C1/C2) already take, extended one layer up.
    An edge where either endpoint matches zero declared layers is skipped
    (outside layering scope, not a violation and not an error). A
    same-layer pairing is never flagged (`is_dependency_allowed`'s
    same-layer-always-allowed rule) -- including within a same-file
    self-import edge classified into more than one layer, where a
    disallowed combination among that file's own multiple classifications
    is still a real, reportable violation even though `source == target`.

## C6: cycle detection

15. **Cycles always use file-granularity edges**, regardless of the requested
    node/edge `granularity`. Package rollups can create reciprocal directory
    edges without any file-level loop, so only file-level SCCs mean a real
    import cycle.
16. **`cycles` is unconditional in the response envelope.** It needs no
    layer configuration; `layering_enabled=False` does not suppress it.
17. **Cycles ignore `node_cap`** and use the full root-scoped `in_scope`
    graph. A display cap must not hide a real cycle; root scope still
    applies because out-of-scope endpoints never enter the edge set.
18. **Cycle discovery is iterative Tarjan SCC**, O(V+E), rather than a
    recursive DFS so a long acyclic import chain cannot exceed Python's
    recursion limit. `_find_cycles` stays local to this module: no second
    ACIE caller needs general SCC machinery.
19. **Each entry names complete non-trivial SCC membership.** A component
    with at least two files is a cycle; a singleton is one only when its
    file-edge self-loop exists. This reports every member rather than
    selecting an arbitrary simple-cycle path from a larger SCC.
20. **Ordering is deterministic:** sort nodes inside each component, then
    sort cycle entries by their first node path.
21. **`full` has no effect on cycles:** SCC membership has no confidence or
    provenance to expose.
22. **No new errors or inputs** are needed; cycle detection operates solely
    on data `architecture()` already indexes.
23. **The full file-edge computation is hoisted into `architecture()` and
    shared** by C2/C3 rendering, C5 layer-violation detection, and C6 cycle
    detection. Each consumer filters that one result to its own display or
    policy scope without revisiting import relations; layering still returns
    immediately without iterating edges when it is not configured.
"""

from acie.layer_config import LayerConfig, classify_layers, is_dependency_allowed, load_layer_config
from acie.module_paths import path_to_dotted
from acie.storage.index_meta_store import IndexMetaStore
from acie.storage.relation_store import RelationStore
from acie.storage.symbol_store import SymbolStore
from acie.tools.errors import InvalidArgumentError, InvalidConfigError
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
    repo_root: str | None = None,
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

    file_edges, external_dependency_count = _compute_file_edges(
        in_scope, relation_store, dotted_name_index,
    )
    node_paths = {module.path for module in in_scope}

    if granularity == "file":
        nodes, edges, truncated = _file_granularity(
            in_scope, file_edges, external_dependency_count, node_cap, full,
        )
    else:
        nodes, edges, truncated = _package_granularity(
            in_scope, file_edges, external_dependency_count, node_cap,
        )

    layer_config = _load_layer_config_or_raise(repo_root)
    layer_violations = _detect_layer_violations(file_edges, layer_config)
    cycles = _find_cycles(file_edges, node_paths)
    return {
        "index_generation": index_generation,
        "root": root,
        "granularity": granularity,
        "nodes": nodes,
        "edges": edges,
        "node_cap": node_cap,
        "truncated": truncated,
        "layering_enabled": layer_config is not None,
        "layer_violations": layer_violations,
        "cycles": [{"nodes": cycle} for cycle in cycles],
    }


def _compute_file_edges(scoped_modules, relation_store, dotted_name_index):
    """Compute full root-scoped file edges and external-import counts once.

    C2/C3 rendering, C5 layer-violation detection, and C6 cycle detection
    all consume this result; the renderers filter it to their capped display
    views without revisiting `RelationStore`.
    """
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
                if candidate_path in node_paths:
                    edges.add((module.path, candidate_path))
        external_dependency_count[module.path] = count
    return edges, external_dependency_count


def _find_cycles(file_edges: set[tuple[str, str]], node_paths: set[str]) -> list[list[str]]:
    """Return sorted membership lists for every non-trivial file SCC.

    Iterative Tarjan DFS avoids recursion-depth failures on long import
    chains. Initializing every `node_paths` member makes the explicit
    singleton-exclusion check below cover isolated files too.
    """
    adjacency: dict[str, list[str]] = {path: [] for path in node_paths}
    for source, target in file_edges:
        adjacency[source].append(target)

    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    active: set[str] = set()
    component_stack: list[str] = []
    cycles: list[list[str]] = []
    next_index = 0

    for start in adjacency:
        if start in indices:
            continue
        indices[start] = lowlinks[start] = next_index
        next_index += 1
        active.add(start)
        component_stack.append(start)
        dfs_stack = [(start, iter(adjacency[start]))]

        while dfs_stack:
            node, neighbors = dfs_stack[-1]
            try:
                neighbor = next(neighbors)
            except StopIteration:
                dfs_stack.pop()
                if lowlinks[node] == indices[node]:
                    component = []
                    while True:
                        member = component_stack.pop()
                        active.remove(member)
                        component.append(member)
                        if member == node:
                            break
                    if len(component) > 1 or (node, node) in file_edges:
                        cycles.append(sorted(component))
                if dfs_stack and node in active:
                    parent, _ = dfs_stack[-1]
                    lowlinks[parent] = min(lowlinks[parent], lowlinks[node])
                continue

            if neighbor not in indices:
                indices[neighbor] = lowlinks[neighbor] = next_index
                next_index += 1
                active.add(neighbor)
                component_stack.append(neighbor)
                dfs_stack.append((neighbor, iter(adjacency[neighbor])))
            elif neighbor in active:
                lowlinks[node] = min(lowlinks[node], indices[neighbor])

    return sorted(cycles, key=lambda cycle: cycle[0])


def _file_granularity(in_scope, file_edges, external_dependency_count, node_cap, full):
    truncated = len(in_scope) > node_cap
    scoped_modules = in_scope[:node_cap]
    scoped_paths = {module.path for module in scoped_modules}
    edges = {
        (source, target)
        for source, target in file_edges
        if source in scoped_paths and target in scoped_paths
    }

    nodes = []
    for module in scoped_modules:
        item = render_symbol(module, full=full)
        item["external_dependency_count"] = external_dependency_count[module.path]
        nodes.append(item)

    return nodes, [{"source": s, "target": t} for s, t in sorted(edges)], truncated


def _package_granularity(in_scope, file_edges, file_external_dependency_count, node_cap):
    module_dir = {module.path: _package_dir(module.path) for module in in_scope}
    all_dirs = sorted(set(module_dir.values()))
    truncated = len(all_dirs) > node_cap
    scoped_dirs = set(all_dirs[:node_cap])

    external_dependency_count: dict[str, int] = {directory: 0 for directory in scoped_dirs}
    for module in in_scope:
        source_dir = module_dir[module.path]
        if source_dir in scoped_dirs:
            external_dependency_count[source_dir] += file_external_dependency_count[module.path]

    edges = {
        (module_dir[source], module_dir[target])
        for source, target in file_edges
        if module_dir[source] in scoped_dirs
        and module_dir[target] in scoped_dirs
        and module_dir[source] != module_dir[target]
    }
    nodes = [
        {"path": directory, "kind": "package", "external_dependency_count": external_dependency_count[directory]}
        for directory in sorted(scoped_dirs)
    ]
    return nodes, [{"source": source, "target": target} for source, target in sorted(edges)], truncated


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


def _load_layer_config_or_raise(repo_root: str | None) -> LayerConfig | None:
    """`None` when `repo_root` itself is `None` (no caller-supplied repo
    root at all -- dispatch.py always injects one via `resolve_repo_root`,
    but `architecture()` is a plain pure function, so a direct caller that
    doesn't pass `repo_root` gets the same "layering not opted into"
    behavior as a repo with no `.acie/config.json`, not an error) or when
    `.acie/config.json` doesn't exist (layering genuinely not opted in,
    per `load_layer_config`'s own contract). A malformed config re-raises
    as `InvalidConfigError` -- `.acie/config.json`'s own loader (C4)
    raises a plain `ValueError` on purpose (errors.py's docstring: codes
    are added alongside the tool that first needs them, not declared
    speculatively ahead of C4), and this is that tool.
    """
    if repo_root is None:
        return None
    try:
        return load_layer_config(repo_root)
    except ValueError as exc:
        raise InvalidConfigError(str(exc)) from exc


def _detect_layer_violations(file_edges, layer_config):
    """Layering-violation detection (C5), always over the supplied full
    root-scoped file-granularity edge set, regardless of the caller's
    `granularity` or `node_cap`:

    - **Always file granularity, never package.** Memory `b75c92b3`: a
      naturally-authored layer glob like `"pkg/api/*"` matches every file
      under `pkg/api` but does NOT match the bare directory string
      `"pkg/api"` itself (ordinary shell-glob semantics -- a `*` after `/`
      needs something after it). `classify_layers` was only ever validated
      against file-shaped paths, so calling it with a package-granularity
      directory path would silently fail to classify most real configs.
      The shared full edge walk now happens unconditionally in
      `architecture()` for C6 cycle detection; when `layer_config` is
      `None`, this function still skips all violation-detection work and
      returns `[]` without iterating `file_edges`.
    - **Still respects `root`.** `in_scope` is already root-filtered by
      the time this runs (`architecture()` computes it once for both
      granularities); an edge with either endpoint outside `root`'s scope
      was never a candidate node in the first place, so it can't appear in
      the file-edge set this walks.

    Returns one violation dict (`{source, target, from_layer, to_layer}`)
    per disallowed `(from_layer, to_layer)` combination found on an edge --
    fanning out over every classification pairing when either endpoint
    matches more than one declared layer, the same "report every real
    combination, never silently fold to one guess" stance `classify_
    layers` (C4) and `_resolve_import_target`'s ambiguous-import fan-out
    (C1/C2) both already take. An edge where either endpoint matches zero
    declared layers is skipped entirely (outside layering scope, not a
    violation and not an error -- `classify_layers`'s own contract). A
    same-layer pairing is never flagged (`is_dependency_allowed`'s
    same-layer-always-allowed rule), including within a same-file
    self-import edge classified into more than one layer: a disallowed
    combination among that file's own multiple classifications is still a
    real, reportable violation even though `source == target`.
    """
    if layer_config is None:
        return []

    violations = []
    for source, target in sorted(file_edges):
        source_layers = classify_layers(layer_config, source)
        target_layers = classify_layers(layer_config, target)
        for from_layer in source_layers:
            for to_layer in target_layers:
                if not is_dependency_allowed(layer_config, from_layer, to_layer):
                    violations.append(
                        {"source": source, "target": target, "from_layer": from_layer, "to_layer": to_layer}
                    )
    return violations
