"""graph: unified dependency/call graph traversal MCP tool.

See ARCHITECTURE.md "MCP Tool Surface": `graph(root: symbol_id, graph_type,
direction)` unifies the dependency graph (file/module level, import edges)
and call graph (tree-sitter baseline precision, call edges) into one tool.
Per the cross-cutting pagination rule, graph-shaped tools take a node-cap
plus hard depth-clamp instead of true cursor pagination -- there is no
cursor/next_cursor here at all, only a single bounded BFS per request.

Seam decisions confirmed with the user before writing tests (AskUserQuestion,
all three recommended options chosen):

1. **graph_type -> predicate mapping**: call={"calls"}, dependency=
   {"imports"}. inherits/references/defines are excluded from both -- they
   aren't dependency- or call-shaped edges, and are already reachable via
   find_references/get_definition.
2. **Unresolved import targets**: `imports` relations' target is very often
   a raw dotted-name string, not a resolvable symbol_id (see
   extract_relations.py's `_import_relations` -- an external import has no
   ACIE-tracked definition, so target is literally the dotted-name text,
   e.g. "os.path"). Rather than special-casing this per graph_type, node
   resolution is unified: every node id is looked up in symbol_store; found
   -> full Symbol shape + `resolved: true`; not found -> `{id, resolved:
   false}` leaf. For graph_type="dependency" this means essentially every
   import target renders unresolved in v0 (extract_relations never emits a
   real symbol_id-shaped import target yet) -- an honest reflection of what
   ACIE currently knows, not a special dependency-only code path.
3. **Envelope shape**: `{index_generation, nodes, edges, node_cap,
   depth_clamp, truncated}` -- nodes and edges as two separate deduped
   lists (both cycle-safe, keyed by node id / composite edge key), not one
   flat type-tagged list.

**direction semantics** (not one of the 3 confirmed questions -- there was
no genuine fork here, so this was decided locally rather than asked):
"downstream" walks outbound edges from root (root is the relation's source
-- "what root depends on / calls"); "upstream" walks inbound edges (root is
the relation's target -- "what depends on / calls root"). This mirrors
get_definition/find_references' existing use of RelationStore's
list_by_source/list_by_target in the same source/target sense.

**truncated semantics**: true only if node_cap or depth_clamp actually cut
off a real reachable node/edge that traversal didn't get to record --
node_cap truncation is unambiguous (a new node was dropped on the spot).
depth_clamp truncation is *not* assumed just because the clamp was reached
with a non-empty frontier -- that frontier's own outbound edges are peeked
(one extra, non-recording query per remaining frontier node) to check
whether stopping there actually lost anything, so a depth_clamp that
happens to land exactly on the graph's true diameter still reports
truncated=false.
"""

from acie.ir.relation import Relation
from acie.storage.index_meta_store import IndexMetaStore
from acie.storage.relation_store import RelationStore
from acie.storage.symbol_store import SymbolStore
from acie.tools.errors import InvalidArgumentError, SymbolNotFoundError
from acie.tools.render import render_relation, render_symbol

# Local v0 defaults, not specified in ARCHITECTURE.md -- same status as
# find_symbol/get_definition's _DEFAULT_LIMIT.
_DEFAULT_NODE_CAP = 100
_DEFAULT_DEPTH_CLAMP = 5

_PREDICATES_BY_GRAPH_TYPE = {
    "call": "calls",
    "dependency": "imports",
}
_DIRECTIONS = {"upstream", "downstream"}


