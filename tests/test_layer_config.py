"""Tests for acie.layer_config -- v1 slice C4 (wayfinder ticket 47d8cd0d):
`.acie/config.json`'s first-ever real schema, named layers as path globs
plus a layer adjacency list, and the loader that reads it.

See acie.layer_config's module docstring for the seam decisions this
closes, including the open design question from memories cf327766/95ced07b
(does a layer boundary reuse architecture()'s directory-prefix `root`
semantics, or need its own concept? -- it uses `fnmatch.fnmatchcase` path
globs, the same mechanism `structural_search`'s existing `path_glob`
parameter already uses (NOT `find_symbol`'s own `path_glob`, which is a
different mechanism, SQLite `GLOB`), a deliberately different and more
general concept than architecture()'s prefix scope).
"""

import json

import pytest

from acie.layer_config import LayerConfig, classify_layers, is_dependency_allowed, load_layer_config


def _write_config(tmp_path, payload: dict) -> None:
    acie_dir = tmp_path / ".acie"
    acie_dir.mkdir()
    (acie_dir / "config.json").write_text(json.dumps(payload))


# -- load_layer_config: file presence / absence --------------------------


def test_returns_none_when_acie_dir_does_not_exist(tmp_path):
    assert load_layer_config(str(tmp_path)) is None


def test_returns_none_when_config_json_does_not_exist(tmp_path):
    (tmp_path / ".acie").mkdir()
    assert load_layer_config(str(tmp_path)) is None


# -- load_layer_config: happy path ----------------------------------------


def test_loads_layers_and_allowed_dependencies(tmp_path):
    _write_config(
        tmp_path,
        {
            "layers": {"api": ["src/api/*"], "core": ["src/core/*"]},
            "allowed_dependencies": {"api": ["core"]},
        },
    )

    config = load_layer_config(str(tmp_path))

    assert config == LayerConfig(
        layers={"api": ["src/api/*"], "core": ["src/core/*"]},
        allowed_dependencies={"api": ["core"]},
    )


def test_missing_layers_key_defaults_to_empty_dict(tmp_path):
    _write_config(tmp_path, {})

    config = load_layer_config(str(tmp_path))

    assert config == LayerConfig(layers={}, allowed_dependencies={})


def test_missing_allowed_dependencies_key_defaults_to_empty_dict(tmp_path):
    _write_config(tmp_path, {"layers": {"api": ["src/api/*"]}})

    config = load_layer_config(str(tmp_path))

    assert config == LayerConfig(layers={"api": ["src/api/*"]}, allowed_dependencies={})


def test_a_layer_with_no_glob_patterns_yet_is_allowed(tmp_path):
    # A transitional state: the layer is named but nothing has moved into
    # it yet. Not an error -- see load_layer_config's docstring.
    _write_config(tmp_path, {"layers": {"planned": []}})

    config = load_layer_config(str(tmp_path))

    assert config == LayerConfig(layers={"planned": []}, allowed_dependencies={})


# -- load_layer_config: malformed content ---------------------------------


def test_malformed_json_raises_value_error(tmp_path):
    acie_dir = tmp_path / ".acie"
    acie_dir.mkdir()
    (acie_dir / "config.json").write_text("{not valid json")

    with pytest.raises(ValueError, match="not valid JSON"):
        load_layer_config(str(tmp_path))


def test_non_object_top_level_raises_value_error(tmp_path):
    _write_config_raw(tmp_path, "[1, 2, 3]")

    with pytest.raises(ValueError, match="JSON object"):
        load_layer_config(str(tmp_path))


def _write_config_raw(tmp_path, text: str) -> None:
    acie_dir = tmp_path / ".acie"
    acie_dir.mkdir()
    (acie_dir / "config.json").write_text(text)


def test_layers_value_must_be_an_object(tmp_path):
    _write_config(tmp_path, {"layers": ["api", "core"]})

    with pytest.raises(ValueError, match="layers"):
        load_layer_config(str(tmp_path))


def test_layer_glob_list_must_be_a_list(tmp_path):
    _write_config(tmp_path, {"layers": {"api": "src/api/*"}})

    with pytest.raises(ValueError, match="api"):
        load_layer_config(str(tmp_path))


