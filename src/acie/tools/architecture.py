"""architecture: module/package-aggregation MCP tool (v1 capability C,
wayfinder ticket 47d8cd0d).

**Slice C1 only** -- this module currently holds just the dotted-name index
builder infrastructure the eventual `architecture(root, granularity, ...)`
MCP tool will consume. The tool function itself, its MCP registration in
mcp_server.py, package-directory rollup, `.acie/config.json` layering
rules, and cycle detection are C2-C6, not yet built (see wayfinder map
5d8fa498's "Decisions so far" / memory 3627eece for the full C1-C6
breakdown). No MCP tool is exposed by this module yet.

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
"""

from acie.module_paths import path_to_dotted
from acie.storage.symbol_store import SymbolStore


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
