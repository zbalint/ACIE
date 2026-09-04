import logging
import time
from dataclasses import dataclass
from acie.daemon.bootstrap import make_index_job
from acie.daemon import dispatch
from acie.daemon.lsp_enrichment import run_enrichment_pass
from acie.daemon.pyright_process import PyrightProcessRegistry
from acie.daemon.write_queue import WriteQueue
from acie.indexer import IndexResult
from acie.repo_id import resolve_index_db_path, resolve_repo_id, resolve_repo_root
from acie.storage.connection import open_connection
from acie.storage.index_meta_store import IndexMetaStore
from acie.storage.relation_store import RelationStore
from acie.storage.symbol_store import SymbolStore

_logger = logging.getLogger(__name__)
# Keep this in parity with runtime.py's _SHUTDOWN_DRAIN_TIMEOUT_SECONDS.
_CLEANUP_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class ScanResult:
    repo_id: str
    repo_root: str
    files_scanned: int
    files_failed: int
    symbols_upserted: int
    relations_upserted: int
    relations_enriched: int
    elapsed_seconds: float


class ScanError(Exception):
    """path is not inside a git repository."""

def run_scan(repo_path: str, *, base_dir: str | None = None) -> ScanResult:
    started_at = time.monotonic()
    repo_id = resolve_repo_id(repo_path)
    repo_root = resolve_repo_root(repo_path)
    if repo_id is None or repo_root is None:
        raise ScanError(f"{repo_path!r} is not inside a git repository")
    db_path = resolve_index_db_path(repo_path, base_dir=base_dir)
    if db_path is None:
        raise ScanError(f"{repo_path!r} is not inside a git repository")

    write_queue = WriteQueue(db_path_for=lambda _repo_id: db_path)
    process_registry = PyrightProcessRegistry()
    try:
        files = list(dispatch.walk_repo(repo_root))
        _ = _run_pass(write_queue, repo_id, files)
        pass2_results = _run_pass(write_queue, repo_id, files)

        def _mark_job(conn) -> None:
            IndexMetaStore(conn=conn).mark_cross_file_pass_done()

        write_queue.submit(repo_id, _mark_job).result()

        read_conn = open_connection(db_path)
        try:
            symbol_store = SymbolStore(conn=read_conn)
            relation_store = RelationStore(conn=read_conn)
            resolved = run_enrichment_pass(
                repo_root=repo_root,
                repo_id=repo_id,
                process_registry=process_registry,
                write_queue=write_queue,
                walk_repo=dispatch.walk_repo,
                symbol_store=symbol_store,
                relation_store=relation_store,
            )
        finally:
            read_conn.close()

    finally:
        try:
            process_registry.close(timeout=_CLEANUP_TIMEOUT_SECONDS)
        finally:
            write_queue.close(timeout=_CLEANUP_TIMEOUT_SECONDS)

    return ScanResult(
        repo_id=repo_id,
        repo_root=repo_root,
        files_scanned=len(files),
        files_failed=sum(result is None for result in pass2_results),
        symbols_upserted=sum(
            result.symbols_upserted for result in pass2_results if result is not None
        ),
        relations_upserted=sum(
            result.relations_upserted for result in pass2_results if result is not None
        ),
        relations_enriched=len(resolved),
        elapsed_seconds=time.monotonic() - started_at,
    )


def _run_pass(
    write_queue: WriteQueue, repo_id: str, files: list[tuple[str, str]]
) -> list[IndexResult | None]:
    submitted = [
        (path, write_queue.submit(repo_id, make_index_job(path, source_text)))
        for path, source_text in files
    ]
    results: list[IndexResult | None] = []
    for path, future in submitted:
        try:
            results.append(future.result())
        except Exception:
            _logger.warning("Failed to index %s", path, exc_info=True)
            results.append(None)
    return results
