"""Tests for ProviderManager -> Claude `--settings` env overlay.

The overlay pins the active profile's provider/model env vars at the highest
("flag") settings layer so they override `~/.claude/settings.json` while
`setting_sources=["user","project"]` still inherits skills/memory/plugins.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src.config.providers import ProviderManager


def _seed(tmp_path: Path, profiles: dict, active: str):
    """Construct a ProviderManager from a pre-written providers.json."""
    storage = tmp_path / "providers.json"
    storage.write_text(
        json.dumps({"active": active, "profiles": profiles}),
        encoding="utf-8",
    )
    config = SimpleNamespace(claude_model=None, anthropic_api_key_str=None)
    return ProviderManager(config, storage_path=storage)


_FULL = {
    "name": "cpa",
    "base_url": "https://cpa.example",
    "auth_token": "tok-cpa",
    "api_key": None,
    "default_model": "mimo[1m]",
    "opus_model": "mimo[1m]",
    "sonnet_model": "mimo[1m]",
    "haiku_model": "mimo[1m]",
}

_SPARSE = {
    "name": "anyrouter",
    "base_url": "https://anyrouter.example",
    "auth_token": "tok-any",
    "api_key": None,
    "default_model": None,
    "opus_model": None,
    "sonnet_model": None,
    "haiku_model": None,
}


def test_build_env_overlay_includes_all_defined_provider_fields(tmp_path):
    pm = _seed(tmp_path, {"cpa": _FULL}, active="cpa")

    assert pm.build_env_overlay() == {
        "ANTHROPIC_BASE_URL": "https://cpa.example",
        "ANTHROPIC_AUTH_TOKEN": "tok-cpa",
        "ANTHROPIC_MODEL": "mimo[1m]",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "mimo[1m]",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "mimo[1m]",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "mimo[1m]",
    }


def test_build_env_overlay_omits_unset_fields_so_they_inherit(tmp_path):
    pm = _seed(tmp_path, {"anyrouter": _SPARSE}, active="anyrouter")

    overlay = pm.build_env_overlay()

    # Only provider identity is pinned; model keys absent -> inherit user settings.
    assert overlay == {
        "ANTHROPIC_BASE_URL": "https://anyrouter.example",
        "ANTHROPIC_AUTH_TOKEN": "tok-any",
    }
    assert "ANTHROPIC_MODEL" not in overlay
    assert "ANTHROPIC_DEFAULT_OPUS_MODEL" not in overlay


def test_build_env_overlay_includes_api_key_when_set(tmp_path):
    direct = {
        **_SPARSE,
        "name": "direct",
        "base_url": None,
        "auth_token": None,
        "api_key": "sk-direct",
    }
    pm = _seed(tmp_path, {"direct": direct}, active="direct")

    assert pm.build_env_overlay() == {"ANTHROPIC_API_KEY": "sk-direct"}


def test_build_env_overlay_reflects_default_model_set_via_set_default_model(tmp_path):
    """set_default_model pins ANTHROPIC_MODEL but leaves base_url on the profile."""
    pm = _seed(tmp_path, {"cpa": _FULL}, active="cpa")

    pm.set_default_model("claude-opus-4-8")
    overlay = pm.build_env_overlay()

    assert overlay["ANTHROPIC_MODEL"] == "claude-opus-4-8"
    assert overlay["ANTHROPIC_BASE_URL"] == "https://cpa.example"


def test_overlay_file_written_and_refreshed(tmp_path):
    """The overlay JSON exists after load and tracks provider/model changes."""
    pm = _seed(tmp_path, {"cpa": _FULL}, active="cpa")

    overlay_file = pm.settings_overlay_path()
    assert overlay_file.exists()  # written during _load()
    env = json.loads(overlay_file.read_text(encoding="utf-8"))["env"]
    assert env["ANTHROPIC_BASE_URL"] == "https://cpa.example"
    assert env["ANTHROPIC_MODEL"] == "mimo[1m]"

    pm.set_default_model("claude-opus-4-8")
    env = json.loads(overlay_file.read_text(encoding="utf-8"))["env"]
    assert env["ANTHROPIC_MODEL"] == "claude-opus-4-8"
    assert env["ANTHROPIC_BASE_URL"] == "https://cpa.example"


def test_set_default_model_clears_when_none(tmp_path):
    pm = _seed(tmp_path, {"cpa": _FULL}, active="cpa")
    pm.set_default_model(None)
    profile = pm.get_active()
    assert profile.default_model is None
    assert pm.get_effective_model() is None


def test_switch_profile_no_longer_clears_model_override(tmp_path):
    pm = _seed(
        tmp_path,
        {"a": {**_SPARSE, "name": "a", "default_model": "model-a[1m]"},
         "b": {**_SPARSE, "name": "b", "default_model": "model-b[200k]"}},
        active="a",
    )
    assert pm.get_effective_model() == "model-a[1m]"
    pm.switch_profile("b")
    assert pm.get_effective_model() == "model-b[200k]"
    pm.switch_profile("a")
    assert pm.get_effective_model() == "model-a[1m]"


def test_get_model_source_no_more_override(tmp_path):
    pm = _seed(tmp_path, {"cpa": _FULL}, active="cpa")
    assert pm.get_model_source() == "profile"


def test_save_no_longer_writes_model_override(tmp_path):
    pm = _seed(tmp_path, {"cpa": _FULL}, active="cpa")
    pm.set_default_model("some-model[1m]")
    raw = json.loads(pm._storage_path.read_text(encoding="utf-8"))
    assert "model_override" not in raw
    assert raw["profiles"]["cpa"]["default_model"] == "some-model[1m]"


def test_parse_models_response_dedup_and_sort():
    """_parse_models_response 去重并按名称排序。"""
    from src.config.providers import _parse_models_response

    raw = {
        "object": "list",
        "data": [
            {"id": "z-model", "object": "model"},
            {"id": "a-model", "object": "model"},
            {"id": "z-model", "object": "model"},
            {"id": "b-model", "object": "model"},
        ]
    }
    result = _parse_models_response(raw)
    assert result == ["a-model", "b-model", "z-model"]


def test_parse_models_response_empty():
    """空 data 或异常格式返回空列表。"""
    from src.config.providers import _parse_models_response

    assert _parse_models_response({}) == []
    assert _parse_models_response({"data": []}) == []
    assert _parse_models_response({"data": [{"no_id": "x"}]}) == []
