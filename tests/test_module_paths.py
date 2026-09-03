"""Tests for acie.module_paths -- the file_path<->dotted-module-name
derivation extracted (v1 slice C1, wayfinder ticket 47d8cd0d) out of
indexer.py's originally-private `_module_path_matches`.

The src-layout suffix-match cases below are the empirical verification
demanded by the design-risk memory (271dc881, "Architecture-Tool Design
Risk: Dotted-Name Import Resolution Unverified Against src-Layout Repos"):
`module_path_matches`/`path_to_dotted` already do SUFFIX matching, not
exact full-path matching, so a `src/`-style package-root prefix on the
candidate file path is tolerated for free -- the risk memory's feared
failure mode (every src-layout internal import silently misclassifying as
external) does not occur.
"""

from acie.module_paths import module_path_matches, path_to_dotted


def test_path_to_dotted_converts_slashes_to_dots_and_strips_py_suffix():
    assert path_to_dotted("pkg/mod.py") == "pkg.mod"


def test_path_to_dotted_strips_init_py_to_the_package_name():
    assert path_to_dotted("pkg/sub/__init__.py") == "pkg.sub"


def test_path_to_dotted_top_level_file_has_no_dots():
    assert path_to_dotted("mod.py") == "mod"


def test_module_path_matches_exact_full_dotted_path():
    assert module_path_matches("pkg/mod.py", "pkg.mod") is True


def test_module_path_matches_src_layout_prefix_is_tolerated_via_suffix_match():
    # src/mypackage/foo.py is importable as `mypackage.foo`, not
    # `src.mypackage.foo` -- module_path_matches must still recognize it.
    assert module_path_matches("src/mypackage/foo.py", "mypackage.foo") is True


def test_module_path_matches_src_layout_bare_leaf_suffix_also_matches():
    assert module_path_matches("src/mypackage/foo.py", "foo") is True


def test_module_path_matches_rejects_a_non_suffix_substring():
    # "package.foo" is a substring of "mypackage.foo" but not a dotted
    # suffix of it -- must not match.
    assert module_path_matches("src/mypackage/foo.py", "package.foo") is False


def test_module_path_matches_rejects_an_unrelated_module_path():
    assert module_path_matches("pkg/mod.py", "other.mod") is False


def test_module_path_matches_init_py_candidate_against_its_package_name():
    assert module_path_matches("src/mypackage/__init__.py", "mypackage") is True
