# Implementation Spec: v1 Slice C6 — Cycle Detection (`cycles` field)

**Status: spec ready for implementation, not yet built.** Written for
hand-off to a different implementing agent; review happens in a fresh
session afterward (Claude, in this project's usual review role — see
"Workflow reminders" below). Do not skip straight to coding without reading
`src/acie/tools/architecture.py`'s module docstring in full first — this
spec extends its numbered decision list (currently 1-14) and assumes the
same vocabulary (`in_scope`, file vs. package granularity, the C1 dotted-
name index, etc.) without re-explaining it.

## Context

Wayfinder ticket `47d8cd0d` ("What schema and MCP tool surface support
architecture-level queries?", map `5d8fa498` "[ACIE] Wayfinder Map: v1
design spec", context_id `wayfinder:acie:v1-design-spec`) locked the
`architecture` MCP tool's shape. Capability C has six slices; C1-C5 are
done and pushed (commits `8183c78`, `260caec`, `48cfec3`, `bf2a5ac`,
`933e2d9`). **C6 is the last one**: "cycle detection (DFS/SCC) over
architecture's file-granularity edges, producing a `cycles` field" — the
exact scope named when C5 was committed (memory `c125ccdf`). Once C6 lands,
Capability C is complete; do not start Capability D (LSP/pyright
enrichment) in the same pass.

The C2 section of `architecture.py`'s own module docstring already
foreshadowed this: keeping a genuine same-file self-import as a real
`source == target` edge (decision 3) was explicitly justified as "rather
than ... hide a genuine one-node cycle from C6's future cycle detection."
This spec makes good on that.

## Design decisions (continuing the numbering in `architecture.py`'s module
docstring — C5 ended at 14; this is 15 onward)