def graph(
    symbol_store: SymbolStore,
    relation_store: RelationStore,
    index_meta_store: IndexMetaStore,
    root: str,
    graph_type: str,
    direction: str,
    node_cap: int = _DEFAULT_NODE_CAP,
    depth_clamp: int = _DEFAULT_DEPTH_CLAMP,
    full: bool = False,
) -> dict:
    if graph_type not in _PREDICATES_BY_GRAPH_TYPE:
        raise InvalidArgumentError(
            f"graph_type must be one of {sorted(_PREDICATES_BY_GRAPH_TYPE)}, got {graph_type!r}"
        )
    if direction not in _DIRECTIONS:
        raise InvalidArgumentError(f"direction must be one of {sorted(_DIRECTIONS)}, got {direction!r}")
    # LIVE_MCP_QUALIFICATION_REPORT.md (2026-09-01): root is seeded into
    # `nodes` before any cap check below, so a non-positive node_cap/
    # depth_clamp used to still return the root node, contradicting the cap.
    if node_cap <= 0:
        raise InvalidArgumentError(f"node_cap must be a positive integer, got {node_cap!r}")
    if depth_clamp <= 0:
        raise InvalidArgumentError(f"depth_clamp must be a positive integer, got {depth_clamp!r}")

    predicate = _PREDICATES_BY_GRAPH_TYPE[graph_type]
    index_generation = index_meta_store.current_generation()

    root_symbol = symbol_store.get(root)
    if root_symbol is None:
        raise SymbolNotFoundError(f"no live symbol with id {root!r}")

    nodes = {root: _render_node(root, root_symbol, full=full)}
    edges: list[Relation] = []
    # Disable-and-rerun verification (see completion memory) found this
    # dedup guard is NOT load-bearing by any current test: a node is
    # expanded exactly once ever (added to next_frontier only when
    # is_new_node, and never re-added once present in `nodes`), so a given
    # relation can only ever be discovered from one BFS expansion -- there
    # is no code path today that queries the same (current) twice. Kept
    # anyway as defensive correctness-by-construction against that
    # invariant changing later, not because a counterfactual test forces
    # it -- same honestly-flagged status as structural_search's
    # pattern_index sort tiebreak (66719509).
    seen_edges: set = set()
    truncated = False

    def neighbors_of(current: str) -> list[Relation]:
        if direction == "downstream":
            return relation_store.list_by_source(current, predicates={predicate})
        return relation_store.list_by_target(current, predicates={predicate})

    def neighbor_id_of(relation: Relation) -> str:
        return relation.target if direction == "downstream" else relation.source

    frontier = {root}
    depth = 0
    while depth < depth_clamp and frontier:
        next_frontier: set = set()
        for current in sorted(frontier):
            for relation in sorted(neighbors_of(current), key=_edge_ordering_key):
                neighbor_id = neighbor_id_of(relation)
                is_new_node = neighbor_id not in nodes
                if is_new_node and len(nodes) >= node_cap:
                    truncated = True
                    continue
                edge_key = _edge_key(relation)
                if edge_key not in seen_edges:
                    seen_edges.add(edge_key)
                    edges.append(relation)
                if is_new_node:
                    neighbor_symbol = symbol_store.get(neighbor_id)
                    nodes[neighbor_id] = _render_node(neighbor_id, neighbor_symbol, full=full)
                    next_frontier.add(neighbor_id)
        frontier = next_frontier
        depth += 1

    if not truncated and frontier:
        # depth_clamp stopped us before expanding this frontier -- only
        # real truncation if it actually had something left to discover.
        for current in sorted(frontier):
            found_unrecorded = any(
                _edge_key(relation) not in seen_edges or neighbor_id_of(relation) not in nodes
                for relation in neighbors_of(current)
            )
            if found_unrecorded:
                truncated = True
                break

    return {
        "index_generation": index_generation,
        "nodes": list(nodes.values()),
        "edges": [render_relation(relation, full=full) for relation in edges],
        "node_cap": node_cap,
        "depth_clamp": depth_clamp,
        "truncated": truncated,
    }


def _render_node(node_id: str, symbol, *, full: bool) -> dict:
    if symbol is None:
        return {"id": node_id, "resolved": False}
    item = render_symbol(symbol, full=full)
    item["resolved"] = True
    return item


def _edge_key(relation: Relation) -> tuple:
    return (
        relation.source, relation.target, relation.predicate,
        relation.site_file, relation.site_line, relation.site_col,
    )


def _edge_ordering_key(relation: Relation) -> tuple:
    return (relation.site_file, relation.site_line, relation.site_col, relation.source, relation.target)
