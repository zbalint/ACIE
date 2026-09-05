"""One bounded, opportunistic pyright enrichment pass for D3.

This module deliberately owns neither daemon triggering (D6) nor relation merge
policy (D4). It rechecks only unresolved or AMBIGUOUS calls/inherits sites,
then submits LSP definitions and H2 composition inferences through the existing
WriteQueue.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlparse
from urllib.request import url2pathname

from acie.adapters.python.extract_relations import extract_relations_with_deferred_edges
from acie.daemon import merge_policy
from acie.daemon.lsp_client import LspClient, LspError
from acie.ir.relation import DeferredImportSelfCall, Relation
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



@dataclass(frozen=True)
class _MixinSite:
    source: str
    enclosing_class: str
    method_name: str
    site_file: str
    site_line: int
    site_col: int


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
            if isinstance(site, _MixinSite):
                candidates = _composition_method_candidates(site, relation_store, symbol_store)
                if not candidates:
                    continue
                confidence = Confidence.INFERRED if len(candidates) == 1 else Confidence.AMBIGUOUS
                for target in candidates:
                    relation = Relation(
                        source=site.source,
                        target=target.id,
                        predicate="calls",
                        site_file=site.site_file,
                        site_line=site.site_line,
                        site_col=site.site_col,
                        confidence=confidence,
                        provenance=Provenance(provider=provider, version=version, observed_at=observed_at_fn()),
                    )
                    submitted.append(write_queue.submit(repo_id, _make_merge_job(relation)))
                    resolved.append(relation)
                continue
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
            submitted.append(write_queue.submit(repo_id, _make_merge_job(relation)))
            resolved.append(relation)

        if submitted:
            submitted[-1].result()
        return resolved
    finally:
        client.close()


def _worklist(
    files: list[tuple[str, str]], symbol_store: SymbolStore, relation_store: RelationStore, observed_at: str
) -> list[_Site | _MixinSite]:
    sites: set[_Site] = set()
    mixin_sites: set[_MixinSite] = set()
    for path, source_text in files:
        for relation in relation_store.list_by_site_file(path, predicates={"calls", "inherits"}):
            if relation.confidence == Confidence.AMBIGUOUS:
                sites.add(_Site(relation.source, relation.site_file, relation.site_line, relation.site_col, relation.predicate))
        (
            extracted_relations,
            deferred_calls,
            deferred_inherits,
            _,
            deferred_self_calls,
        ) = extract_relations_with_deferred_edges(path, source_text, observed_at)
        unresolved = unresolved_deferred_sites(
            deferred_calls, deferred_inherits, symbol_store, deferred_self_calls
        )
        extracted_h1_call_sites = {
            (relation.source, relation.site_file, relation.site_line, relation.site_col)
            for relation in extracted_relations
            if relation.predicate == "calls"
        }
        unresolved_self_calls = {
            item for item in unresolved.calls if isinstance(item, DeferredImportSelfCall)
        }
        h1_resolved_self_call_sites = {
            (item.source, item.site_file, item.site_line, item.site_col)
            for item in deferred_self_calls
            if item not in unresolved_self_calls
        }
        for item in unresolved.calls:
            site_key = (item.source, item.site_file, item.site_line, item.site_col)
            if isinstance(item, DeferredImportSelfCall):
                if (
                    item.enclosing_class is not None
                    and site_key not in extracted_h1_call_sites
                    and site_key not in h1_resolved_self_call_sites
                ):
                    mixin_sites.add(
                        _MixinSite(
                            source=item.source,
                            enclosing_class=item.enclosing_class,
                            method_name=item.method_name,
                            site_file=item.site_file,
                            site_line=item.site_line,
                            site_col=item.site_col,
                        )
                    )
                elif item.enclosing_class is None:
                    sites.add(_Site(item.source, item.site_file, item.site_line, item.site_col, "calls"))
            else:
                sites.add(_Site(item.source, item.site_file, item.site_line, item.site_col, "calls"))
        sites.update(_Site(item.source, item.site_file, item.site_line, item.site_col, "inherits") for item in unresolved.inherits)
    mixin_site_keys = {
        (site.source, site.site_file, site.site_line, site.site_col) for site in mixin_sites
    }
    sites = {
        site
        for site in sites
        if (site.source, site.site_file, site.site_line, site.site_col) not in mixin_site_keys
    }
    return sorted(sites) + sorted(mixin_sites, key=lambda site: (site.site_file, site.site_line, site.site_col, site.source))


def _composition_method_candidates(
    site: _MixinSite, relation_store: RelationStore, symbol_store: SymbolStore
) -> list[Symbol]:
    candidates: dict[str, Symbol] = {}
    for composition in relation_store.list_by_target(site.enclosing_class, predicates={"inherits"}):
        for base_relation in relation_store.list_by_source(composition.source, predicates={"inherits"}):
            if base_relation.target == site.enclosing_class:
                continue
            base = symbol_store.get(base_relation.target)
            if base is None:
                continue
            for method in symbol_store.find_by_qualname_and_kind(
                f"{base.qualname}.{site.method_name}", kind="method"
            ):
                if method.path == base.path:
                    candidates[method.id] = method
    return [candidates[target] for target in sorted(candidates)]


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


def _make_merge_job(relation: Relation):
    def job(conn) -> merge_policy.MergeOutcome:
        return merge_policy.apply_enrichment_write(RelationStore(conn=conn), relation)

    return job
