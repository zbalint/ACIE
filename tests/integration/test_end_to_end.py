"""End-to-end integration test closing slice-8 follow-up 3b2c11b8.

All 191 tests up to this point TDD'd each of the 8 MCP tools exclusively
against hand-built in-memory SymbolStore/RelationStore fixtures constructed
directly in each tool's own test file. None of them ran the real pipeline
(extract_symbols/extract_relations -> indexer.index_file -> SymbolStore/
RelationStore) against real Python source and then exercised the tool layer
against that real, indexer-produced state. A bug class per-tool unit tests
structurally cannot catch: a subtle mismatch between what a tool assumes an
indexer-produced Symbol/Relation looks like and what extract_symbols/
extract_relations actually produce.

This file indexes one realistic fixture module exactly once (via the real
on-disk .acie index path, same as test_indexer.py's
test_index_file_persists_to_a_real_on_disk_index_for_a_resolved_repo) and
runs all 8 MCP tools against that single shared state together, cross-
checking their outputs agree with each other where they should (e.g.
graph's call-graph nodes match find_references' usage sites for the same
symbol). Call-site positions used for position-based lookups are read back
from the real indexed RelationStore, not hand-guessed line/column numbers,
so this test can't silently drift from what the indexer actually produces.
"""

import subprocess

from acie.indexer import index_file
from acie.repo_id import resolve_index_db_path
from acie.storage.index_meta_store import IndexMetaStore
from acie.storage.relation_store import RelationStore
from acie.storage.symbol_store import SymbolStore
from acie.tools.explain import explain
from acie.tools.find_references import find_references
from acie.tools.find_symbol import find_symbol
from acie.tools.get_definition import get_definition
from acie.tools.graph import graph
from acie.tools.impact_analysis import impact_analysis
from acie.tools.list_imports import list_imports
from acie.tools.structural_search import structural_search

_OBSERVED_AT = "2026-08-31T00:00:00Z"
_PATH = "pkg/mod.py"

# Exercises: an import (dependency-graph target), a base/derived class pair
# (inherits), two distinct call sites into the same function from two
# different container kinds (a method and a top-level function), and a
# bare-name assignment reference (references) -- one fixture module wide
# enough to touch every v0 predicate (imports, inherits, calls, references,
# defines) and every v0 symbol kind (module, class, method, function).
_SOURCE = (
    "import os\n"
    "\n"
    "class Animal:\n"
    "    def speak(self):\n"
    "        pass\n"
    "\n"
    "class Dog(Animal):\n"
    "    def speak(self):\n"
    "        return bark()\n"
    "\n"
    "def bark():\n"
    "    return \"woof\"\n"
    "\n"
    "def main():\n"
    "    result = bark()\n"
    "    return result\n"
    "\n"
    "sound = bark\n"
)

_MODULE_ID = f"{_PATH}:#module"
_ANIMAL_ID = f"{_PATH}:Animal#class"
_DOG_ID = f"{_PATH}:Dog#class"
_DOG_SPEAK_ID = f"{_PATH}:Dog.speak#method"
_BARK_ID = f"{_PATH}:bark#function"
_MAIN_ID = f"{_PATH}:main#function"


