from acie.adapters.python.extract_relations import extract_relations, extract_relations_with_deferred_edges
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


def test_module_imports_multiple_names_from_a_module():
    source = "from collections import OrderedDict, defaultdict\n"

    relations = extract_relations(path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    imports = [r for r in relations if r.predicate == "imports"]
    assert len(imports) == 2
    targets = {r.target for r in imports}
    assert targets == {"collections.OrderedDict", "collections.defaultdict"}
    assert all(r.site_line == 1 for r in imports)
    assert all(r.confidence == Confidence.EXTRACTED for r in imports)


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


def test_class_inheriting_from_a_name_imported_from_another_module_is_deferred_not_dropped():
    # Mirrors test_call_to_a_name_imported_from_another_module_is_deferred_not_dropped:
    # extract_relations is single-file-scoped and cannot itself resolve a
    # base class imported from elsewhere -- it must be deferred, not
    # silently dropped like a genuinely undefined name would be.
    source = "from pkg.other import Base\n\n\nclass Foo(Base):\n    pass\n"

    relations, deferred_calls, deferred_inherits, deferred_overrides = extract_relations_with_deferred_edges(
        path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z"
    )

    assert [r for r in relations if r.predicate == "inherits"] == []
    assert deferred_calls == []
    assert len(deferred_inherits) == 1
    deferred_inherit = deferred_inherits[0]
    assert deferred_inherit.source == "pkg/mod.py:Foo#class"
    assert deferred_inherit.module_path == "pkg.other"
    assert deferred_inherit.name == "Base"
    assert deferred_inherit.site_file == "pkg/mod.py"
    assert deferred_inherit.site_line == 4
    assert deferred_inherit.provenance.provider == "tree-sitter"


def test_class_with_one_same_file_base_and_one_imported_base_defers_only_the_imported_one():
    # agy review suggestion, slice A2: superclasses.named_children is a
    # sequential loop over every base -- a mixed `class Foo(SameFile,
    # Imported):` must resolve the same-file base immediately AND defer
    # the imported one, not treat the whole class as one all-or-nothing case.
    source = "from pkg.other import Imported\n\n\nclass SameFile:\n    pass\n\n\nclass Foo(SameFile, Imported):\n    pass\n"

    relations, deferred_calls, deferred_inherits, deferred_overrides = extract_relations_with_deferred_edges(
        path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z"
    )

    inherits = [r for r in relations if r.predicate == "inherits"]
    assert len(inherits) == 1
    assert inherits[0].source == "pkg/mod.py:Foo#class"
    assert inherits[0].target == "pkg/mod.py:SameFile#class"
    assert inherits[0].confidence == Confidence.EXTRACTED
    assert deferred_calls == []
    assert len(deferred_inherits) == 1
    assert deferred_inherits[0].source == "pkg/mod.py:Foo#class"
    assert deferred_inherits[0].module_path == "pkg.other"
    assert deferred_inherits[0].name == "Imported"


def test_class_inheriting_from_a_name_from_a_plain_import_statement_is_not_deferred():
    # `import pkg.other` is used as `pkg.other.Base`, never as a bare
    # identifier base class -- it must never populate the alias map used to
    # defer cross-module inherits, same as the calls-side equivalent.
    source = "import pkg.other\n\n\nclass Foo(other):\n    pass\n"

    relations, deferred_calls, deferred_inherits, deferred_overrides = extract_relations_with_deferred_edges(
        path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z"
    )

    assert deferred_inherits == []
    assert [r for r in relations if r.predicate == "inherits"] == []


def test_method_overrides_a_base_class_method_in_the_same_file():
    source = "class Base:\n    def bar(self):\n        pass\n\n\nclass Foo(Base):\n    def bar(self):\n        pass\n"

    relations = extract_relations(path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    overrides = [r for r in relations if r.predicate == "overrides"]
    assert len(overrides) == 1
    rel = overrides[0]
    assert rel.source == "pkg/mod.py:Foo.bar#method"
    assert rel.target == "pkg/mod.py:Base.bar#method"
    assert rel.confidence == Confidence.EXTRACTED
    assert rel.site_file == "pkg/mod.py"
    assert rel.site_line == 7
    assert rel.provenance.provider == "tree-sitter"


def test_class_with_no_base_class_produces_no_overrides():
    source = "class Foo:\n    def bar(self):\n        pass\n"

    relations = extract_relations(path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    assert [r for r in relations if r.predicate == "overrides"] == []


def test_method_with_no_matching_base_method_produces_no_override():
    source = "class Base:\n    def bar(self):\n        pass\n\n\nclass Foo(Base):\n    def baz(self):\n        pass\n"

    relations = extract_relations(path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    assert [r for r in relations if r.predicate == "overrides"] == []


def test_override_from_a_base_class_not_defined_in_this_file_produces_no_edge():
    source = "class Foo(SomeImportedBase):\n    def bar(self):\n        pass\n"

    relations = extract_relations(path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    assert [r for r in relations if r.predicate == "overrides"] == []


def test_override_from_an_ambiguous_redefined_base_class_is_ambiguous():
    source = (
        "class Base:\n    def bar(self):\n        pass\n\n\n"
        "class Base:\n    def bar(self):\n        pass\n\n\n"
        "class Foo(Base):\n    def bar(self):\n        pass\n"
    )

    relations = extract_relations(path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    overrides = [r for r in relations if r.predicate == "overrides"]
    assert len(overrides) == 2
    targets = {r.target for r in overrides}
    assert targets == {"pkg/mod.py:Base.bar#method", "pkg/mod.py:Base.bar#method@2"}
    assert all(r.source == "pkg/mod.py:Foo.bar#method" for r in overrides)
    assert all(r.confidence == Confidence.AMBIGUOUS for r in overrides)


def test_redefined_subclass_name_does_not_leak_an_unrelated_occurrences_methods_into_overrides():
    # Regression (agy/gemini review, 2026-09-02): methods_by_class is keyed
    # by bare qualname text, so a redefined class name (two `class Foo:`
    # bodies) merges both occurrences' methods under the same key. Without
    # scoping to the specific class_definition node being evaluated, the
    # first Foo's bar (whose class has no base at all) would incorrectly be
    # treated as a candidate override source when processing the second,
    # unrelated Foo(Base) occurrence, purely because they share a qualname.
    source = (
        "class Foo:\n    def bar(self):\n        pass\n\n\n"
        "class Base:\n    def bar(self):\n        pass\n\n\n"
        "class Foo(Base):\n    def bar(self):\n        pass\n"
    )

    relations = extract_relations(path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    overrides = [r for r in relations if r.predicate == "overrides"]
    assert len(overrides) == 1
    rel = overrides[0]
    assert rel.source == "pkg/mod.py:Foo.bar#method@2"
    assert rel.target == "pkg/mod.py:Base.bar#method"
    assert rel.confidence == Confidence.EXTRACTED


def test_override_from_multiple_inheritance_with_more_than_one_base_defining_the_method_is_ambiguous():
    source = (
        "class A:\n    def bar(self):\n        pass\n\n\n"
        "class B:\n    def bar(self):\n        pass\n\n\n"
        "class Foo(A, B):\n    def bar(self):\n        pass\n"
    )

    relations = extract_relations(path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    overrides = [r for r in relations if r.predicate == "overrides"]
    assert len(overrides) == 2
    targets = {r.target for r in overrides}
    assert targets == {"pkg/mod.py:A.bar#method", "pkg/mod.py:B.bar#method"}
    assert all(r.source == "pkg/mod.py:Foo.bar#method" for r in overrides)
    assert all(r.confidence == Confidence.AMBIGUOUS for r in overrides)


def test_override_from_a_base_class_imported_from_another_module_is_deferred_not_dropped():
    # Slice A3: mirrors test_class_inheriting_from_a_name_imported_from_another_module_is_deferred_not_dropped
    # for the `overrides` predicate -- extract_relations is single-file-scoped
    # and cannot itself tell whether a cross-file base defines a same-named
    # method, so the check must be deferred, not silently dropped like a
    # genuinely undefined name would be.
    source = "from pkg.other import Base\n\n\nclass Foo(Base):\n    def bar(self):\n        pass\n"

    relations, deferred_calls, deferred_inherits, deferred_overrides = extract_relations_with_deferred_edges(
        path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z"
    )

    assert [r for r in relations if r.predicate == "overrides"] == []
    assert deferred_calls == []
    assert len(deferred_overrides) == 1
    deferred_override = deferred_overrides[0]
    assert deferred_override.source == "pkg/mod.py:Foo.bar#method"
    assert deferred_override.module_path == "pkg.other"
    assert deferred_override.base_name == "Base"
    assert deferred_override.method_name == "bar"
    assert deferred_override.site_file == "pkg/mod.py"
    assert deferred_override.site_line == 5
    assert deferred_override.provenance.provider == "tree-sitter"


def test_class_with_no_methods_produces_no_deferred_override_even_with_an_imported_base():
    source = "from pkg.other import Base\n\n\nclass Foo(Base):\n    pass\n"

    relations, deferred_calls, deferred_inherits, deferred_overrides = extract_relations_with_deferred_edges(
        path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z"
    )

    assert deferred_overrides == []


def test_class_with_one_same_file_base_and_one_imported_base_defers_the_override_check_against_the_imported_one_too():
    # Mirrors test_class_with_one_same_file_base_and_one_imported_base_defers_only_the_imported_one
    # (the inherits-side equivalent): the same-file base already defines
    # `bar`, so that override edge resolves immediately -- but the imported
    # base is checked independently too, since it might *also* define `bar`,
    # which only indexer.py's repo-wide index can tell.
    source = (
        "from pkg.other import Imported\n\n\n"
        "class SameFile:\n    def bar(self):\n        pass\n\n\n"
        "class Foo(SameFile, Imported):\n    def bar(self):\n        pass\n"
    )

    relations, deferred_calls, deferred_inherits, deferred_overrides = extract_relations_with_deferred_edges(
        path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z"
    )

    overrides = [r for r in relations if r.predicate == "overrides"]
    assert len(overrides) == 1
    assert overrides[0].source == "pkg/mod.py:Foo.bar#method"
    assert overrides[0].target == "pkg/mod.py:SameFile.bar#method"
    assert overrides[0].confidence == Confidence.EXTRACTED

    assert len(deferred_overrides) == 1
    deferred_override = deferred_overrides[0]
    assert deferred_override.source == "pkg/mod.py:Foo.bar#method"
    assert deferred_override.base_name == "Imported"
    assert deferred_override.module_path == "pkg.other"
    assert deferred_override.method_name == "bar"


def test_override_check_against_a_name_from_a_plain_import_statement_is_not_deferred():
    # `import pkg.other` is used as `pkg.other.Base`, never as a bare
    # identifier base class -- it must never populate the alias map used to
    # defer cross-module override checks, same as the calls/inherits-side
    # equivalents.
    source = "import pkg.other\n\n\nclass Foo(other):\n    def bar(self):\n        pass\n"

    relations, deferred_calls, deferred_inherits, deferred_overrides = extract_relations_with_deferred_edges(
        path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z"
    )

    assert deferred_overrides == []
    assert [r for r in relations if r.predicate == "overrides"] == []


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


def test_self_method_call_resolves_to_the_method_in_the_same_class():
    source = (
        "class Foo:\n"
        "    def caller(self):\n"
        "        self.callee()\n"
        "\n"
        "    def callee(self):\n"
        "        pass\n"
    )

    relations = extract_relations(path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    calls = [r for r in relations if r.predicate == "calls"]
    assert len(calls) == 1
    rel = calls[0]
    assert rel.source == "pkg/mod.py:Foo.caller#method"
    assert rel.target == "pkg/mod.py:Foo.callee#method"
    assert rel.confidence == Confidence.EXTRACTED


def test_self_method_call_to_a_method_on_a_different_class_produces_no_edge():
    source = (
        "class Foo:\n"
        "    def caller(self):\n"
        "        self.callee()\n"
        "\n"
        "\n"
        "class Bar:\n"
        "    def callee(self):\n"
        "        pass\n"
    )

    relations = extract_relations(path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    calls = [r for r in relations if r.predicate == "calls"]
    assert calls == []


def test_self_method_call_to_an_ambiguous_redefined_method_name():
    source = (
        "class Foo:\n"
        "    def caller(self):\n"
        "        self.callee()\n"
        "\n"
        "    def callee(self):\n"
        "        pass\n"
        "\n"
        "    def callee(self):\n"
        "        pass\n"
    )

    relations = extract_relations(path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    calls = [r for r in relations if r.predicate == "calls"]
    assert len(calls) == 2
    targets = {r.target for r in calls}
    assert targets == {"pkg/mod.py:Foo.callee#method", "pkg/mod.py:Foo.callee#method@2"}
    assert all(r.confidence == Confidence.AMBIGUOUS for r in calls)


def test_non_self_attribute_call_still_produces_no_edge():
    source = (
        "class Foo:\n"
        "    def caller(self, other):\n"
        "        other.callee()\n"
        "\n"
        "    def callee(self):\n"
        "        pass\n"
    )

    relations = extract_relations(path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    calls = [r for r in relations if r.predicate == "calls"]
    assert calls == []


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


def test_call_to_a_name_imported_from_another_module_is_deferred_not_dropped():
    source = "from pkg.other import helper\n\n\nhelper()\n"

    relations, deferred, deferred_inherits, deferred_overrides = extract_relations_with_deferred_edges(
        path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z"
    )

    # extract_relations is single-file-scoped and can never resolve this by
    # itself -- it must be deferred, not silently dropped like a genuinely
    # undefined name would be.
    assert [r for r in relations if r.predicate == "calls"] == []
    assert deferred_inherits == []
    assert len(deferred) == 1
    deferred_call = deferred[0]
    assert deferred_call.source == "pkg/mod.py:#module"
    assert deferred_call.module_path == "pkg.other"
    assert deferred_call.name == "helper"
    assert deferred_call.site_file == "pkg/mod.py"
    assert deferred_call.site_line == 4
    assert deferred_call.provenance.provider == "tree-sitter"


def test_extract_relations_public_function_omits_deferred_calls_entirely():
    source = "from pkg.other import helper\n\n\nhelper()\n"

    relations = extract_relations(path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    assert [r for r in relations if r.predicate == "calls"] == []


def test_call_to_a_name_from_a_plain_import_statement_is_not_deferred():
    # `import pkg.other` is called as `pkg.other.something()`, never as a
    # bare identifier -- it must never populate the alias map used to defer
    # cross-module calls.
    source = "import pkg.other\n\n\nother()\n"

    relations, deferred, deferred_inherits, deferred_overrides = extract_relations_with_deferred_edges(
        path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z"
    )

    assert deferred == []
    assert deferred_inherits == []
    assert [r for r in relations if r.predicate == "calls"] == []
