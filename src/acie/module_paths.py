"""File-path<->dotted-module-name derivation, shared between indexer.py's
cross-file deferred-import resolution and the `architecture` MCP tool's
dotted-name index (v1 slice C1, wayfinder ticket 47d8cd0d).

Originally a private pair (`_module_path_matches` plus its inlined
derivation) inside indexer.py, used only to resolve a DeferredImportCall/
DeferredImportInherit/DeferredImportOverride's raw dotted module_path
against a repo-wide symbol candidate's own file path. C1 needs the
identical derivation to build a one-time dotted_name -> file_path index for
the architecture tool's internal/external import classification -- a real
second caller (this project's "wait for a real 2nd caller before
generalizing" norm), and indexer.py/acie.tools are dependency-graph
siblings (neither imports the other -- confirmed via
`mcp__acie__find_references`/grep before this extraction), so this lives in
acie's shared top-level namespace instead of either call site's own
package, same reasoning as pytest_conventions.py's B2 extraction.

No PYTHONPATH/source-root config exists or can be assumed for an arbitrary
target repo, so `module_path_matches` treats a candidate's own file path as
authoritative and checks whether the imported dotted module_path equals it
or is one of its dotted *suffixes* -- this lets `from acie.daemon.discovery
import x` resolve against a candidate at `src/acie/daemon/discovery.py`
without ACIE ever knowing "src/" is a source root. (This is also what
closes the src-layout risk flagged in memory 271dc881 before C1 started:
the derivation was already suffix-tolerant, not an exact full-path match,
so no fix was needed -- see tests/test_module_paths.py's src-layout cases
for the empirical verification.)
"""


def path_to_dotted(file_path: str) -> str:
    """A file's own fully-qualified dotted module name, derived purely from
    its repo-relative path: `/` -> `.`, trailing `.py`/`__init__.py`
    stripped. No package-root stripping -- callers needing source-root
    tolerance use `module_path_matches`'s suffix check instead of relying
    on this being exact.
    """
    dotted = file_path
    if dotted.endswith("/__init__.py"):
        dotted = dotted[: -len("/__init__.py")]
    elif dotted.endswith(".py"):
        dotted = dotted[: -len(".py")]
    return dotted.replace("/", ".")


def module_path_matches(candidate_file_path: str, imported_module_path: str) -> bool:
    """Whether `imported_module_path` (a raw dotted string from an import
    statement) resolves to `candidate_file_path`: exact match, or a dotted
    suffix of the candidate's own path_to_dotted derivation.
    """
    dotted = path_to_dotted(candidate_file_path)
    return dotted == imported_module_path or dotted.endswith(f".{imported_module_path}")