def test_layer_glob_entries_must_be_strings(tmp_path):
    _write_config(tmp_path, {"layers": {"api": [1, 2]}})

    with pytest.raises(ValueError, match="api"):
        load_layer_config(str(tmp_path))


def test_allowed_dependencies_value_must_be_an_object(tmp_path):
    _write_config(tmp_path, {"layers": {"api": ["src/api/*"]}, "allowed_dependencies": ["api"]})

    with pytest.raises(ValueError, match="allowed_dependencies"):
        load_layer_config(str(tmp_path))


def test_allowed_dependencies_key_must_reference_a_declared_layer(tmp_path):
    _write_config(
        tmp_path,
        {"layers": {"api": ["src/api/*"]}, "allowed_dependencies": {"bogus": ["api"]}},
    )

    with pytest.raises(ValueError, match="bogus"):
        load_layer_config(str(tmp_path))


def test_allowed_dependencies_value_entries_must_reference_a_declared_layer(tmp_path):
    _write_config(
        tmp_path,
        {"layers": {"api": ["src/api/*"]}, "allowed_dependencies": {"api": ["bogus"]}},
    )

    with pytest.raises(ValueError, match="bogus"):
        load_layer_config(str(tmp_path))


def test_allowed_dependencies_value_must_be_a_list(tmp_path):
    _write_config(
        tmp_path,
        {"layers": {"api": ["src/api/*"], "core": ["src/core/*"]}, "allowed_dependencies": {"api": "core"}},
    )

    with pytest.raises(ValueError, match="api"):
        load_layer_config(str(tmp_path))


# -- classify_layers -------------------------------------------------------


def test_classify_layers_matches_a_single_layer_glob():
    config = LayerConfig(layers={"api": ["src/api/*"]}, allowed_dependencies={})

    assert classify_layers(config, "src/api/handler.py") == ["api"]


def test_classify_layers_returns_empty_list_when_no_glob_matches():
    config = LayerConfig(layers={"api": ["src/api/*"]}, allowed_dependencies={})

    assert classify_layers(config, "src/core/db.py") == []


def test_classify_layers_star_crosses_directory_separators():
    # fnmatch's "*" matches any characters including "/" -- a single-
    # segment "src/api/*" pattern already covers every nested file under
    # src/api, no "**" special case needed. See module docstring.
    config = LayerConfig(layers={"api": ["src/api/*"]}, allowed_dependencies={})

    assert classify_layers(config, "src/api/sub/handler.py") == ["api"]


def test_classify_layers_does_not_match_a_similarly_prefixed_sibling_directory():
    config = LayerConfig(layers={"api": ["src/api/*"]}, allowed_dependencies={})

    assert classify_layers(config, "src/apiextra/handler.py") == []


def test_classify_layers_returns_every_matching_layer_sorted_when_ambiguous():
    config = LayerConfig(
        layers={"zebra": ["src/shared/*"], "api": ["src/shared/*"]},
        allowed_dependencies={},
    )

    assert classify_layers(config, "src/shared/util.py") == ["api", "zebra"]


def test_classify_layers_pattern_matching_is_case_sensitive():
    config = LayerConfig(layers={"api": ["src/API/*"]}, allowed_dependencies={})

    assert classify_layers(config, "src/api/handler.py") == []


# -- is_dependency_allowed --------------------------------------------------


def test_is_dependency_allowed_true_for_same_layer_even_if_undeclared():
    config = LayerConfig(layers={"api": ["src/api/*"]}, allowed_dependencies={})

    assert is_dependency_allowed(config, "api", "api") is True


def test_is_dependency_allowed_true_when_declared():
    config = LayerConfig(
        layers={"api": ["src/api/*"], "core": ["src/core/*"]},
        allowed_dependencies={"api": ["core"]},
    )

    assert is_dependency_allowed(config, "api", "core") is True


def test_is_dependency_allowed_false_when_not_declared():
    config = LayerConfig(
        layers={"api": ["src/api/*"], "core": ["src/core/*"], "web": ["src/web/*"]},
        allowed_dependencies={"api": ["core"]},
    )

    assert is_dependency_allowed(config, "api", "web") is False


def test_is_dependency_allowed_false_when_source_layer_has_no_entry_at_all():
    config = LayerConfig(
        layers={"api": ["src/api/*"], "core": ["src/core/*"]},
        allowed_dependencies={},
    )

    assert is_dependency_allowed(config, "api", "core") is False
