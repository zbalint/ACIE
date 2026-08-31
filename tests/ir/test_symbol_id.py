import pytest

from acie.ir.symbol_id import SymbolIdParts, build_symbol_id, parse_symbol_id


def test_build_symbol_id_without_ordinal_has_no_trailing_at():
    symbol_id = build_symbol_id(
        path="pkg/mod.py", qualname="Foo.bar", kind="method"
    )
    assert symbol_id == "pkg/mod.py:Foo.bar#method"


def test_build_symbol_id_with_ordinal_appends_at_ordinal():
    symbol_id = build_symbol_id(
        path="pkg/mod.py", qualname="Foo.bar", kind="method", ordinal=2
    )
    assert symbol_id == "pkg/mod.py:Foo.bar#method@2"


def test_parse_symbol_id_round_trips_without_ordinal():
    parts = parse_symbol_id("pkg/mod.py:Foo.bar#method")
    assert parts == SymbolIdParts(
        path="pkg/mod.py", qualname="Foo.bar", kind="method", ordinal=None
    )


def test_parse_symbol_id_round_trips_with_ordinal():
    parts = parse_symbol_id("pkg/mod.py:Foo.bar#method@2")
    assert parts == SymbolIdParts(
        path="pkg/mod.py", qualname="Foo.bar", kind="method", ordinal=2
    )


def test_parse_symbol_id_rejects_malformed_string_missing_hash():
    with pytest.raises(ValueError, match="symbol id"):
        parse_symbol_id("pkg/mod.py:Foo.bar")
