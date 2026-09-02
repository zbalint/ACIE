"""impact_analysis: blast-radius / confidence-tiered impact MCP tool.

See ARCHITECTURE.md "MCP Tool Surface": impact_analysis is kept separate
from `graph` because blast-radius analysis spans both dependency and call
edges simultaneously and doesn't fit cleanly into one `graph_type`. It
returns a capped list of affected symbols plus a tier-broken-out
`impact_summary` count, and uses the same node-cap/depth-clamp shortcut
family as `graph` (no cursor pagination).

Seam decisions confirmed with the user before writing tests
(AskUserQuestion, all four recommended options chosen):

1. **Build independently from graph.py, not a shared extraction.** graph's
   BFS internals were NOT extracted into a shared helper for this slice --
   per this project's established "wait for a real 2nd caller before
   generalizing" norm, and because impact_analysis's shape (multi-predicate
   simultaneous traversal, no edges output, a confidence-tier summary) is
   different enough from graph's single-predicate/dual-direction BFS that
   guessing the right shared abstraction now would risk over-generalizing
   graph.py based on one caller's needs. If the two traversals turn out
   substantially identical after this lands, extract then -- not before.
2. **Predicate set: {"calls", "imports"} only** -- mirrors graph's two
   graph_type predicate mappings combined into one traversal.
   inherits/references/defines stay excluded, same reasoning as graph
   (already reachable via find_references/get_definition).
3. **Direction is fixed upstream, not a parameter.** "Impact of changing
   root" means finding what depends on/calls root -- i.e. what would
   break -- which is inbound edges (root is the relation's target, the
   neighbor is the relation's source). Unlike graph, there is no direction
   parameter at all.
4. **Envelope shape**: {index_generation, root, affected_symbols,
   impact_summary, node_cap, depth_clamp, truncated} -- a capped list of
   affected symbols (resolved/unresolved leaf, same rendering as graph's
   nodes) plus the confidence-tier-broken-out impact_summary count. No
   separate edges list, matching ARCHITECTURE.md's literal wording.

**Local decision, flagged (not one of the 4 confirmed questions -- no real
fork, so decided the same way graph's local defaults/decisions were):**

- **root is excluded from affected_symbols.** Root is the thing being
  changed, not a symbol affected by the change. node_cap still counts root
  as 1 of the cap internally (same mechanic as graph's node dict, which
  always seeds with root) -- this only affects what's *emitted*, not the
  cap arithmetic, so node_cap=N always yields at most N-1 affected symbols.
- **A node's confidence tier in impact_summary is the confidence of the
  edge that first discovered it.** A node is added to `nodes` (and to
  `impact_summary`'s tally) exactly once, on first BFS discovery -- the
  same single-expansion-per-node invariant graph.py already relies on for
  its (non-load-bearing-by-test, kept-defensively) edge dedup. Since a
  symbol can only be discovered once, there is no real "which of several
  tiers should win" question to resolve; the discovering edge's tier is
  the only tier available.
- **No `edges` tracking/dedup at all** (unlike graph.py's `seen_edges`
  set) -- since impact_analysis's envelope never exposes edges, multiple
  call sites into the same node don't need distinguishing; only whether a
  *node* is new matters for both node_cap gating and the depth_clamp peek
  check. This makes impact_analysis's loop body simpler than graph's, not
  a downgrade -- graph's edge-level detail simply isn't part of this
  tool's contract.
- **`_DEFAULT_NODE_CAP = 100`, `_DEFAULT_DEPTH_CLAMP = 5`** -- same local
  v0 default values as graph.py, not specified in ARCHITECTURE.md, same
  "flagged, not asked about" status as every prior numeric default in this
  project.
"""

from acie.ir.relation import Relation
from acie.ir.symbol import Confidence
from acie.storage.index_meta_store import IndexMetaStore
from acie.storage.relation_store import RelationStore
from acie.storage.symbol_store import SymbolStore
from acie.tools.errors import InvalidArgumentError, SymbolNotFoundError
from acie.tools.render import render_symbol

_DEFAULT_NODE_CAP = 100
_DEFAULT_DEPTH_CLAMP = 5

_PREDICATES = {"calls", "imports"}


def impact_analysis(
    symbol_store: SymbolStore,
    relation_store: RelationStore,
    index_meta_store: IndexMetaStore,
    root: str,
    node_cap: int = _DEFAULT_NODE_CAP,
    depth_clamp: int = _DEFAULT_DEPTH_CLAMP,
    full: bool = False,
) -> dict:
    # LIVE_MCP_QUALIFICATION_REPORT.md (2026-09-01): root is seeded into
    # `nodes` before any cap check below, so a non-positive node_cap/
    # depth_clamp used to still return the root node, contradicting the cap.
    if node_cap <= 0:
        raise InvalidArgumentError(f"node_cap must be a positive integer, got {node_cap!r}")
    if depth_clamp <= 0:
        raise InvalidArgumentError(f"depth_clamp must be a positive integer, got {depth_clamp!r}")

    index_generation = index_meta_store.current_generation()

    root_symbol = symbol_store.get(root)
    if root_symbol is None:
        raise SymbolNotFoundError(f"no live symbol with id {root!r}")

    nodes = {root: _render_node(root, root_symbol, full=full)}
    discovery_confidence: dict[str, Confidence] = {}
    truncated = False

    def neighbors_of(current: str) -> list[Relation]:
        return relation_store.list_by_target(current, predicates=_PREDICATES)

    frontier = {root}
    depth = 0
    while depth < depth_clamp and frontier:
        next_frontier: set = set()
        for current in sorted(frontier):
            for relation in sorted(neighbors_of(current), key=_edge_ordering_key):
                neighbor_id = relation.source
                is_new_node = neighbor_id not in nodes
                if is_new_node and len(nodes) >= node_cap:
                    truncated = True
                    continue
                if is_new_node:
                    neighbor_symbol = symbol_store.get(neighbor_id)
                    nodes[neighbor_id] = _render_node(neighbor_id, neighbor_symbol, full=full)
                    discovery_confidence[neighbor_id] = relation.confidence
                    next_frontier.add(neighbor_id)
        frontier = next_frontier
        depth += 1

    if not truncated and frontier:
        # depth_clamp stopped us before expanding this frontier -- only
        # real truncation if it actually had an unrecorded node left to
        # discover (same peek-precision approach as graph.py, simplified
        # since there's no edges list to check here -- only node presence
        # matters for this tool's output).
        for current in sorted(frontier):
            if any(relation.source not in nodes for relation in neighbors_of(current)):
                truncated = True
                break

    affected_symbols = [nodes[node_id] for node_id in nodes if node_id != root]

    impact_summary = {tier.value: 0 for tier in Confidence}
    for confidence in discovery_confidence.values():
        impact_summary[confidence.value] += 1
    impact_summary["total"] = len(affected_symbols)

    return {
        "index_generation": index_generation,
        "root": root,
        "affected_symbols": affected_symbols,
        "impact_summary": impact_summary,
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


def _edge_ordering_key(relation: Relation) -> tuple:
    return (relation.site_file, relation.site_line, relation.site_col, relation.source, relation.target)
