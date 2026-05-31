"""Tests for ProviderManager -> Claude `--settings` env overlay.

The overlay pins the active profile's provider/model env vars at the highest
("flag") settings layer so they override `~/.claude/settings.json` while
`setting_sources=["user","project"]` still inherits skills/memory/plugins.
"""

import json
from pathlib import Path
from types import SimpleNamespace

from src.config.providers import ProviderManager


def _seed(tmp_path: Path, profiles: dict, active: str, model_override=None):
    """Construct a ProviderManager from a pre-written providers.json."""
    storage = tmp_path / "providers.json"
    storage.write_text(
        json.dumps(
            {"active": active, "model_override": model_override, "profiles": profiles}
        ),
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


def test_build_env_overlay_reflects_model_override(tmp_path):
    """A /model override pins ANTHROPIC_MODEL but leaves base_url on the profile.

    This is the core fix: the chosen model is sent to the profile's provider,
    not whatever provider the CLI's user settings point at.
    """
    pm = _seed(tmp_path, {"cpa": _FULL}, active="cpa")

    pm.set_model_override("claude-opus-4-8")
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

    pm.set_model_override("claude-opus-4-8")
    env = json.loads(overlay_file.read_text(encoding="utf-8"))["env"]
    assert env["ANTHROPIC_MODEL"] == "claude-opus-4-8"
    assert env["ANTHROPIC_BASE_URL"] == "https://cpa.example"
