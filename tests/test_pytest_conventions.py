from acie.pytest_conventions import is_test_file_path, is_test_qualname


def test_is_test_file_path_matches_test_prefix():
    assert is_test_file_path("tests/test_foo.py") is True


def test_is_test_file_path_matches_test_suffix():
    assert is_test_file_path("tests/foo_test.py") is True


def test_is_test_file_path_rejects_a_non_test_file():
    assert is_test_file_path("pkg/mod.py") is False


def test_is_test_file_path_rejects_a_test_named_directory_with_a_plain_module():
    assert is_test_file_path("test_pkg/mod.py") is False


def test_is_test_qualname_matches_a_top_level_test_function():
    assert is_test_qualname("test_foo") is True


def test_is_test_qualname_matches_a_unittest_style_method_leaf():
    assert is_test_qualname("TestFoo.test_bar") is True


def test_is_test_qualname_rejects_a_non_test_named_leaf():
    assert is_test_qualname("TestFoo.setup_method") is False
