"""RPC dispatch: wires the 10 pure-function MCP tools to incoming requests.

See DAEMON.md "RPC Dispatch". Still no real socket/thread I/O in this
slice -- the daemon server's accept-loop/threading is a later slice.
`dispatch_request` is a callable layer: it takes an already-decoded
request envelope dict (see protocol.py's `build_request` shape) and
returns an already-encodable response envelope dict, doing real disk and
SQLite I/O in between (fresh-per-call store construction,
structural_search's file-reading seam) exactly as DAEMON.md's "RPC
Dispatch" and "Store lifecycle: fresh-per-call" sections describe.
"""

import fnmatch
import inspect
import os
from datetime import datetime, timezone
from typing import Callable

from acie.daemon.protocol import build_error_response, build_success_response
from acie.repo_id import resolve_index_db_path, resolve_repo_root
from acie.storage.index_meta_store import IndexMetaStore
from acie.storage.relation_store import RelationStore
from acie.storage.symbol_store import SymbolStore
from acie.tools.affected_tests import affected_tests
from acie.tools.architecture import architecture
from acie.tools.errors import AcieToolError
from acie.tools.explain import explain
from acie.tools.find_references import find_references
from acie.tools.find_symbol import find_symbol
from acie.tools.get_definition import get_definition
from acie.tools.graph import graph
from acie.tools.impact_analysis import impact_analysis
from acie.tools.list_imports import list_imports
from acie.tools.structural_search import structural_search

# Verbatim per DAEMON.md "RPC Dispatch" -- method equals the key exactly, no namespacing.
DISPATCH_TABLE: dict[str, Callable[..., dict]] = {
    "find_symbol": find_symbol,
    "get_definition": get_definition,
    "find_references": find_references,
    "list_imports": list_imports,
    "structural_search": structural_search,
    "graph": graph,
    "impact_analysis": impact_analysis,
    "explain": explain,
    "affected_tests": affected_tests,
    "architecture": architecture,
}

# Extensions/dirs skipped when structural_search's disk-I/O seam walks a
# repo -- ACIE v0 only parses Python (ARCHITECTURE.md), and dotted dirs
# (.git, .venv, ...) are never source a caller wants matched.
_SOURCE_EXTENSION = ".py"


def dispatch_request(request: dict, *, repo_ready: Callable[[str], bool], base_dir: str | None = None) -> dict:
    """Dispatch one decoded request envelope, returning a response envelope.

    `repo_ready` is a required seam, not a default-True convenience: the
    real per-repo readiness flag lives in the write-queue/bootstrap-index
    machinery DAEMON.md describes, which is out of this slice's scope (see
    "Bootstrap Indexing & INDEX_NOT_READY"). Its eventual daemon-server
    caller supplies the real flag; tests supply a fake one.
    """
    request_id = request.get("id")
    method = request.get("method")
    repo_path = request.get("repo_path")
    params = request.get("params")

    if not isinstance(method, str) or not method:
        return build_error_response(request_id, "MALFORMED_REQUEST", "method is required and must be a non-empty string")
    if not isinstance(repo_path, str) or not repo_path:
        return build_error_response(request_id, "MALFORMED_REQUEST", "repo_path is required and must be a non-empty string")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return build_error_response(request_id, "MALFORMED_REQUEST", "params must be a JSON object")

    # Per DAEMON.md "Bootstrap Indexing & INDEX_NOT_READY": checked before
    # DISPATCH_TABLE lookup or any store construction -- an unready repo
    # never opens a store connection at all.
    if not repo_ready(repo_path):
        return build_error_response(
            request_id, "INDEX_NOT_READY", f"repo at {repo_path!r} has not finished indexing yet",
            details={"index_generation": 0},
        )

    if method not in DISPATCH_TABLE:
        return build_error_response(request_id, "UNKNOWN_METHOD", f"no such method: {method}")

    db_path = resolve_index_db_path(repo_path, base_dir=base_dir)
    if db_path is None:
        return build_error_response(
            request_id, "MALFORMED_REQUEST", f"repo_path {repo_path!r} is not inside a git repository"
        )

    tool = DISPATCH_TABLE[method]
    symbol_store = SymbolStore(db_path)
    relation_store = RelationStore(db_path)
    index_meta_store = IndexMetaStore(db_path)

    try:
        result = _call_tool(
            tool, params, repo_path=repo_path,
            symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        )
    except AcieToolError as exc:
        return build_error_response(request_id, exc.code, str(exc))
    except Exception as exc:  # noqa: BLE001 -- per DAEMON.md "Error wrapping": anything not an AcieToolError maps to INTERNAL_ERROR.
        return build_error_response(request_id, "INTERNAL_ERROR", str(exc))

    return build_success_response(request_id, result)


