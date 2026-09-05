# ACIE External Ground-Truth Validation Report

**Date:** 2026-09-05
**Target:** SALTMDB (`/home/zbalint/workspace/SALTMDB`, commit `4835c99`, branch `develop`, clean tree)
**Scope closed:** `b79e087e` item 4 — "external/adversarial ground-truth validation," the single highest-priority, never-executed item on ACIE's own test matrix per the `80b11cee` verified-current-state snapshot. Every ACIE accuracy claim up to and including F1 had only ever been checked against ACIE's own codebase; this is the first pass against a real, unrelated, differently-authored/differently-styled/differently-aged codebase.

Full evidence trail lives in SALTMDB (shared, cross-agent) memory `aa8679e0-5689-46d0-876c-9faaa4af3235` and three linked bug-specific memories (`20556be1`, `febf3e07`, `5021d1a9`). This document is a standalone summary for the ACIE repo itself, following the existing `LIVE_MCP_QUALIFICATION_REPORT.md` convention.

## Method

1. Loaded the `acie-usage` skill for trust-boundary rules — every claim below was independently verified against direct `grep`/`Read` of SALTMDB's source, never trusted from a single ACIE tool call, and every miss was cross-validated through a second, independent tool path (`find_references` **and** `get_definition(position=...)`) before being recorded as a finding.
2. `acie scan /home/zbalint/workspace/SALTMDB --json` → `repo_id e1b10a4f6cee5348`, 269 files scanned, **0 failed**, 4140 symbols upserted, 13507 relations upserted, 595 enriched, 50.7s.
3. Confirmed the index actually targeted SALTMDB (not ACIE's own tree): `find_symbol("store_relation")` / `find_symbol("bulk_store_relations")` returned `src/saltmdb/domain/services/relation_service.py` at lines 134 and 1821 — exactly matching an independent `grep`. File-count shape matched the task's stated target (72 real `src/saltmdb/**/*.py` files, 127 `tests/**/*.py` files).
4. Worked the full checklist: exact symbol/definition resolution across ~15 functions/classes in ~12 files; cross-file `calls`/`references` for bare imports, `self.method()`, and the from-import-submodule attribute pattern Capability F1 targets; deliberate attempts at the two gaps F1's own review left open (locally-constructed-object-then-method, `ClassName.method()`/multi-level dotted chains); aliased imports, relative imports, decorators, duplicate names across nested scopes, and a constructed malformed-syntax file; `list_imports`, `structural_search`, `graph`/`impact_analysis`/`affected_tests` on a real caller chain; `architecture` layering/dependency queries.

## Headline result

**Not a uniform verdict.** Symbol/definition extraction itself is rock solid externally. Several specific relation-resolution capabilities — the exact narrow slice they were built and reviewed against — hold up. But three previously undocumented, common-pattern gaps in cross-file call resolution were found, none of which self-hosted dogfooding against ACIE's own codebase had ever surfaced, plus one tool-ergonomics bug in `architecture`.

### What held up

- **Exact symbol/definition line-range resolution: 100% agreement** with source across every sample checked. Every gap found below was in cross-file relation *resolution*, never in the underlying tree-sitter symbol extraction.
- **F1's narrowest supported case works**: absolute, non-aliased, module-level `from pkg import mod; mod.func()` / `mod.Class()` resolves correctly — `telemetry_service.Timer()`, `telemetry_service.classify_result(...)` (`daemon/dispatch.py:494,504`), and `memory_service.archive_memory(...)` (`daemon/dispatch.py:224-227`, correctly following the `memory_service` package's own `__init__.py` re-export from `.lifecycle`) all resolved cross-file.
- **`impact_analysis`/`affected_tests`** on `resolve_or_create_tag` correctly surfaced a real, non-trivial caller chain (`relation_service.py:consolidate_memories` → 59 real affected tests across 8 test files).
- **Duplicate top-level names**: `_hash`/`_sha` appear (with different implementations) in ~9 different SALTMDB test/script files; `find_symbol` disambiguated every one correctly, zero cross-file leakage.
- **`structural_search`** (native tree-sitter `.scm` queries, not ast-grep) correctly matched all 18 `@mcp.tool()`-decorated functions in `mcp/tools.py`, including one with a 50+ line multi-line-string decorator argument, capturing the full span and correct function name.
- **`architecture`'s cycle detection** found a real cycle (`mcp/server.py` <-> `mcp/tools.py`), independently confirmed by grep.
- The two cases F1's own review (`53e94fc1`) already named as deliberately deferred behaved as documented: a locally-constructed object then `.method()` (`timer = telemetry_service.Timer(); timer.elapsed_ms()`) is still silently dropped — confirmed, not a surprise.

### New gaps found (not previously documented)

