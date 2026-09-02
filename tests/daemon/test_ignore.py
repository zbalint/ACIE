import os

from acie.daemon import ignore


def _write(repo_root, rel_path, content):
    abs_path = os.path.join(repo_root, rel_path)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "w", encoding="utf-8") as f:
        f.write(content)


def test_no_gitignore_anywhere_means_nothing_is_ignored(tmp_path):
    repo_root = str(tmp_path)
    matcher = ignore.IgnoreMatcher(repo_root)

    assert matcher.matches("src/foo.py") is False


def test_git_directory_is_always_ignored_even_with_no_gitignore(tmp_path):
    repo_root = str(tmp_path)
    matcher = ignore.IgnoreMatcher(repo_root)

    assert matcher.matches(".git") is True
    assert matcher.matches(".git/HEAD") is True


def test_root_gitignore_pattern_is_respected(tmp_path):
    repo_root = str(tmp_path)
    _write(repo_root, ".gitignore", "build/\n*.pyc\n")

    matcher = ignore.IgnoreMatcher(repo_root)

    assert matcher.matches("build/output.py") is True
    assert matcher.matches("src/foo.pyc") is True
    assert matcher.matches("src/foo.py") is False


def test_nested_gitignore_pattern_only_applies_within_its_own_subtree(tmp_path):
    repo_root = str(tmp_path)
    _write(repo_root, "vendor/.gitignore", "generated/\n")

    matcher = ignore.IgnoreMatcher(repo_root)

    assert matcher.matches("vendor/generated/thing.py") is True
    # Same basename outside vendor/ is untouched by vendor/'s .gitignore.
    assert matcher.matches("generated/thing.py") is False


def test_nested_gitignore_can_re_include_a_path_the_root_gitignore_ignored(tmp_path):
    repo_root = str(tmp_path)
    _write(repo_root, ".gitignore", "*.log\n")
    _write(repo_root, "keep/.gitignore", "!important.log\n")

    matcher = ignore.IgnoreMatcher(repo_root)

    assert matcher.matches("other/debug.log") is True
    assert matcher.matches("keep/important.log") is False


def test_root_gitignore_negation_can_be_overridden_by_a_deeper_ignore(tmp_path):
    repo_root = str(tmp_path)
    _write(repo_root, ".gitignore", "!keep_me.txt\n")
    _write(repo_root, "sub/.gitignore", "keep_me.txt\n")

    matcher = ignore.IgnoreMatcher(repo_root)

    # Root says "never ignore keep_me.txt" but the closer, more specific
    # sub/.gitignore re-ignores it within sub/ -- deeper takes precedence.
    assert matcher.matches("sub/keep_me.txt") is True
    # Outside sub/, the root negation still applies (nothing there ignores it).
    assert matcher.matches("keep_me.txt") is False


def test_get_ignore_matcher_caches_the_same_instance_for_a_repo_root(tmp_path):
    repo_root = str(tmp_path)

    first = ignore.get_ignore_matcher(repo_root)
    second = ignore.get_ignore_matcher(repo_root)

    assert first is second


def test_invalidate_forces_recompilation_on_next_get(tmp_path):
    repo_root = str(tmp_path)
    matcher_before = ignore.get_ignore_matcher(repo_root)
    assert matcher_before.matches("build/output.py") is False

    _write(repo_root, ".gitignore", "build/\n")
    ignore.invalidate(repo_root)
    matcher_after = ignore.get_ignore_matcher(repo_root)

    assert matcher_after is not matcher_before
    assert matcher_after.matches("build/output.py") is True


def test_get_ignore_matcher_for_different_repo_roots_are_independent(tmp_path):
    root_a = str(tmp_path / "a")
    root_b = str(tmp_path / "b")
    os.makedirs(root_a)
    os.makedirs(root_b)
    _write(root_a, ".gitignore", "secret.py\n")

    matcher_a = ignore.get_ignore_matcher(root_a)
    matcher_b = ignore.get_ignore_matcher(root_b)

    assert matcher_a.matches("secret.py") is True
    assert matcher_b.matches("secret.py") is False
