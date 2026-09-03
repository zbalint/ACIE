"""`.acie/config.json` layering schema + loader (v1 slice C4, wayfinder
ticket 47d8cd0d): this config file's first-ever real schema. Named layers
as path globs, plus a layer->layer allowed-dependency adjacency list --
the ticket resolution's exact wording (memory 5d8fa498's "Decisions so
far"). C5 (layering-violation detection, not yet built) will read
`architecture()`'s file-granularity edges and classify each endpoint via
this module to decide whether an edge crosses a disallowed layer boundary.

## Where the file lives

`<repo_root>/.acie/config.json` -- a user-owned, hand-editable, committable
file (ARCHITECTURE.md "State layout"), NOT `~/.acie` (ACIE's own generated
per-repo state directory, see repo_id.py). `repo_root` here is the same
value `resolve_repo_root`/dispatch.py's per-call store construction already
resolve; this module takes it as a plain parameter rather than re-deriving
it, matching every other repo-root-aware module in this codebase.

## Resolving the open design question (memories cf327766 / 95ced07b)

Both memories asked whether a layer boundary should reuse
`architecture()`'s directory-prefix `root` scoping (a path-segment prefix
check picked specifically because SQLite GLOB's `*`/`?`/`[`/`]` wildcards
would misinterpret a real repo path) or needs its own concept. It needs its
own: the ticket resolution says layers are **path globs**, and this
codebase already has an established, different mechanism for exactly that
-- `fnmatch.fnmatchcase`, the same one `structural_search.py`'s `path_glob`
parameter and dispatch.py's own file-walk filtering already use. (`find_
symbol`'s own `path_glob` parameter is NOT the same mechanism -- it
delegates to `SymbolStore.search`, which matches via SQLite's native
`GLOB` operator, a different matcher with its own syntax quirks; this
module deliberately follows `structural_search`/dispatch.py's convention,
not `find_symbol`'s.) Reusing `fnmatch.fnmatchcase` here is cheap and
consistent (no new pattern-matching machinery) and is a deliberately
different concept from `root`'s prefix check: `root` scopes "the whole
aggregation view", constrained to be a literal path (hence the SQLite-
wildcard-character concern); a layer glob is meant to *be* a wildcard
pattern the user writes on purpose. A trailing `/*` matches everything
under a directory (fnmatch's `*` crosses `/`, so `"src/api/*"` already
matches `"src/api/sub/handler.py"` with no `**` special case needed) but
not the bare directory path itself as a literal string, and does not match
a similarly-prefixed sibling (`"src/apiextra/...` does not match
`"src/api/*"`). **This is NOT the same as `.gitignore` semantics**, despite
the superficial resemblance: git's own wildmatch `*` does not cross `/` (a
gitignore pattern needs an explicit trailing `/**` to recurse), so a
`.acie/config.json` author coming from `.gitignore` habits should not
assume the two behave identically -- `fnmatch`'s `*` is unconditionally
recursive across `/`, which is *more* permissive than gitignore's, a real
divergence worth calling out rather than papering over.

## Validation philosophy

Unlike `daemon/discovery.py`'s `read_discovery_file` (a daemon-generated
file that degrades to `None` on any corruption, because a reader can
always trigger a respawn), `.acie/config.json` is hand-edited by a human --
a typo should fail loudly with a message pointing at the mistake, not
silently discard the user's layering rules. Malformed content therefore
raises `ValueError` (same choice `ir/symbol_id.py` already made for
malformed caller-supplied input), not a `None`/degraded return. Only the
file's outright *absence* returns `None`: `.acie/config.json` not existing
means "this repo has not opted into layering yet", a legitimate and
common state, not a mistake.

No `AcieToolError` subclass is added to `acie.tools.errors` here --
`errors.py`'s own docstring says its codes are "added alongside the tool
... that first needs them, each driven by its own failing test, not
declared speculatively ahead of time," and this slice adds no MCP tool.
C5's own tool implementation is the right place to decide how it maps a
`ValueError` from this loader onto a wire error code.
"""

import fnmatch
import json
import os
from dataclasses import dataclass, field

_ACIE_CONFIG_RELATIVE_PATH = os.path.join(".acie", "config.json")


