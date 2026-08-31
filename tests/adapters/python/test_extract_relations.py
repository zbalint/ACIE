from acie.adapters.python.extract_relations import extract_relations
from acie.ir.symbol import Confidence


def test_module_defines_a_top_level_function():
    source = "def foo():\n    pass\n"

    relations = extract_relations(path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    defines = [r for r in relations if r.predicate == "defines"]
    assert len(defines) == 1
    rel = defines[0]
    assert rel.source == "pkg/mod.py:#module"
    assert rel.target == "pkg/mod.py:foo#function"
    assert rel.site_file == "pkg/mod.py"
    assert rel.site_line == 1
    assert rel.confidence == Confidence.EXTRACTED
    assert rel.provenance.provider == "tree-sitter"


def test_class_defines_its_method_and_module_defines_the_class():
    source = "class Foo:\n    def bar(self):\n        pass\n"

    relations = extract_relations(path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    defines = [r for r in relations if r.predicate == "defines"]
    assert len(defines) == 2
    by_target = {r.target: r for r in defines}
    assert by_target["pkg/mod.py:Foo#class"].source == "pkg/mod.py:#module"
    assert by_target["pkg/mod.py:Foo.bar#method"].source == "pkg/mod.py:Foo#class"


def test_module_imports_a_plain_module():
    source = "import os\n"

    relations = extract_relations(path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    imports = [r for r in relations if r.predicate == "imports"]
    assert len(imports) == 1
    rel = imports[0]
    assert rel.source == "pkg/mod.py:#module"
    assert rel.target == "os"
    assert rel.site_line == 1
    assert rel.confidence == Confidence.EXTRACTED


def test_module_imports_a_name_from_a_module():
    source = "from collections import OrderedDict\n"

    relations = extract_relations(path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    imports = [r for r in relations if r.predicate == "imports"]
    assert len(imports) == 1
    rel = imports[0]
    assert rel.source == "pkg/mod.py:#module"
    assert rel.target == "collections.OrderedDict"
    assert rel.site_line == 1
    assert rel.confidence == Confidence.EXTRACTED


def test_class_inherits_a_base_class_defined_in_the_same_file():
    source = "class Base:\n    pass\n\n\nclass Foo(Base):\n    pass\n"

    relations = extract_relations(path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    inherits = [r for r in relations if r.predicate == "inherits"]
    assert len(inherits) == 1
    rel = inherits[0]
    assert rel.source == "pkg/mod.py:Foo#class"
    assert rel.target == "pkg/mod.py:Base#class"
    assert rel.confidence == Confidence.EXTRACTED


def test_class_inherits_from_an_ambiguous_redefined_base_name():
    source = "class Base:\n    pass\n\n\nclass Base:\n    pass\n\n\nclass Foo(Base):\n    pass\n"

    relations = extract_relations(path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    inherits = [r for r in relations if r.predicate == "inherits"]
    assert len(inherits) == 2
    targets = {r.target for r in inherits}
    assert targets == {"pkg/mod.py:Base#class", "pkg/mod.py:Base#class@2"}
    assert all(r.confidence == Confidence.AMBIGUOUS for r in inherits)
    assert all(r.source == "pkg/mod.py:Foo#class" for r in inherits)


def test_class_inherits_from_a_name_not_defined_in_this_file():
    source = "class Foo(SomeImportedBase):\n    pass\n"

    relations = extract_relations(path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    inherits = [r for r in relations if r.predicate == "inherits"]
    assert inherits == []


def test_module_level_call_to_a_top_level_function():
    source = "def foo():\n    pass\n\n\nfoo()\n"

    relations = extract_relations(path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    calls = [r for r in relations if r.predicate == "calls"]
    assert len(calls) == 1
    rel = calls[0]
    assert rel.source == "pkg/mod.py:#module"
    assert rel.target == "pkg/mod.py:foo#function"
    assert rel.confidence == Confidence.EXTRACTED


def test_call_from_inside_a_function_body_sources_from_that_function():
    source = "def helper():\n    pass\n\n\ndef caller():\n    helper()\n"

    relations = extract_relations(path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    calls = [r for r in relations if r.predicate == "calls"]
    assert len(calls) == 1
    rel = calls[0]
    assert rel.source == "pkg/mod.py:caller#function"
    assert rel.target == "pkg/mod.py:helper#function"


def test_call_to_an_ambiguous_redefined_function_name():
    source = "def foo():\n    pass\n\n\ndef foo():\n    pass\n\n\nfoo()\n"

    relations = extract_relations(path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    calls = [r for r in relations if r.predicate == "calls"]
    assert len(calls) == 2
    targets = {r.target for r in calls}
    assert targets == {"pkg/mod.py:foo#function", "pkg/mod.py:foo#function@2"}
    assert all(r.confidence == Confidence.AMBIGUOUS for r in calls)


def test_call_to_a_name_not_defined_in_this_file_produces_no_edge():
    source = "print('hi')\n"

    relations = extract_relations(path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    calls = [r for r in relations if r.predicate == "calls"]
    assert calls == []


def test_bare_name_reference_on_assignment_rhs():
    source = "def foo():\n    pass\n\n\nx = foo\n"

    relations = extract_relations(path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    references = [r for r in relations if r.predicate == "references"]
    assert len(references) == 1
    rel = references[0]
    assert rel.source == "pkg/mod.py:#module"
    assert rel.target == "pkg/mod.py:foo#function"
    assert rel.confidence == Confidence.EXTRACTED


def test_reference_to_an_ambiguous_redefined_name():
    source = "def foo():\n    pass\n\n\ndef foo():\n    pass\n\n\nx = foo\n"

    relations = extract_relations(path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    references = [r for r in relations if r.predicate == "references"]
    assert len(references) == 2
    assert all(r.confidence == Confidence.AMBIGUOUS for r in references)


def test_reference_to_a_name_not_defined_in_this_file_produces_no_edge():
    source = "x = os\n"

    relations = extract_relations(path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    references = [r for r in relations if r.predicate == "references"]
    assert references == []
