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


def test_relative_from_import_is_extracted_and_drives_deferred_import_call():
    source = "from . import lifecycle\n\n\nlifecycle.func()\n"
    path = "pkg/sub/mod.py"

    relations = extract_relations(path=path, source_text=source, observed_at="2026-09-05T00:00:00Z")

    imports = [r for r in relations if r.predicate == "imports"]
    assert len(imports) == 1
    import_rel = imports[0]
    assert import_rel.source == "pkg/sub/mod.py:#module"
    assert import_rel.target == "pkg.sub.lifecycle"
    assert import_rel.site_line == 1
    assert import_rel.confidence == Confidence.EXTRACTED

    relations, deferred_calls, deferred_inherits, deferred_overrides = extract_relations_with_deferred_edges(
        path=path, source_text=source, observed_at="2026-09-05T00:00:00Z"
    )

    assert [r for r in relations if r.predicate == "calls"] == []
    assert deferred_inherits == []
    assert deferred_overrides == []
    assert len(deferred_calls) == 1
    deferred_call = deferred_calls[0]
    assert deferred_call.source == "pkg/sub/mod.py:#module"
    assert deferred_call.module_path == "pkg.sub"
    assert deferred_call.name == "lifecycle"
    assert deferred_call.attribute == "func"
    assert deferred_call.site_col == 10
    assert deferred_call.site_file == path
    assert deferred_call.site_line == 4


def test_relative_from_imports_resolve_parent_and_submodule_with_mixed_names():
    source = (
        "from .. import parent\n"
        "from .sub import x, y as z\n"
        "\n"
        "parent()\n"
        "x()\n"
        "z()\n"
    )
    path = "pkg/sub/mod.py"

    relations = extract_relations(path=path, source_text=source, observed_at="2026-09-05T00:00:00Z")
    imports = [r for r in relations if r.predicate == "imports"]

    assert len(imports) == 3
    assert {r.target for r in imports} == {"pkg.parent", "pkg.sub.sub.x", "pkg.sub.sub.y"}
    assert all(r.source == "pkg/sub/mod.py:#module" for r in imports)
    assert next(r for r in imports if r.target == "pkg.parent").site_line == 1
    assert all(r.site_line == 2 for r in imports if r.target != "pkg.parent")
    assert all(r.confidence == Confidence.EXTRACTED for r in imports)

    relations, deferred_calls, deferred_inherits, deferred_overrides = extract_relations_with_deferred_edges(
        path=path, source_text=source, observed_at="2026-09-05T00:00:00Z"
    )

    assert [r for r in relations if r.predicate == "calls"] == []
    assert deferred_inherits == []
    assert deferred_overrides == []
    assert {(call.module_path, call.name) for call in deferred_calls} == {
        ("pkg", "parent"),
        ("pkg.sub.sub", "x"),
        ("pkg.sub.sub", "z"),
    }


def test_aliased_from_import_targets_original_name_and_defers_attribute_call():
    source = "from pkg import mod as alias\n\n\nalias.func()\n"
    path = "pkg/consumer.py"

    relations = extract_relations(path=path, source_text=source, observed_at="2026-09-05T00:00:00Z")

    imports = [r for r in relations if r.predicate == "imports"]
    assert len(imports) == 1
    import_rel = imports[0]
    assert import_rel.source == "pkg/consumer.py:#module"
    assert import_rel.target == "pkg.mod"
    assert import_rel.site_line == 1
    assert import_rel.confidence == Confidence.EXTRACTED

    relations, deferred_calls, deferred_inherits, deferred_overrides = extract_relations_with_deferred_edges(
        path=path, source_text=source, observed_at="2026-09-05T00:00:00Z"
    )

    assert [r for r in relations if r.predicate == "calls"] == []
    assert deferred_inherits == []
    assert deferred_overrides == []
    assert len(deferred_calls) == 1
    deferred_call = deferred_calls[0]
    assert deferred_call.source == "pkg/consumer.py:#module"
    assert deferred_call.module_path == "pkg"
    assert deferred_call.name == "alias"
    assert deferred_call.attribute == "func"
    assert deferred_call.site_file == path
    assert deferred_call.site_line == 4
    assert deferred_call.site_col == 6


