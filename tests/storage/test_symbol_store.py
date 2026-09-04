from acie.ir.symbol import Confidence, Provenance, Symbol
from acie.storage.symbol_store import SymbolStore


def make_symbol(**overrides) -> Symbol:
    defaults = dict(
        id="pkg/mod.py:Foo.bar#method",
        path="pkg/mod.py",
        qualname="Foo.bar",
        kind="method",
        start_line=10,
        start_col=4,
        end_line=12,
        end_col=8,
        confidence=Confidence.EXTRACTED,
        provenance=Provenance(
            provider="tree-sitter", version="0.21.0", observed_at="2026-08-31T00:00:00Z"
        ),
    )
    defaults.update(overrides)
    return Symbol(**defaults)


def test_upsert_then_get_round_trips_symbol():
    store = SymbolStore(":memory:")
    symbol = make_symbol()

    store.upsert(symbol)

    assert store.get(symbol.id) == symbol


def test_conn_kwarg_reuses_an_already_open_connection_instead_of_opening_its_own():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    writer = SymbolStore(conn=conn)
    symbol = make_symbol()
    writer.upsert(symbol)

    # A second store built on the *same* conn must see the first store's
    # write -- proving no separate sqlite3.connect() happened under the
    # hood (a fresh :memory: connection would see an empty db instead).
    reader = SymbolStore(conn=conn)

    assert reader.get(symbol.id) == symbol


def test_first_upsert_creates_one_history_observation():
    store = SymbolStore(":memory:")
    symbol = make_symbol()

    store.upsert(symbol)

    assert store.history(symbol.id) == [symbol]


def test_reupsert_with_identical_content_does_not_grow_history():
    store = SymbolStore(":memory:")
    symbol = make_symbol()
    store.upsert(symbol)

    reobserved = make_symbol(
        provenance=Provenance(
            provider="tree-sitter", version="0.21.0", observed_at="2026-08-31T01:00:00Z"
        )
    )
    store.upsert(reobserved)

    assert store.get(symbol.id) == reobserved
    assert store.history(symbol.id) == [symbol]


def test_reupsert_with_changed_span_appends_second_history_entry():
    store = SymbolStore(":memory:")
    symbol = make_symbol()
    store.upsert(symbol)

    moved = make_symbol(
        start_line=20,
        end_line=22,
        provenance=Provenance(
            provider="tree-sitter", version="0.21.0", observed_at="2026-08-31T01:00:00Z"
        ),
    )
    store.upsert(moved)

    assert store.get(symbol.id) == moved
    assert store.history(symbol.id) == [symbol, moved]


def test_delete_hard_deletes_live_row_and_writes_tombstone():
    store = SymbolStore(":memory:")
    symbol = make_symbol()
    store.upsert(symbol)

    store.delete(symbol.id, observed_at="2026-08-31T02:00:00Z")

    assert store.get(symbol.id) is None
    assert store.history(symbol.id) == [symbol]
    assert store.is_tombstoned(symbol.id)


def test_list_by_path_returns_only_live_symbols_for_that_path():
    store = SymbolStore(":memory:")
    same_path = make_symbol(id="pkg/mod.py:Foo.bar#method")
    other_path = make_symbol(id="pkg/other.py:baz#function", path="pkg/other.py", qualname="baz", kind="function")
    store.upsert(same_path)
    store.upsert(other_path)

    assert store.list_by_path("pkg/mod.py") == [same_path]


def test_search_matches_qualname_substring_case_sensitively():
    store = SymbolStore(":memory:")
    matching = make_symbol(id="pkg/mod.py:foobar#function", path="pkg/mod.py", qualname="foobar", kind="function")
    non_matching = make_symbol(id="pkg/mod.py:baz#function", path="pkg/mod.py", qualname="baz", kind="function")
    store.upsert(matching)
    store.upsert(non_matching)

    assert store.search(qualname_substring="oob") == [matching]
    assert store.search(qualname_substring="OOB") == []


def test_search_orders_results_by_id_ascending():
    store = SymbolStore(":memory:")
    b = make_symbol(id="pkg/mod.py:bfoo#function", path="pkg/mod.py", qualname="bfoo", kind="function")
    a = make_symbol(id="pkg/mod.py:afoo#function", path="pkg/mod.py", qualname="afoo", kind="function")
    store.upsert(b)
    store.upsert(a)

    assert store.search(qualname_substring="foo") == [a, b]