**1. F1's cross-module attribute-call fix silently drops three common import styles** (memory `20556be1`):

| Pattern | Example (SALTMDB) | Result |
|---|---|---|
| Relative import, any aliasing | `from . import tags as tag_ops` (`write.py:19`) → `tag_ops.resolve_or_create_tag(...)` (`write.py:518,538`) | **Missed** |
| Relative import, non-aliased | `from . import lifecycle` (`duplicates.py:24`) → `lifecycle.archive_memory(...)` (`duplicates.py:307`) | **Missed** |
| Aliased absolute import | `from saltmdb.daemon import client as daemon_client` (`tools.py:6`) → `daemon_client.call(...)` (`tools.py:166,175`) | **Missed** |
| Function-local import (breaking a real import cycle) | `from saltmdb.mcp.identity import SESSION_IDENTITY` inside a function body (`__main__.py:59`) → `SESSION_IDENTITY.configure_owner(...)` (`__main__.py:62`) | **Missed** |
| Same import, module-level | identical import at `mcp/server.py:8` → `SESSION_IDENTITY.configure_owner(...)` (`server.py:34`) | Resolved correctly |

`list_imports` itself confirms the root cause at the extraction level, not just resolution: `write.py:16`'s single import statement `from saltmdb.utils.envelope import error as envelope_error, rejected, ok as envelope_ok, warning` returns only the two **non-aliased** names (`rejected`, `warning`) from `list_imports` — the two aliased names from the *same statement* are silently dropped. Relative imports are entirely absent from `list_imports` output in every file checked (write.py, duplicates.py, tools.py).

None of these three variants (relative, aliased, function-local) were named in F1's own documented deferral list (`53e94fc1`: locally-constructed object, multi-level dotted chain, `ClassName.method()`), and all three are idiomatic, common patterns — SALTMDB uses all three for legitimate reasons (package-internal relative imports, aliasing for readability/collision-avoidance, and a function-local import specifically to break the real `mcp/server.py`↔`mcp/tools.py` cycle `architecture` itself detects).

**2. Capability A (inheritance/overrides) misses `self.<method>()` across real multi-inheritance mixin composition** (memory `febf3e07`): `src/saltmdb/viewer/routes/__init__.py` composes `SALTMDBHandler` from 8 mixins across 8 files plus `ViewerHandlerBase`. `ViewerHandlerBase.send_json` has 56+ real `self.send_json(...)` call sites across those mixin files; `find_references` found only the 4 same-file ones (93% miss rate on this symbol). Independently confirmed via `get_definition(position=...)` at a call site returning `SYMBOL_NOT_FOUND`. Distinct from the already-fixed `f9a17c62` leak bug (that was wrong-answer leakage; this is pure invisibility).

**3. `architecture(root=...)` silently returns empty for an absolute path** (memory `5021d1a9`) — even the exact `repo_root` `acie scan` itself just reported. Only a repo-relative root (matching every other tool's own path convention) returns real data. No error, no distinguishing signal from "genuinely no dependencies."

## What this pass could not test

- **Malformed-syntax-file resilience**: constructed a throwaway git repo with a deliberately broken `.py` file (unbalanced parens, missing colons) under the session scratchpad and scanned it via the `acie scan` CLI (clean exit, `files_failed: 0`). However, the MCP query tools used in this session (`find_symbol`, `find_references`, etc.) turned out to be bound to a single fixed repo context (SALTMDB) with no `repo`/`root`-selection parameter — querying the scratch repo's own symbols to check partial-file-skip behavior was not possible from this session. Recorded as an operational limitation, not a finding either way.
- **`ClassName.method()` / true multi-level (3+) dotted chains**: SALTMDB's own style never organically produces these forms (consistently uses `from x import name` + bare calls, or single-level `module.attr()`); synthetic construction was blocked by the same repo-binding limitation above. Inconclusive-by-absence, not confirmed.
- **Concurrency/multi-repo isolation and performance-at-scale** (`80b11cee` gaps 4b/4c) were out of scope for this pass.

## Bottom line

External validation earns genuine confidence in ACIE's core symbol-extraction accuracy and in the specific capability slices whose test suites happen to match real usage. It also demonstrates exactly why this test was overdue: three of the four new findings are cases where ACIE's own codebase simply doesn't exercise the pattern that broke on someone else's code. Recommend building the next round of Capability F/A regression tests directly from SALTMDB's shapes (mixin composition across N files, relative+aliased+local imports) rather than from patterns invented for ACIE's own style.

*Per the acie-usage skill, nothing here was fixed as part of this pass — findings are recorded for whoever picks up relation-extraction/resolution coverage next. See linked SALTMDB memories for full per-finding evidence and recommendations.*