def test_plain_aliased_import_targets_original_module_without_alias_map_entry():
    source = "import pkg as alias\n\n\nalias.func()\n"
    path = "pkg/consumer.py"

    relations = extract_relations(path=path, source_text=source, observed_at="2026-09-05T00:00:00Z")

    imports = [r for r in relations if r.predicate == "imports"]
    assert len(imports) == 1
    import_rel = imports[0]
    assert import_rel.source == "pkg/consumer.py:#module"
    assert import_rel.target == "pkg"
    assert import_rel.site_line == 1
    assert import_rel.confidence == Confidence.EXTRACTED

    relations, deferred_calls, deferred_inherits, deferred_overrides = extract_relations_with_deferred_edges(
        path=path, source_text=source, observed_at="2026-09-05T00:00:00Z"
    )

    assert [r for r in relations if r.predicate == "calls"] == []
    assert deferred_calls == []
    assert deferred_inherits == []
    assert deferred_overrides == []


def test_function_local_from_import_is_attributed_to_module_and_drives_deferred_call():
    source = "def caller():\n    from pkg import helper\n    helper.func()\n"
    path = "pkg/consumer.py"

    relations = extract_relations(path=path, source_text=source, observed_at="2026-09-05T00:00:00Z")

    imports = [r for r in relations if r.predicate == "imports"]
    assert len(imports) == 1
    import_rel = imports[0]
    assert import_rel.source == "pkg/consumer.py:#module"
    assert import_rel.target == "pkg.helper"
    assert import_rel.site_line == 2
    assert import_rel.confidence == Confidence.EXTRACTED

    relations, deferred_calls, deferred_inherits, deferred_overrides = extract_relations_with_deferred_edges(
        path=path, source_text=source, observed_at="2026-09-05T00:00:00Z"
    )

    assert [r for r in relations if r.predicate == "calls"] == []
    assert deferred_inherits == []
    assert deferred_overrides == []
    assert len(deferred_calls) == 1
    deferred_call = deferred_calls[0]
    assert deferred_call.source == "pkg/consumer.py:caller#function"
    assert deferred_call.module_path == "pkg"
    assert deferred_call.name == "helper"
    assert deferred_call.attribute == "func"
    assert deferred_call.site_file == path
    assert deferred_call.site_line == 3
    assert deferred_call.site_col == 11


def test_relative_import_that_walks_above_top_level_package_is_skipped():
    source = "from .. import missing\n\n\nmissing()\n"
    path = "pkg/mod.py"

    relations = extract_relations(path=path, source_text=source, observed_at="2026-09-05T00:00:00Z")

    assert [r for r in relations if r.predicate == "imports"] == []

    relations, deferred_calls, deferred_inherits, deferred_overrides = extract_relations_with_deferred_edges(
        path=path, source_text=source, observed_at="2026-09-05T00:00:00Z"
    )

    assert [r for r in relations if r.predicate == "imports"] == []
    assert deferred_calls == []
    assert deferred_inherits == []
    assert deferred_overrides == []


def test_relative_import_at_a_bare_root_level_init_file_resolves_without_the_filename_leaking_in():
    source = "from . import x\n"
    path = "__init__.py"

    relations = extract_relations(path=path, source_text=source, observed_at="2026-09-05T00:00:00Z")

    imports = [r for r in relations if r.predicate == "imports"]
    assert len(imports) == 1
    assert imports[0].target == "x"


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


def test_attribute_call_to_a_submodule_imported_from_another_module_is_deferred_not_dropped():
    source = "from acie import scan\n\n\nscan.run_scan(path)\n"

    relations, deferred, deferred_inherits, deferred_overrides = extract_relations_with_deferred_edges(
        path="src/acie/cli.py", source_text=source, observed_at="2026-09-05T00:00:00Z"
    )

    assert [r for r in relations if r.predicate == "calls"] == []
    assert deferred_inherits == []
    assert deferred_overrides == []
    assert len(deferred) == 1
    deferred_call = deferred[0]
    assert deferred_call.source == "src/acie/cli.py:#module"
    assert deferred_call.module_path == "acie"
    assert deferred_call.name == "scan"
    assert deferred_call.attribute == "run_scan"
    assert deferred_call.site_file == "src/acie/cli.py"
    assert deferred_call.site_line == 4
    assert deferred_call.site_col == 5
    assert deferred_call.provenance.provider == "tree-sitter"