def _indexed_stores(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    db_path = resolve_index_db_path(str(repo), base_dir=str(tmp_path / "acie-home"))

    symbol_store = SymbolStore(db_path)
    relation_store = RelationStore(db_path)
    index_meta_store = IndexMetaStore(db_path)

    result = index_file(
        path=_PATH, source_text=_SOURCE, observed_at=_OBSERVED_AT,
        symbol_store=symbol_store, relation_store=relation_store,
        index_meta_store=index_meta_store,
    )
    assert result.skipped is False, "fixture source must parse cleanly"
    return symbol_store, relation_store, index_meta_store


def test_all_eight_mcp_tools_operate_correctly_against_one_real_indexed_repo(tmp_path):
    symbol_store, relation_store, index_meta_store = _indexed_stores(tmp_path)

    # 1. find_symbol: substring name lookup finds the real indexed function.
    found = find_symbol(symbol_store=symbol_store, index_meta_store=index_meta_store, name="bark")
    assert [r["id"] for r in found["results"]] == [_BARK_ID]

    # 2. get_definition by symbol_id.
    definition = get_definition(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        symbol_id=_BARK_ID,
    )
    assert [r["id"] for r in definition["results"]] == [_BARK_ID]

    # 2b. get_definition by position, resolved against a call site the real
    # indexer produced (not a hand-guessed line/column).
    call_sites = relation_store.list_by_target(_BARK_ID, predicates={"calls"})
    assert len(call_sites) == 2  # Dog.speak's and main's calls to bark()
    a_call_site = call_sites[0]
    by_position = get_definition(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        position={
            "file": a_call_site.site_file,
            "line": a_call_site.site_line,
            "column": a_call_site.site_col,
        },
    )
    assert [r["id"] for r in by_position["results"]] == [_BARK_ID]

    # 3. find_references: defines (1) + calls (2) + references (1, "sound =
    # bark") -- USAGE_PREDICATES includes defines, unlike resolve.py's
    # position-resolution predicate set.
    references = find_references(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        symbol_id=_BARK_ID,
    )
    assert references["total_count"] == 4
    assert {r["predicate"] for r in references["results"]} == {"defines", "calls", "references"}

    # 4. list_imports: the real extract_relations-produced import edge, an
    # unresolved raw dotted-name target (no ACIE-tracked definition for
    # "os").
    imports = list_imports(relation_store=relation_store, index_meta_store=index_meta_store, file=_PATH)
    assert [(r["target"], r["predicate"]) for r in imports["results"]] == [("os", "imports")]

    # 5. structural_search: live tree-sitter query over the same source
    # text, entirely bypassing SymbolStore/RelationStore per its own design.
    search = structural_search(
        files={_PATH: _SOURCE}, index_meta_store=index_meta_store,
        pattern="(class_definition name: (identifier) @class.name)",
        observed_at=_OBSERVED_AT,
    )
    assert {c["captures"]["class.name"][0]["text"] for c in search["results"]} == {"Animal", "Dog"}

    # 6. graph: call graph, upstream from bark ("who calls bark").
    call_graph = graph(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=_BARK_ID, graph_type="call", direction="upstream",
    )
    caller_ids = {n["id"] for n in call_graph["nodes"] if n["id"] != _BARK_ID}
    assert caller_ids == {_MAIN_ID, _DOG_SPEAK_ID}

    # 6b. graph: dependency graph, downstream from the module ("what the
    # module imports") -- the unresolved-leaf path (extract_relations never
    # emits a real symbol_id-shaped import target, per graph.py's own
    # documented seam).
    dependency_graph = graph(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=_MODULE_ID, graph_type="dependency", direction="downstream",
    )
    unresolved = [n for n in dependency_graph["nodes"] if n["id"] != _MODULE_ID]
    assert unresolved == [{"id": "os", "resolved": False}]

    # 7. impact_analysis: blast radius of changing bark -- both of its
    # callers, confidence-tiered.
    impact = impact_analysis(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=_BARK_ID,
    )
    affected_ids = {s["id"] for s in impact["affected_symbols"]}
    assert affected_ids == {_MAIN_ID, _DOG_SPEAK_ID}
    assert impact["impact_summary"]["total"] == 2
    assert impact["impact_summary"]["EXTRACTED"] == 2

    # 8. explain: full observation history, confidence/provenance always
    # shown even at the default full=False (the terse-mode follow-up
    # resolved earlier this session).
    history = explain(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        symbol_id=_BARK_ID,
    )
    assert history["results"][0]["id"] == _BARK_ID
    assert history["results"][0]["confidence"] == "EXTRACTED"
    assert history["results"][0]["provenance"]["provider"] == "tree-sitter"

    # Cross-tool consistency: the inherits edge graph's call/dependency
    # traversal doesn't cover is independently visible via find_references
    # anchored on the base class.
    animal_refs = find_references(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        symbol_id=_ANIMAL_ID,
    )
    assert any(
        r["source"] == _DOG_ID and r["predicate"] == "inherits" for r in animal_refs["results"]
    )