15. **Cycles are always computed at file granularity**, reusing the same
    file-to-file edge set `_detect_layer_violations` already uses (see 23
    below) — identical precedent to decision 12 (layer violations always
    classify at file level regardless of the requested `granularity`).
    Rationale, specific to cycles: a package-level edge is created whenever
    *any* file in directory A imports *any* file in directory B (C3
    decision 10 already drops same-directory edges as self-loops, but
    cross-directory aggregation is a many-to-many fold). Two directories
    can show edges in both directions — `pkg/a/x.py -> pkg/b/y.py` and
    `pkg/b/z.py -> pkg/a/w.py` — with **no single file actually part of a
    real loop** (`x.py`'s import chain never comes back to `x.py`). Running
    SCC on package-rolled-up edges would report that as a cycle; it isn't
    one. File granularity is the only level where "cycle" means what it
    actually should: an ordered chain of real imports that returns to
    where it started.
16. **`cycles` is present in the envelope unconditionally**, on every
    `architecture()` call regardless of `granularity`, and regardless of
    whether `.acie/config.json`/layering is configured — unlike
    `layer_violations` (opt-in, gated on `layer_config is not None`),
    cycle detection needs no external config; it's a pure property of the
    already-indexed import graph. `layering_enabled` staying `False` does
    not suppress `cycles`.
17. **`cycles` ignores `node_cap`**, computed over the full (still
    `root`-scoped) `in_scope` set — identical rationale to decision 13: a
    display-size cap silently hiding a real cycle from the caller is a
    worse failure mode than the extra walk. Still respects `root` (an edge
    to a file outside `root`'s scope was already dropped per decision 4,
    so a "cycle" that only closes by routing through an out-of-scope file
    is correctly not detected — this falls out of `in_scope` being computed
    once, upstream of everything, not a case needing special-cased logic).
18. **Algorithm: Tarjan's strongly-connected-components algorithm**, one
    DFS pass, O(V+E) — matches the "DFS/SCC" framing this slice was scoped
    with from the start. **Implement it iteratively (an explicit stack),
    not recursively.** Python's default recursion limit (1000) is a real
    risk here: a repo with a long, purely linear import chain and zero
    actual cycles (>1000 files, no branching) would blow a recursive
    implementation even though there's nothing genuinely circular to
    report. Add a new private helper, `_find_cycles(file_edges: set[tuple[str,
    str]], node_paths: set[str]) -> list[list[str]]`, colocated in
    `architecture.py` next to `_compute_file_edges`/`_detect_layer_violations`
    — don't split it into a separate module; nothing else in ACIE needs
    general SCC computation yet, and the module docstring already cites
    "wait for a real 2nd caller" as this codebase's extraction trigger
    (`module_paths.py`'s own history). `node_paths` is needed alongside
    `file_edges` because a node with zero edges (no self-loop, no
    neighbors) must never appear in the output — Tarjan's algorithm only
    ever visits nodes reachable via the edge set, but a defensive isolated
    check is worth keeping explicit in the implementation rather than
    relying on that as an unstated invariant.
19. **A cycle entry is the full membership of one non-trivial strongly
    connected component**: `{"nodes": [file paths]}`. "Non-trivial" means
    SCC size ≥ 2 (a genuine multi-file loop), OR SCC size == 1 **with** a
    self-loop edge (`source == target`, the real self-import edge decision
    3 already keeps) — a lone file with no self-loop is never a cycle.
    Report full SCC membership, not one arbitrary simple cycle path picked
    out of it: once an SCC has more than 2 members it can contain many
    distinct simple cycles, and choosing one to display would be exactly
    the "silently guess at one answer" move this codebase's standing stance
    already rejects elsewhere (decision 5's ambiguous-import fan-out,
    decision 14's report-every-real-combination stance for layer
    violations). SCC membership is the only representation that's both
    correct and doesn't privilege one arbitrary path over another.
20. **Determinism**: each cycle's own `nodes` list is sorted alphabetically;
    the top-level `cycles` list is then sorted by its first (i.e. smallest)
    member path. (Two distinct SCCs can never share a first member, so no
    further tiebreak is needed.)
21. **`full` has no effect on cycle entries** — an SCC membership list has
    no per-node confidence/provenance concept to reveal, the same no-op
    stance decision 3 gives file-granularity edges and decision 9 gives
    package nodes.
22. **No new error codes.** Cycle detection can't fail on its own: no
    external file to be malformed, no new argument to validate beyond what
    `architecture()` already checks.
23. **Refactor: hoist the full file-edge computation out of
    `_detect_layer_violations` into `architecture()` itself, computed
    once, unconditionally, per call.** Today, `_compute_file_edges(in_scope,
    ...)` is only invoked from inside `_detect_layer_violations`, and only
    when `layer_config is not None` (the function returns `[]` immediately
    otherwise, per its own docstring's "costs nothing when `layer_config`
    is `None`" claim). C6 needs that exact same full, root-scoped,
    untruncated file-edge set unconditionally on every call. Computing it
    twice — once (conditionally) for layering, once (always) for cycles —
    would be pure waste whenever both features are active on the same
    call. So: call `_compute_file_edges(in_scope, relation_store,
    dotted_name_index)` once in `architecture()`, pass the resulting
    `file_edges` into both `_detect_layer_violations(file_edges,
    layer_config)` (signature changes: no longer takes `in_scope`/
    `relation_store`/`dotted_name_index` and no longer recomputes them
    internally) and `_find_cycles(file_edges, node_paths)`. This is a
    genuine second-caller trigger for the hoist, the same "wait for a real
    2nd caller" pattern decision 18 above invokes to justify **not**
    splitting `_find_cycles` into its own module — here the second caller
    has actually arrived, so the hoist is warranted, not premature.
    **Update `_detect_layer_violations`'s docstring**: the line claiming
    "costs nothing when `layer_config` is `None`" is no longer accurate
    post-C6 (the edge walk itself is now unconditional, shared with
    cycles) — rewrite it to say the *violation-detection* work is skipped
    when `layer_config is None` (returns `[]` immediately without
    iterating `file_edges`), not that the underlying walk is skipped.

## Envelope shape after C6

```
{index_generation, root, granularity, nodes, edges, node_cap, truncated,
 layering_enabled, layer_violations, cycles}
```

`cycles: list[dict]`, each `{"nodes": [file_path, ...]}` — `[]` when no
cycles exist. No new parameters on `architecture()`'s own signature: unlike
layering (C5), which needed a new `repo_root` dispatch-injected seam,
cycle detection needs nothing new — it's computed purely from data
`architecture()` already has in scope by the time C5's code runs
(`in_scope`, `relation_store`, `dotted_name_index`), no external file to
read, no new caller-facing knob.

## Files to touch

1. **`src/acie/tools/architecture.py`**
   - `_find_cycles(file_edges, node_paths) -> list[list[str]]` (Tarjan's,
     iterative). Returns already-sorted-per-entry, sorted-overall (decision
     20) — don't leave sorting to the caller.
   - `architecture()`: compute `file_edges` once (decision 23), call
     `_find_cycles`, add `"cycles"` to the returned dict.
   - `_detect_layer_violations`: drop its own internal
     `_compute_file_edges` call; take `file_edges` as a parameter instead.
     Update its docstring per decision 23's last paragraph.
   - Module docstring: update the top-of-file status line (currently
     "**Slices C1-C5** -- cycle detection (C6, a `cycles` field) is not yet
     built") to reflect C6 being done, and add a new "## C6: cycle
     detection" section transcribing decisions 15-23 above in the same
     style as the existing C2/C3/C5 sections (numbered decisions continuing
     the file's own running list, not restarting at 1).
2. **`tests/tools/test_architecture.py`** — see test list below. Follow
   this file's existing helper conventions (`_stores()`, `_index(...)`) and
   docstring-comment-per-test style already used throughout.
3. **`ARCHITECTURE.md`** — the Capability C paragraph currently ends
   "**C5** wires this loader into the `architecture` tool ... Remaining:
   **C6** cycle detection (DFS/SCC) → `cycles` field." Update this to
   describe C6 as done, in the same one-paragraph-per-slice density the
   rest of that entry already uses (it's a single long paragraph covering
   C1-C5 today — C6 continues the same paragraph, doesn't start a new
   section).

## Test list (new, in `tests/tools/test_architecture.py`)

- `test_no_cycles_produces_an_empty_cycles_list`
- `test_two_files_importing_each_other_produce_a_two_node_cycle`
- `test_three_files_in_a_directed_ring_are_reported_as_one_cycle`
- `test_a_file_importing_itself_is_reported_as_a_one_node_cycle` (reuses the
  same self-loop fixture shape as the existing
  `test_a_file_importing_from_itself_produces_a_self_loop_edge`)
- `test_a_file_with_no_self_loop_and_no_incoming_edge_is_not_a_cycle`
- `test_a_diamond_import_shape_produces_no_cycle` (A→B, A→C, B→D, C→D — no
  loop, despite four edges and shared endpoints)
- `test_two_independent_cycles_are_both_reported`
- `test_a_larger_strongly_connected_component_is_reported_with_all_members`
  (4+ node SCC — asserts full membership, not just one path through it)
- `test_cycle_nodes_are_sorted_alphabetically_within_each_cycle`
- `test_cycles_list_is_sorted_by_first_member_for_determinism`
- `test_cycles_are_computed_at_file_granularity_even_when_package_granularity_is_requested`
- `test_cycles_are_not_scoped_by_node_cap`
- `test_cycles_respect_root_scope` (a real cycle entirely outside `root` is
  not reported)
- `test_a_cycle_broken_by_root_scope_is_not_reported` (one member of what
  would be a real cycle falls outside `root` — the edge into it was already
  dropped per decision 4, so no cycle is detected; this is a regression
  guard on decision 17's "falls out of `in_scope`, not special-cased"
  claim, not new behavior to build)
- `test_cycles_present_even_with_no_acie_config` (layering disabled,
  `cycles` still populated — decision 16)
- `test_full_true_has_no_effect_on_cycle_entries`
- `test_layer_violations_still_correct_after_the_file_edges_hoist` (a
  regression guard: re-run one or two of the existing C5 layering tests
  conceptually to confirm decision 23's refactor didn't change
  `layer_violations` behavior — reuse existing fixtures/assertions rather
  than inventing new ones if the existing C5 tests already cover this
  adequately after the signature change)

## Workflow reminders for the implementing agent

- Repo TDD convention: write the failing tests first, then implement —
  see this project's `tdd` skill and memory `9b020543` ("v1 Implementation
  Workflow: One Slice Per Session, Stop Before Commit for Review").
- **This is one slice.** Per that same memory: implement C6, run the
  **full** test suite (`cd /home/zbalint/workspace/ACIE && .venv/bin/pytest`),
  not just the new tests, and **stop before committing** — leave the diff
  uncommitted for review in a fresh session. Do not `git commit` or push.
- Expect the pre-existing daemon/MCP-transport election-port flake (2
  failures, tracked since slice A1, confirmed unrelated — see memory
  `c125ccdf`) and nothing else. Any *other* new failure must be root-caused
  and fixed, not waved through as "probably that flake."
- Do not touch `_DAEMON_INJECTED_PARAMETERS`, `dispatch.py`, or
  `mcp_server.py` — C6 needs no new dispatch-injected seam (see "Envelope
  shape" above). Needing one would mean the implementation has drifted
  from this spec; stop and flag it rather than improvise past it.
- Do not start Capability D (LSP/pyright enrichment) in this pass — C6
  finishes Capability C; stop there.
- Standing dogfooding ground rule (map `5d8fa498`'s Notes) still applies
  unchanged: the live `mcp__acie__*` MCP tools stay v0-pinned throughout
  (dev-repo edits are inert to them until the eventual end-of-v1 cutover,
  which hasn't happened yet — capability D isn't started). Fine to use them
  as the working code-intelligence tool while implementing; spot-check any
  result against ground truth before it drives a consequential decision
  (a rename, a blast-radius-driven change).

## Acceptance criteria

- All new tests above pass; full existing suite still passes with only the
  known pre-existing flake.
- `cycles` present in every `architecture()` response; `[]` when none
  exist; correct membership and ordering per decisions 19-20.
- `architecture.py`'s module docstring gains the "## C6" section; top-of-
  file status line updated; `ARCHITECTURE.md`'s Capability C paragraph
  marks C6 done.
- No new MCP-facing parameters, no new error codes, no `dispatch.py`/
  `mcp_server.py` changes.
- Diff left uncommitted, ready for review.