def test_search_kind_filter_excludes_non_matching_kinds():
    store = SymbolStore(":memory:")
    function_sym = make_symbol(id="pkg/mod.py:foo#function", path="pkg/mod.py", qualname="foo", kind="function")
    class_sym = make_symbol(id="pkg/mod.py:foo#class", path="pkg/mod.py", qualname="foo", kind="class")
    store.upsert(function_sym)
    store.upsert(class_sym)

    assert store.search(qualname_substring="foo", kind="class") == [class_sym]


def test_search_path_glob_filter_scopes_to_matching_files():
    store = SymbolStore(":memory:")
    in_pkg = make_symbol(id="pkg/mod.py:foo#function", path="pkg/mod.py", qualname="foo", kind="function")
    elsewhere = make_symbol(id="other/mod.py:foo#function", path="other/mod.py", qualname="foo", kind="function")
    store.upsert(in_pkg)
    store.upsert(elsewhere)

    assert store.search(qualname_substring="foo", path_glob="pkg/*") == [in_pkg]


def test_find_by_qualname_and_kind_matches_exactly_across_files():
    store = SymbolStore(":memory:")
    in_pkg = make_symbol(id="pkg/mod.py:helper#function", path="pkg/mod.py", qualname="helper", kind="function")
    elsewhere = make_symbol(
        id="other/mod.py:helper#function", path="other/mod.py", qualname="helper", kind="function"
    )
    store.upsert(in_pkg)
    store.upsert(elsewhere)

    assert set(store.find_by_qualname_and_kind(qualname="helper", kind="function")) == {in_pkg, elsewhere}


def test_find_by_qualname_and_kind_is_exact_not_a_substring_match():
    store = SymbolStore(":memory:")
    store.upsert(make_symbol(id="pkg/mod.py:helper_extra#function", path="pkg/mod.py", qualname="helper_extra", kind="function"))

    assert store.find_by_qualname_and_kind(qualname="helper", kind="function") == []


def test_find_by_qualname_and_kind_filters_by_kind():
    store = SymbolStore(":memory:")
    function_sym = make_symbol(id="pkg/mod.py:foo#function", path="pkg/mod.py", qualname="foo", kind="function")
    class_sym = make_symbol(id="pkg/mod.py:foo#class", path="pkg/mod.py", qualname="foo", kind="class")
    store.upsert(function_sym)
    store.upsert(class_sym)

    assert store.find_by_qualname_and_kind(qualname="foo", kind="function") == [function_sym]


def test_at_start_returns_the_symbol_whose_start_position_matches_exactly():
    store = SymbolStore(":memory:")
    symbol = make_symbol()
    store.upsert(symbol)

    assert store.at_start(path=symbol.path, line=symbol.start_line, col=symbol.start_col) == symbol


def test_at_start_returns_none_when_no_symbol_starts_at_that_exact_position():
    store = SymbolStore(":memory:")
    store.upsert(make_symbol())

    assert store.at_start(path="pkg/mod.py", line=999, col=0) is None

def test_at_position_returns_a_single_symbol_at_its_exact_start():
    store = SymbolStore(":memory:")
    symbol = make_symbol()
    store.upsert(symbol)

    assert store.at_position(path=symbol.path, line=symbol.start_line, col=symbol.start_col) == symbol


def test_at_position_returns_the_smallest_symbol_containing_the_position():
    store = SymbolStore(":memory:")
    outer = make_symbol(
        id="pkg/mod.py:Outer#class",
        qualname="Outer",
        kind="class",
        start_line=1,
        start_col=0,
        end_line=20,
        end_col=0,
    )
    method = make_symbol(
        id="pkg/mod.py:Outer.method#method",
        qualname="Outer.method",
        kind="method",
        start_line=5,
        start_col=4,
        end_line=10,
        end_col=0,
    )
    store.upsert(outer)
    store.upsert(method)

    assert store.at_position(path="pkg/mod.py", line=6, col=8) == method


def test_at_position_falls_back_to_the_module_when_no_definition_contains_the_position():
    store = SymbolStore(":memory:")
    module = make_symbol(
        id="pkg/mod.py:#module",
        qualname="",
        kind="module",
        start_line=1,
        start_col=0,
        end_line=100,
        end_col=0,
    )
    store.upsert(module)

    assert store.at_position(path="pkg/mod.py", line=50, col=0) == module


def test_at_position_returns_none_for_a_different_path_or_outside_every_span():
    store = SymbolStore(":memory:")
    store.upsert(make_symbol())

    assert store.at_position(path="pkg/other.py", line=10, col=4) is None