def test_extract_relations_public_function_omits_deferred_calls_entirely():
    source = "from pkg.other import helper\n\n\nhelper()\n"

    relations = extract_relations(path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    assert [r for r in relations if r.predicate == "calls"] == []


def test_extract_relations_public_function_omits_deferred_attribute_calls_entirely():
    source = "from acie import scan\n\n\nscan.run_scan(path)\n"

    relations = extract_relations(path="src/acie/cli.py", source_text=source, observed_at="2026-09-05T00:00:00Z")

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


# ---------------------------------------------------------------------------
# pytest-fixture dependency-injection heuristic (slice B2, wayfinder ticket
# df13991a). Same-file only -- see extract_relations.py module docstring for
# the cross-file/conftest.py scope note. Always AMBIGUOUS confidence,
# provenance.provider="pytest-fixture-heuristic": a naming-convention
# heuristic, never a resolved reference.
# ---------------------------------------------------------------------------


def test_test_function_parameter_matching_a_same_file_fixture_produces_an_ambiguous_calls_edge():
    source = (
        "import pytest\n\n\n"
        "@pytest.fixture\n"
        "def db():\n"
        "    pass\n\n\n"
        "def test_uses_db(db):\n"
        "    pass\n"
    )

    relations = extract_relations(path="tests/test_mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    calls = [r for r in relations if r.predicate == "calls"]
    assert len(calls) == 1
    rel = calls[0]
    assert rel.source == "tests/test_mod.py:test_uses_db#function"
    assert rel.target == "tests/test_mod.py:db#function"
    assert rel.confidence == Confidence.AMBIGUOUS
    assert rel.provenance.provider == "pytest-fixture-heuristic"
    assert rel.site_file == "tests/test_mod.py"


def test_fixture_function_can_itself_depend_on_another_fixture_by_parameter_name():
    # Fixture-to-fixture DI is real pytest behavior and not restricted to
    # test-convention files -- a plain module can host a conftest-style
    # fixture chain.
    source = (
        "import pytest\n\n\n"
        "@pytest.fixture\n"
        "def base():\n"
        "    pass\n\n\n"
        "@pytest.fixture\n"
        "def derived(base):\n"
        "    pass\n"
    )

    relations = extract_relations(path="pkg/conftest.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    calls = [r for r in relations if r.predicate == "calls"]
    assert len(calls) == 1
    rel = calls[0]
    assert rel.source == "pkg/conftest.py:derived#function"
    assert rel.target == "pkg/conftest.py:base#function"
    assert rel.confidence == Confidence.AMBIGUOUS


def test_ordinary_helper_function_parameter_matching_a_fixture_name_produces_no_edge():
    # Neither a test function nor a fixture itself -- pytest would never
    # inject a fixture into it, so this is not a DI site even though the
    # parameter name collides with a real fixture.
    source = (
        "import pytest\n\n\n"
        "@pytest.fixture\n"
        "def db():\n"
        "    pass\n\n\n"
        "def helper(db):\n"
        "    pass\n"
    )

    relations = extract_relations(path="tests/test_mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    assert [r for r in relations if r.predicate == "calls"] == []


def test_test_function_outside_a_test_convention_file_is_not_a_di_site():
    source = (
        "import pytest\n\n\n"
        "@pytest.fixture\n"
        "def db():\n"
        "    pass\n\n\n"
        "def test_uses_db(db):\n"
        "    pass\n"
    )

    relations = extract_relations(path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    assert [r for r in relations if r.predicate == "calls"] == []


def test_self_and_cls_parameters_are_never_treated_as_fixture_di():
    source = (
        "import pytest\n\n\n"
        "class TestFoo:\n"
        "    @pytest.fixture\n"
        "    def self(self):\n"  # pathological but guards the skip explicitly
        "        pass\n\n"
        "    def test_thing(self):\n"
        "        pass\n"
    )

    relations = extract_relations(path="tests/test_mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    assert [r for r in relations if r.predicate == "calls"] == []


def test_fixture_decorator_called_with_arguments_is_still_recognized():
    source = (
        "import pytest\n\n\n"
        "@pytest.fixture(scope=\"module\")\n"
        "def db():\n"
        "    pass\n\n\n"
        "def test_uses_db(db):\n"
        "    pass\n"
    )

    relations = extract_relations(path="tests/test_mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    calls = [r for r in relations if r.predicate == "calls"]
    assert len(calls) == 1
    assert calls[0].target == "tests/test_mod.py:db#function"


def test_bare_fixture_decorator_imported_from_pytest_is_recognized():
    source = (
        "from pytest import fixture\n\n\n"
        "@fixture\n"
        "def db():\n"
        "    pass\n\n\n"
        "def test_uses_db(db):\n"
        "    pass\n"
    )

    relations = extract_relations(path="tests/test_mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    calls = [r for r in relations if r.predicate == "calls"]
    assert len(calls) == 1
    assert calls[0].target == "tests/test_mod.py:db#function"


def test_a_bare_decorator_named_fixture_but_not_imported_from_pytest_is_not_recognized():
    source = (
        "from somewhere_else import fixture\n\n\n"
        "@fixture\n"
        "def db():\n"
        "    pass\n\n\n"
        "def test_uses_db(db):\n"
        "    pass\n"
    )

    relations = extract_relations(path="tests/test_mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    assert [r for r in relations if r.predicate == "calls"] == []


def test_parameter_name_with_no_matching_fixture_produces_no_edge():
    source = "def test_thing(unrelated_param):\n    pass\n"

    relations = extract_relations(path="tests/test_mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    assert [r for r in relations if r.predicate == "calls"] == []


# --- P1 code-review fixes, 2026-09-03: class-scope precedence, name=
# override, and mandatory-parameter-only matching. All three verified
# against a real pytest run/source before implementing (see docstrings on
# _fixture_definitions/_fixture_di_relations/_param_name_nodes). ---


def test_a_class_local_fixture_shadows_a_same_named_module_fixture_for_its_own_class():
    # A same-named class-level fixture takes precedence over the
    # module-level one for members of its OWN class -- real pytest, not
    # ambiguity. The module-level fixture is NOT also a candidate here.
    source = (
        "import pytest\n\n\n"
        "@pytest.fixture\n"
        "def db():\n"
        "    pass\n\n\n"
        "class TestFoo:\n"
        "    @pytest.fixture\n"
        "    def db(self):\n"
        "        pass\n\n"
        "    def test_uses_db(self, db):\n"
        "        pass\n"
    )

    relations = extract_relations(path="tests/test_mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    calls = [r for r in relations if r.predicate == "calls"]
    assert len(calls) == 1
    rel = calls[0]
    assert rel.source == "tests/test_mod.py:TestFoo.test_uses_db#method"
    assert rel.target == "tests/test_mod.py:TestFoo.db#method"
    assert rel.confidence == Confidence.AMBIGUOUS


def test_a_class_local_fixture_is_not_visible_to_a_sibling_class():
    # TestB has no db fixture of its own -- it must fall back to the
    # module-level db, never TestA's class-scoped one (real pytest class
    # fixtures are invisible outside their own class).
    source = (
        "import pytest\n\n\n"
        "@pytest.fixture\n"
        "def db():\n"
        "    pass\n\n\n"
        "class TestA:\n"
        "    @pytest.fixture\n"
        "    def db(self):\n"
        "        pass\n\n\n"
        "class TestB:\n"
        "    def test_uses_db(self, db):\n"
        "        pass\n"
    )

    relations = extract_relations(path="tests/test_mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    calls = [r for r in relations if r.predicate == "calls"]
    assert len(calls) == 1
    rel = calls[0]
    assert rel.source == "tests/test_mod.py:TestB.test_uses_db#method"
    assert rel.target == "tests/test_mod.py:db#function"


def test_two_same_named_module_level_fixtures_are_genuinely_ambiguous():
    # Real redefinition-collision ambiguity, distinct from the shadowing
    # cases above -- both candidates are in the SAME (module) scope.
    source = (
        "import pytest\n\n\n"
        "@pytest.fixture\n"
        "def db():\n"
        "    pass\n\n\n"
        "@pytest.fixture\n"
        "def db():\n"
        "    pass\n\n\n"
        "def test_uses_db(db):\n"
        "    pass\n"
    )

    relations = extract_relations(path="tests/test_mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    calls = [r for r in relations if r.predicate == "calls"]
    assert len(calls) == 2
    targets = {r.target for r in calls}
    assert targets == {"tests/test_mod.py:db#function", "tests/test_mod.py:db#function@2"}
    assert all(r.confidence == Confidence.AMBIGUOUS for r in calls)


def test_fixture_with_a_custom_public_name_is_indexed_under_that_name():
    source = (
        "import pytest\n\n\n"
        '@pytest.fixture(name="db")\n'
        "def database():\n"
        "    pass\n\n\n"
        "def test_uses_db(db):\n"
        "    pass\n"
    )

    relations = extract_relations(path="tests/test_mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    calls = [r for r in relations if r.predicate == "calls"]
    assert len(calls) == 1
    assert calls[0].target == "tests/test_mod.py:database#function"


def test_a_renamed_fixtures_original_function_name_is_no_longer_a_match():
    # pytest.fixture(name=...) REPLACES the registered name, it doesn't
    # add to it -- a parameter matching the underlying function's own name
    # must produce no edge.
    source = (
        "import pytest\n\n\n"
        '@pytest.fixture(name="db")\n'
        "def database():\n"
        "    pass\n\n\n"
        "def test_uses_database(database):\n"
        "    pass\n"
    )

    relations = extract_relations(path="tests/test_mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    assert [r for r in relations if r.predicate == "calls"] == []


def test_fixture_decorator_with_name_and_other_keyword_arguments_together():
    source = (
        "import pytest\n\n\n"
        '@pytest.fixture(scope="module", name="db")\n'
        "def database():\n"
        "    pass\n\n\n"
        "def test_uses_db(db):\n"
        "    pass\n"
    )

    relations = extract_relations(path="tests/test_mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    calls = [r for r in relations if r.predicate == "calls"]
    assert len(calls) == 1
    assert calls[0].target == "tests/test_mod.py:database#function"


def test_a_non_literal_name_argument_falls_back_to_the_function_name():
    source = (
        "import pytest\n\n\n"
        "custom_name = \"db\"\n\n\n"
        "@pytest.fixture(name=custom_name)\n"
        "def database():\n"
        "    pass\n\n\n"
        "def test_uses_database(database):\n"
        "    pass\n"
    )

    relations = extract_relations(path="tests/test_mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    calls = [r for r in relations if r.predicate == "calls"]
    assert len(calls) == 1
    assert calls[0].target == "tests/test_mod.py:database#function"


def test_typed_parameter_without_a_default_is_still_matched_against_a_fixture():
    source = (
        "import pytest\n\n\n"
        "@pytest.fixture\n"
        "def db():\n"
        "    pass\n\n\n"
        "def test_uses_db(db: object):\n"
        "    pass\n"
    )

    relations = extract_relations(path="tests/test_mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    calls = [r for r in relations if r.predicate == "calls"]
    assert len(calls) == 1
    assert calls[0].target == "tests/test_mod.py:db#function"


def test_a_defaulted_parameter_is_never_treated_as_a_fixture_request():
    # pytest's own getfuncargnames only treats a parameter as a fixture
    # request when it has no default value -- a defaulted parameter is a
    # plain optional argument (verified against real pytest, 2026-09-03).
    source = (
        "import pytest\n\n\n"
        "@pytest.fixture\n"
        "def db():\n"
        "    pass\n\n\n"
        "def test_uses_db(db=None):\n"
        "    pass\n"
    )

    relations = extract_relations(path="tests/test_mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    assert [r for r in relations if r.predicate == "calls"] == []


def test_a_typed_defaulted_parameter_is_never_treated_as_a_fixture_request():
    source = (
        "import pytest\n\n\n"
        "@pytest.fixture\n"
        "def db():\n"
        "    pass\n\n\n"
        "def test_uses_db(db: object = None):\n"
        "    pass\n"
    )

    relations = extract_relations(path="tests/test_mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    assert [r for r in relations if r.predicate == "calls"] == []


def test_star_args_and_kwargs_are_never_treated_as_fixture_parameters():
    source = (
        "import pytest\n\n\n"
        "@pytest.fixture\n"
        "def args():\n"
        "    pass\n\n\n"
        "def test_thing(*args, **kwargs):\n"
        "    pass\n"
    )

    relations = extract_relations(path="tests/test_mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    assert [r for r in relations if r.predicate == "calls"] == []


def test_a_fixture_cannot_depend_on_itself_via_a_same_named_parameter():
    # Pathological, but guards against a fixture whose own bare name happens
    # to equal one of its parameter names producing a self-loop edge.
    source = "import pytest\n\n\n@pytest.fixture\ndef db(db):\n    pass\n"

    relations = extract_relations(path="tests/test_mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    assert [r for r in relations if r.predicate == "calls"] == []


def test_class_scoped_fixture_matches_a_test_method_in_the_same_class():
    source = (
        "import pytest\n\n\n"
        "class TestFoo:\n"
        "    @pytest.fixture\n"
        "    def local_db(self):\n"
        "        pass\n\n"
        "    def test_uses_local_db(self, local_db):\n"
        "        pass\n"
    )

    relations = extract_relations(path="tests/test_mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z")

    calls = [r for r in relations if r.predicate == "calls"]
    assert len(calls) == 1
    rel = calls[0]
    assert rel.source == "tests/test_mod.py:TestFoo.test_uses_local_db#method"
    assert rel.target == "tests/test_mod.py:TestFoo.local_db#method"


def test_fixture_di_edges_are_included_in_deferred_edges_entry_point_too():
    source = (
        "import pytest\n\n\n"
        "@pytest.fixture\n"
        "def db():\n"
        "    pass\n\n\n"
        "def test_uses_db(db):\n"
        "    pass\n"
    )

    relations, deferred, deferred_inherits, deferred_overrides = extract_relations_with_deferred_edges(
        path="tests/test_mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z"
    )

    calls = [r for r in relations if r.predicate == "calls"]
    assert len(calls) == 1
    assert calls[0].target == "tests/test_mod.py:db#function"
    assert deferred == []
    assert deferred_inherits == []
    assert deferred_overrides == []
