"""Tests for model panel: fetch_models and data model."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.config.providers import ProviderManager, _parse_models_response


def _seed(tmp_path: Path, profiles: dict, active: str):
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
    "api_key": "sk-test",
    "default_model": "mimo[1m]",
    "opus_model": "mimo[1m]",
    "sonnet_model": "mimo[1m]",
    "haiku_model": "mimo[1m]",
}


class TestFetchModels:
    async def test_fetch_models_returns_sorted_deduped_ids(self, tmp_path):
        pm = _seed(tmp_path, {"cpa": _FULL}, active="cpa")

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_resp.json = AsyncMock(return_value={
            "data": [
                {"id": "z-model"}, {"id": "a-model"}, {"id": "z-model"}
            ]
        })

        with patch("aiohttp.ClientSession.get", return_value=mock_resp):
            result = await pm.fetch_models()

        assert result == ["a-model", "z-model"]

    async def test_fetch_models_empty_on_500(self, tmp_path):
        pm = _seed(tmp_path, {"cpa": _FULL}, active="cpa")

        mock_resp = MagicMock()
        mock_resp.status = 500
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession.get", return_value=mock_resp):
            result = await pm.fetch_models()

        assert result == []

    async def test_fetch_models_empty_when_no_base_url(self, tmp_path):
        sparse = {**_FULL, "base_url": None, "name": "direct"}
        pm = _seed(tmp_path, {"direct": sparse}, active="direct")
        result = await pm.fetch_models()
        assert result == []


class TestSetDefaultModel:
    def test_set_default_model_writes_to_active_profile(self, tmp_path):
        pm = _seed(tmp_path, {"cpa": _FULL}, active="cpa")
        pm.set_default_model("new-model[1m]")

        raw = json.loads(pm._storage_path.read_text(encoding="utf-8"))
        assert "model_override" not in raw
        assert raw["profiles"]["cpa"]["default_model"] == "new-model[1m]"

    def test_set_default_model_none_clears(self, tmp_path):
        pm = _seed(tmp_path, {"cpa": _FULL}, active="cpa")
        pm.set_default_model(None)

        assert pm.get_active().default_model is None

    def test_get_effective_model_falls_back_to_config(self, tmp_path):
        sparse = {**_FULL, "default_model": None}
        config = SimpleNamespace(claude_model="from-config[1m]", anthropic_api_key_str=None)
        pm = _seed(tmp_path, {"cpa": sparse}, active="cpa")
        pm._config = config

        assert pm.get_effective_model() == "from-config[1m]"

    def test_get_model_source_no_override(self, tmp_path):
        pm = _seed(tmp_path, {"cpa": _FULL}, active="cpa")
        assert pm.get_model_source() == "profile"

        sparse = {**_FULL, "default_model": None}
        pm2 = _seed(tmp_path, {"cpa": sparse}, active="cpa")
        assert pm2.get_model_source() == "settings"
