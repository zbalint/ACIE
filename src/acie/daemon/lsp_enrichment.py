"""One bounded, opportunistic pyright enrichment pass for D3.

This module deliberately owns neither daemon triggering (D6) nor relation merge
policy (D4). It rechecks only unresolved or AMBIGUOUS calls/inherits sites,
then submits each unambiguous LSP definition through the existing WriteQueue.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlparse
from urllib.request import url2pathname

from acie.adapters.python.extract_relations import extract_relations_with_deferred_edges
from acie.daemon.lsp_client import LspClient, LspError
from acie.ir.relation import Relation
from acie.ir.symbol import Confidence, Provenance, Symbol
from acie.indexer import unresolved_deferred_sites
from acie.storage.relation_store import RelationStore
from acie.storage.symbol_store import SymbolStore

_logger = logging.getLogger(__name__)
_REQUEST_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True, order=True)
class _Site:
    source: str
    site_file: str
    site_line: int
    site_col: int
    predicate: str


def run_enrichment_pass(
    repo_root: str,
    repo_id: str,
    process_registry,
    write_queue,
    walk_repo: Callable[[str], Iterable[tuple[str, str]]],
    symbol_store: SymbolStore,
    relation_store: RelationStore,
    observed_at_fn: Callable[[], str] = lambda: datetime.now(timezone.utc).isoformat(),
) -> list[Relation]:
    """Enrich current unresolved calls/inherits from one fresh LSP conversation."""
    process = process_registry.ensure_process(repo_root)
    if process is None:
        _logger.warning("Skipping LSP enrichment for %r: no pyright process", repo_root)
        return []

    client = LspClient(process)
    try:
        try:
            initialize_result = client.initialize(repo_root)
        except (LspError, TimeoutError, ConnectionError):
            _logger.warning("Skipping LSP enrichment for %r: initialization failed", repo_root, exc_info=True)
            return []
        if not (client.server_capabilities or {}).get("definitionProvider"):
            _logger.warning("Skipping LSP enrichment for %r: definitionProvider is unavailable", repo_root)
            return []

        server_info = initialize_result.get("serverInfo", {})
        provider = server_info.get("name", "basedpyright") if isinstance(server_info, dict) else "basedpyright"
        version = server_info.get("version", "unknown") if isinstance(server_info, dict) else "unknown"
        files = list(walk_repo(repo_root))
        source_by_path = dict(files)
        sites = _worklist(files, symbol_store, relation_store, observed_at_fn())
        opened_uris: set[str] = set()
        submitted = []
        resolved: list[Relation] = []

        for site in sites:
            uri = (Path(repo_root) / site.site_file).resolve().as_uri()
            if uri not in opened_uris:
                source_text = source_by_path[site.site_file]
                client.send_notification(
                    "textDocument/didOpen",
                    {"textDocument": {"uri": uri, "languageId": "python", "version": 1, "text": source_text}},
                )
                opened_uris.add(uri)
            try:
                result = client.send_request(
                    "textDocument/definition",
                    {"textDocument": {"uri": uri}, "position": {"line": site.site_line - 1, "character": site.site_col}},
                ).result(timeout=_REQUEST_TIMEOUT_SECONDS)
            except ConnectionError:
                _logger.warning("Stopping LSP enrichment after connection failure at %s:%s", site.site_file, site.site_line)
                break
            except (LspError, TimeoutError):
                _logger.warning("Skipping LSP enrichment site %s:%s", site.site_file, site.site_line, exc_info=True)
                continue

            target = _target_for_definition(result, repo_root, symbol_store)
            if target is None:
                continue
            relation = Relation(
                source=site.source,
                target=target.id,
                predicate=site.predicate,
                site_file=site.site_file,
                site_line=site.site_line,
                site_col=site.site_col,
                confidence=Confidence.INFERRED,
                provenance=Provenance(provider=provider, version=version, observed_at=observed_at_fn()),
            )
            submitted.append(write_queue.submit(repo_id, _make_upsert_job(relation)))
            resolved.append(relation)

        if submitted:
            submitted[-1].result()
        return resolved
    finally:
        client.close()


def _worklist(
    files: list[tuple[str, str]], symbol_store: SymbolStore, relation_store: RelationStore, observed_at: str
) -> list[_Site]:
    sites: set[_Site] = set()
    for path, source_text in files:
        for relation in relation_store.list_by_site_file(path, predicates={"calls", "inherits"}):
            if relation.confidence == Confidence.AMBIGUOUS:
                sites.add(_Site(relation.source, relation.site_file, relation.site_line, relation.site_col, relation.predicate))
        _, deferred_calls, deferred_inherits, _ = extract_relations_with_deferred_edges(path, source_text, observed_at)
        unresolved = unresolved_deferred_sites(deferred_calls, deferred_inherits, symbol_store)
        sites.update(_Site(item.source, item.site_file, item.site_line, item.site_col, "calls") for item in unresolved.calls)
        sites.update(_Site(item.source, item.site_file, item.site_line, item.site_col, "inherits") for item in unresolved.inherits)
    return sorted(sites)


def _target_for_definition(result, repo_root: str, symbol_store: SymbolStore) -> Symbol | None:
    locations = [result] if isinstance(result, dict) else result
    if not isinstance(locations, list) or len(locations) != 1:
        return None
    location = locations[0]
    if not isinstance(location, dict):
        return None
    uri, selection_range = _location_target(location)
    if uri is None or selection_range is None:
        return None
    target_path = _relative_path_from_uri(uri, repo_root)
    start = selection_range.get("start") if isinstance(selection_range, dict) else None
    if target_path is None or not isinstance(start, dict):
        return None
    line, col = start.get("line"), start.get("character")
    if not isinstance(line, int) or not isinstance(col, int):
        return None
    return symbol_store.at_position(path=target_path, line=line + 1, col=col)


def _location_target(location: dict) -> tuple[str | None, dict | None]:
    if "uri" in location and "range" in location:
        return location["uri"], location["range"]
    if "targetUri" in location and "targetSelectionRange" in location:
        return location["targetUri"], location["targetSelectionRange"]
    return None, None


def _relative_path_from_uri(uri: str, repo_root: str) -> str | None:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    try:
        return Path(url2pathname(parsed.path)).resolve().relative_to(Path(repo_root).resolve()).as_posix()
    except ValueError:
        return None


def _make_upsert_job(relation: Relation):
    def job(conn) -> None:
        RelationStore(conn=conn).upsert(relation)

    return job
