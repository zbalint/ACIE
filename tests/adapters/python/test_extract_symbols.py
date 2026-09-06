from acie.adapters.python.extract_symbols import extract_symbols, has_syntax_error
from acie.ir.symbol import Confidence


def test_extracts_only_a_module_symbol_from_an_empty_file():
    symbols = extract_symbols(path="pkg/mod.py", source_text="", observed_at="2026-08-31T00:00:00Z")

    assert len(symbols) == 1
    module = symbols[0]
    assert module.path == "pkg/mod.py"
    assert module.qualname == ""
    assert module.kind == "module"
    assert module.confidence == Confidence.EXTRACTED
    assert module.provenance.provider == "tree-sitter"
    assert module.provenance.observed_at == "2026-08-31T00:00:00Z"


def test_extracts_a_top_level_function():
    source = "def foo():\n    pass\n"

    symbols = extract_symbols(path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    functions = [s for s in symbols if s.kind == "function"]
    assert len(functions) == 1
    foo = functions[0]
    assert foo.qualname == "foo"
    assert foo.id == "pkg/mod.py:foo#function"
    assert foo.start_line == 1
    assert foo.end_line == 2
    assert foo.confidence == Confidence.EXTRACTED


def test_extracts_a_top_level_class():
    source = "class Foo:\n    pass\n"

    symbols = extract_symbols(path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    classes = [s for s in symbols if s.kind == "class"]
    assert len(classes) == 1
    foo = classes[0]
    assert foo.qualname == "Foo"
    assert foo.id == "pkg/mod.py:Foo#class"
    assert foo.start_line == 1
    assert foo.end_line == 2


def test_extracts_a_method_with_dotted_qualname():
    source = "class Foo:\n    def bar(self):\n        pass\n"

    symbols = extract_symbols(path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    methods = [s for s in symbols if s.kind == "method"]
    assert len(methods) == 1
    bar = methods[0]
    assert bar.qualname == "Foo.bar"
    assert bar.id == "pkg/mod.py:Foo.bar#method"
    assert bar.start_line == 2
    assert bar.end_line == 3
    # the class itself is still extracted independently of its methods
    assert any(s.kind == "class" and s.qualname == "Foo" for s in symbols)


def test_extracts_a_decorated_top_level_function():
    source = "@staticmethod\ndef foo():\n    pass\n"

    symbols = extract_symbols(path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    functions = [s for s in symbols if s.kind == "function"]
    assert len(functions) == 1
    foo = functions[0]
    assert foo.qualname == "foo"
    assert foo.id == "pkg/mod.py:foo#function"
    assert foo.start_line == 2
    assert foo.end_line == 3


def test_extracts_a_decorated_top_level_class():
    source = "@dataclass\nclass Foo:\n    pass\n"

    symbols = extract_symbols(path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    classes = [s for s in symbols if s.kind == "class"]
    assert len(classes) == 1
    assert classes[0].qualname == "Foo"
    assert classes[0].id == "pkg/mod.py:Foo#class"


def test_extracts_a_decorated_method():
    source = "class Foo:\n    @staticmethod\n    def bar():\n        pass\n"

    symbols = extract_symbols(path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    methods = [s for s in symbols if s.kind == "method"]
    assert len(methods) == 1
    assert methods[0].qualname == "Foo.bar"
    assert methods[0].id == "pkg/mod.py:Foo.bar#method"


def test_redefinition_at_same_qualname_gets_an_ordinal_suffix():
    source = "def foo():\n    pass\n\n\ndef foo():\n    pass\n"

    symbols = extract_symbols(path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    functions = [s for s in symbols if s.kind == "function"]
    assert len(functions) == 2
    first, second = functions
    assert first.id == "pkg/mod.py:foo#function"
    assert first.start_line == 1
    assert second.id == "pkg/mod.py:foo#function@2"
    assert second.start_line == 5


def test_has_syntax_error_is_false_for_valid_source():
    assert has_syntax_error("def foo():\n    pass\n") is False


def test_has_syntax_error_is_true_for_an_unterminated_def():
    assert has_syntax_error("def foo(\n") is True

def test_marks_only_protocol_or_abstractmethod_stub_methods():
    source = (
        "from abc import ABC, ABCMeta, abstractmethod\n"
        "from typing import Protocol\n\n"
        "class Contract(Protocol):\n"
        "    def ellipsis(self): ...\n"
        "    def no_op(self):\n"
        "        pass\n"
        "    def documented(self):\n"
        "        \"\"\"docs\"\"\"\n"
        "    def implemented(self):\n"
        "        return 1\n"
        "    def mixed(self):\n"
        "        pass\n"
        "        return 1\n\n"
        "class GenericContract(Protocol[T]):\n"
        "    def generic(self): ...\n\n"
        "class Abstract(ABC):\n"
        "    @abstractmethod\n"
        "    def abstract_ellipsis(self): ...\n"
        "    @abstractmethod()\n"
        "    def abstract_pass(self):\n"
        "        pass\n"
        "    @abstractmethod\n"
        "    def abstract_docs(self):\n"
        "        \"\"\"docs\"\"\"\n"
        "    @abstractmethod\n"
        "    def abstract_implemented(self):\n"
        "        return 1\n\n"
        "class MetaAbstract(metaclass=ABCMeta):\n"
        "    @abstractmethod\n"
        "    def meta(self):\n"
        "        pass\n\n"
        "class Ordinary:\n"
        "    def no_op(self):\n"
        "        pass\n"
        "    @abstractmethod\n"
        "    def decorated_no_op(self):\n"
        "        pass\n"
    )

    symbols = extract_symbols(path="pkg/mod.py", source_text=source, observed_at="2026-09-06T00:00:00Z")

    methods = {symbol.qualname: symbol for symbol in symbols if symbol.kind == "method"}
    assert {name for name, symbol in methods.items() if symbol.is_stub} == {
        "Contract.ellipsis",
        "Contract.no_op",
        "Contract.documented",
        "Abstract.abstract_ellipsis",
        "Abstract.abstract_pass",
        "Abstract.abstract_docs",
        "GenericContract.generic",
        "MetaAbstract.meta",
    }
    assert all(
        not symbol.is_stub
        for name, symbol in methods.items()
        if name in {"Contract.implemented", "Contract.mixed", "Abstract.abstract_implemented", "Ordinary.no_op", "Ordinary.decorated_no_op"}
    )
