"""Static pytest-convention heuristics shared by affected_tests (slice B1)
and the fixture-DI extractor (slice B2).

Originally private helpers inside acie.tools.affected_tests
(`_is_test_file_path` / the qualname-leaf check inlined in `_is_test_node`).
B2's extract_relations.py needs the identical test-file/test-name
convention to decide which functions are legitimate pytest dependency-
injection sites -- a real second caller (this project's "wait for a real
2nd caller before generalizing" norm), and one in a lower layer
(acie.adapters.python must not import from acie.tools, which itself
depends on acie.storage/acie.ir -- the dependency would point the wrong
way), so this lives in acie's shared top-level namespace instead of either
call site's own package.

Hardcoded pytest convention only, same as B1: `test_*.py`/`*_test.py` file
paths, `test_*` function/method qualnames. A configurable glob override via
`.acie/config.json` was surfaced as a possible follow-up but is explicitly
non-blocking for v1 (wayfinder map 5d8fa498's "Not yet specified").
"""


def is_test_file_path(path: str) -> bool:
    basename = path.rsplit("/", 1)[-1]
    return (basename.startswith("test_") and basename.endswith(".py")) or basename.endswith("_test.py")


def is_test_qualname(qualname: str) -> bool:
    """Whether qualname's final dotted segment (the bare function/method
    name) starts with `test_` -- correctly handles unittest-style
    `TestCase.test_foo` methods via qualname-leaf split, same as a bare
    top-level `test_foo` function.
    """
    leaf = qualname.rsplit(".", 1)[-1]
    return leaf.startswith("test_")