def _call_tool(
    tool: Callable[..., dict], params: dict, *, repo_path: str,
    symbol_store: SymbolStore, relation_store: RelationStore, index_meta_store: IndexMetaStore,
) -> dict:
    """Injects each tool's own declared store/files/observed_at params.

    No per-tool kwarg-coercion wrapper (per DAEMON.md) -- this introspects
    each tool function's own signature (the same ground truth its own
    input validation already trusts) rather than hardcoding which of the 9
    tools needs which stores, which would drift as tools evolve.
    """
    sig_params = inspect.signature(tool).parameters
    kwargs = dict(params)
    if "symbol_store" in sig_params:
        kwargs["symbol_store"] = symbol_store
    if "relation_store" in sig_params:
        kwargs["relation_store"] = relation_store
    if "index_meta_store" in sig_params:
        kwargs["index_meta_store"] = index_meta_store
    if "files" in sig_params:
        # DAEMON.md "structural_search's disk-I/O seam": dispatch itself
        # reads matching files off disk, scoped by path_glob against the
        # repo root resolved from repo_path.
        repo_root = resolve_repo_root(repo_path)
        kwargs["files"] = _read_source_files(repo_root, params.get("path_glob"))
    if "repo_root" in sig_params:
        # architecture's C5 layering-violation detection reads
        # `<repo_root>/.acie/config.json` (acie.layer_config) -- the same
        # resolved repo root "files" already derives from repo_path above,
        # injected independently since not every tool needing one also
        # needs the other.
        kwargs["repo_root"] = resolve_repo_root(repo_path)
    if "observed_at" in sig_params and "observed_at" not in kwargs:
        kwargs["observed_at"] = datetime.now(timezone.utc).isoformat()
    return tool(**kwargs)


def _read_source_files(
    repo_root: str, path_glob: str | None, is_ignored: Callable[[str], bool] | None = None
) -> dict[str, str]:
    """`is_ignored`, when given, is a repo-relative-path predicate (e.g.
    ignore.IgnoreMatcher.matches) shared with the filesystem watcher -- see
    ignore.py's module docstring for why bootstrap and the watcher must
    agree on scope. Optional and defaults to "nothing ignored" so this
    function's other caller (structural_search's live disk-read seam in
    _call_tool, which was never part of that grilling decision) is
    unaffected unless a future change explicitly opts it in too.
    """
    files: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        if is_ignored is not None:
            rel_dirpath = os.path.relpath(dirpath, repo_root)
            # Pruned, not just filtered-later: real git doesn't descend
            # into an ignored directory to look for negated files inside
            # it either, so this matches actual gitignore semantics, not
            # just an efficiency shortcut.
            dirnames[:] = [
                d for d in dirnames
                if not is_ignored(d if rel_dirpath == "." else f"{rel_dirpath}/{d}")
            ]
        for filename in filenames:
            if not filename.endswith(_SOURCE_EXTENSION):
                continue
            abs_path = os.path.join(dirpath, filename)
            rel_path = os.path.relpath(abs_path, repo_root)
            if path_glob is not None and not fnmatch.fnmatchcase(rel_path, path_glob):
                continue
            if is_ignored is not None and is_ignored(rel_path):
                continue
            try:
                with open(abs_path, encoding="utf-8") as f:
                    files[rel_path] = f.read()
            except (UnicodeDecodeError, OSError):
                continue
    return files