@dataclass(frozen=True)
class LayerConfig:
    """A validated `.acie/config.json` layering section.

    `layers` maps a layer name to its list of fnmatch path-glob patterns
    (declaration order preserved from the JSON, though `classify_layers`
    below returns matches sorted for determinism rather than relying on
    it). `allowed_dependencies` maps a layer name to the list of layer
    names it may depend on; a layer absent as a key has no declared
    outbound dependencies other than itself (same-layer edges are always
    allowed implicitly -- see `is_dependency_allowed`).
    """

    layers: dict[str, list[str]] = field(default_factory=dict)
    allowed_dependencies: dict[str, list[str]] = field(default_factory=dict)


def load_layer_config(repo_root: str) -> LayerConfig | None:
    """Loads and validates `<repo_root>/.acie/config.json`.

    Returns `None` if the file does not exist. Raises `ValueError` (naming
    the offending key/file path) for any other missing-or-malformed
    condition: invalid JSON, a non-object top level, `layers`/
    `allowed_dependencies` not shaped as documented on `LayerConfig`, or an
    `allowed_dependencies` key/value naming a layer `layers` never
    declared.
    """
    config_path = os.path.join(repo_root, _ACIE_CONFIG_RELATIVE_PATH)
    try:
        with open(config_path, encoding="utf-8") as f:
            raw_text = f.read()
    except FileNotFoundError:
        return None

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{config_path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{config_path} must contain a JSON object at the top level")

    layers = _validate_layers(data.get("layers", {}), config_path)
    allowed_dependencies = _validate_allowed_dependencies(
        data.get("allowed_dependencies", {}), layers, config_path
    )
    return LayerConfig(layers=layers, allowed_dependencies=allowed_dependencies)


def _validate_layers(raw: object, config_path: str) -> dict[str, list[str]]:
    if not isinstance(raw, dict):
        raise ValueError(f"{config_path}: 'layers' must be a JSON object mapping layer name -> glob list")

    layers: dict[str, list[str]] = {}
    for name, globs in raw.items():
        if not isinstance(globs, list) or not all(isinstance(g, str) for g in globs):
            raise ValueError(f"{config_path}: layers[{name!r}] must be a list of glob-pattern strings")
        layers[name] = list(globs)
    return layers


def _validate_allowed_dependencies(
    raw: object, layers: dict[str, list[str]], config_path: str
) -> dict[str, list[str]]:
    if not isinstance(raw, dict):
        raise ValueError(f"{config_path}: 'allowed_dependencies' must be a JSON object mapping layer name -> layer list")

    allowed_dependencies: dict[str, list[str]] = {}
    for name, targets in raw.items():
        if name not in layers:
            raise ValueError(
                f"{config_path}: allowed_dependencies key {name!r} is not a layer declared in 'layers'"
            )
        if not isinstance(targets, list) or not all(isinstance(t, str) for t in targets):
            raise ValueError(f"{config_path}: allowed_dependencies[{name!r}] must be a list of layer-name strings")
        for target in targets:
            if target not in layers:
                raise ValueError(
                    f"{config_path}: allowed_dependencies[{name!r}] names undeclared layer {target!r}"
                )
        allowed_dependencies[name] = list(targets)
    return allowed_dependencies


def classify_layers(layer_config: LayerConfig, path: str) -> list[str]:
    """Every layer name whose glob list matches `path`, sorted for
    determinism. Empty when no declared layer's globs match -- `path` is
    simply outside this config's layering scope, not an error (mirrors
    `architecture.py`'s own "silently outside this view is not a gap"
    stance for out-of-scope paths).

    More than one match is a real, reported ambiguity (a config authoring
    overlap), never silently folded to one -- the same fan-out-don't-guess
    choice `architecture.py`'s `_resolve_import_target` already makes for
    an ambiguous dotted-name match. Folding this further, if ever needed,
    is left to C5's own judgment call, exactly as C1 left import-target
    folding to C2.
    """
    return sorted(
        name
        for name, globs in layer_config.layers.items()
        if any(fnmatch.fnmatchcase(path, pattern) for pattern in globs)
    )


def is_dependency_allowed(layer_config: LayerConfig, from_layer: str, to_layer: str) -> bool:
    """Whether an edge from `from_layer` to `to_layer` is allowed.

    A layer may always depend on itself, even if `allowed_dependencies`
    never lists it explicitly -- an intra-layer edge is not what
    layering rules exist to police. Otherwise `to_layer` must appear in
    `from_layer`'s declared dependency list; a `from_layer` with no entry
    at all in `allowed_dependencies` has no allowed outbound dependencies
    besides itself.
    """
    if from_layer == to_layer:
        return True
    return to_layer in layer_config.allowed_dependencies.get(from_layer, [])
