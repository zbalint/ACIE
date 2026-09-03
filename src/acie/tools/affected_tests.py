"""affected_tests: static call-graph reachability from pytest-convention tests.

See ARCHITECTURE.md "MCP Tool Surface" and wayfinder ticket df13991a's
resolution. Note on provenance: the ticket's own SALTMDB entry preserved
only its Question (no resolution body was ever appended to that memory) --
the actual resolution survives in the recovered slice-breakdown event
(memory 3627eece) and the v1 design spec map's "Decisions so far" summary
(memory 5d8fa498), both cited below, and this module follows those.

Seam decisions confirmed by that resolution (not independently re-derived):

1. New dedicated MCP tool `affected_tests(root, node_cap, depth_clamp,
   full)`. Built with its OWN traversal internals, not shared with
   impact_analysis/graph -- per this project's established "wait for a
   real 2nd caller before generalizing" norm (already applied once when
   impact_analysis itself was kept independent of graph.py).
2. **Predicate set: {"calls", "overrides"} only** -- no `imports` (a test
   exercises a symbol by calling it or by overriding it in a subclass/
   fixture, not by importing it) and no `inherits`/`references`/`defines`
   (same exclusions impact_analysis/graph already apply).
3. **No new predicate or table for test-to-symbol coverage.** Test
   identification is a query-time pattern match against the already-
   resolved symbols the BFS discovers, hardcoded to the pytest convention
   for v1: `test_*.py`/`*_test.py` file paths, `test_*` function/method
   qualnames. A configurable glob override via `.acie/config.json` was
   surfaced as a possible follow-up but explicitly left unspecified/non-
   blocking for v1 (wayfinder map 5d8fa498's "Not yet specified").
4. Real coverage-data ingestion (`.coverage`/`coverage.json`) is a later
   upgrade, not v1's bar -- this tool is static-reachability-only and never
   presented as ground truth: AMBIGUOUS-confidence discovery (e.g. through
   a dynamically dispatched call) is surfaced via `test_summary`'s tier
   breakdown exactly like impact_analysis's `impact_summary`, not hidden
   or filtered out of the traversal.

**Local decisions, flagged (not literally specified by the ticket
resolution -- decided the same way impact_analysis's own local defaults
were, following that file as the closest sibling tool):**

- **Envelope shape**: `{index_generation, root, affected_tests,
  test_summary, node_cap, depth_clamp, truncated}` -- mirrors
  impact_analysis's shape (`affected_symbols`->`affected_tests`,
  `impact_summary`->`test_summary`), since both tools are bounded-BFS
  blast-radius queries with a confidence-tier-broken-out count and no
  `edges` list.
- **node_cap/depth_clamp bound the full underlying BFS, not just the
  test-identified subset.** A non-test intermediate caller (e.g. a plain
  helper function between `root` and the test that ultimately calls it)
  is still visited and still counts against `node_cap` -- otherwise a
  test reachable only through several non-test hops could never be found
  once node_cap is exhausted by nodes that were never going to be emitted
  anyway. Only test-identified nodes are emitted in `affected_tests`; the
  rest are traversal-only.
- **A node's test/non-test classification requires a resolved symbol.**
  An unresolved leaf (relation source not in `symbol_store`, same
  defensive case impact_analysis/graph already handle) has no kind/path/
  qualname to classify against, so it can never be emitted in
  `affected_tests` -- it is still traversed onward like any other node
  since the graph may route through it to a real test.
- **Test identification requires BOTH the file-path convention AND the
  qualname convention**, not either alone: `kind` must be `"function"` or
  `"method"`, `path`'s basename must match `test_*.py` or `*_test.py`,
  and `qualname`'s final dotted segment (the bare function/method name,
  correctly handling `TestCase.test_foo`-style unittest methods) must
  start with `test_`. A helper named `test_helper` outside a test file, or
  a non-`test_`-named function inside a test file (e.g. a fixture or
  assertion helper), is real pytest behavior that would not be collected
  as a test either -- matching that, not a looser heuristic.
- **`discovery_predicate` per affected test, unconditional** -- same
  rationale/status as impact_analysis's A4 field: traversal metadata, not
  the symbol's own data, and there's no `edges` list to read it from
  otherwise.
- **Direction is fixed upstream, not a parameter** -- same as
  impact_analysis: "which tests are affected by changing root" means
  walking inbound `{calls, overrides}` edges (root is the relation's
  target).
- **`_DEFAULT_NODE_CAP = 100`, `_DEFAULT_DEPTH_CLAMP = 5`** -- same local
  v0/v1 default values as graph.py/impact_analysis.py.
"""

from acie.ir.relation import Relation
from acie.ir.symbol import Confidence
from acie.pytest_conventions import is_test_file_path, is_test_qualname
from acie.storage.index_meta_store import IndexMetaStore
from acie.storage.relation_store import RelationStore
from acie.storage.symbol_store import SymbolStore
from acie.tools.errors import InvalidArgumentError, SymbolNotFoundError
from acie.tools.render import render_symbol

_DEFAULT_NODE_CAP = 100
_DEFAULT_DEPTH_CLAMP = 5

_PREDICATES = {"calls", "overrides"}
_TEST_KINDS = {"function", "method"}


def affected_tests(
    symbol_store: SymbolStore,
    relation_store: RelationStore,
    index_meta_store: IndexMetaStore,
    root: str,
    node_cap: int = _DEFAULT_NODE_CAP,
    depth_clamp: int = _DEFAULT_DEPTH_CLAMP,
    full: bool = False,
) -> dict:
    # Same non-positive-cap guard as graph.py/impact_analysis.py (LIVE_MCP_
    # QUALIFICATION_REPORT.md, 2026-09-01).
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
    discovery_predicate: dict[str, str] = {}
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
                    discovery_predicate[neighbor_id] = relation.predicate
                    next_frontier.add(neighbor_id)
        frontier = next_frontier
        depth += 1

    if not truncated and frontier:
        # depth_clamp stopped us before expanding this frontier -- only real
        # truncation if it actually had an unrecorded node left to discover
        # (same peek-precision approach as graph.py/impact_analysis.py).
        for current in sorted(frontier):
            if any(relation.source not in nodes for relation in neighbors_of(current)):
                truncated = True
                break

    affected = []
    test_summary = {tier.value: 0 for tier in Confidence}
    for node_id, rendered in nodes.items():
        if node_id == root or not _is_test_node(rendered):
            continue
        item = dict(rendered)
        item["discovery_predicate"] = discovery_predicate[node_id]
        affected.append(item)
        test_summary[discovery_confidence[node_id].value] += 1
    test_summary["total"] = len(affected)

    return {
        "index_generation": index_generation,
        "root": root,
        "affected_tests": affected,
        "test_summary": test_summary,
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


def _is_test_node(rendered: dict) -> bool:
    if not rendered.get("resolved") or rendered["kind"] not in _TEST_KINDS:
        return False
    if not is_test_file_path(rendered["path"]):
        return False
    return is_test_qualname(rendered["qualname"])


def _edge_ordering_key(relation: Relation) -> tuple:
    return (relation.site_file, relation.site_line, relation.site_col, relation.source, relation.target)
