"""Runtime provider profile management for switching API endpoints and models."""

import asyncio
import aiohttp
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import structlog

logger = structlog.get_logger(__name__)

_ENV_KEYS = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL",
)

# Context window suffixes: [1m] = 1,000,000 tokens, [200k] = 200,000, etc.
_CONTEXT_SUFFIX_RE = re.compile(r"\[(\d+)(k|m)\]$", re.IGNORECASE)
_DEFAULT_CONTEXT_WINDOW = 200_000
_MODELS_TIMEOUT = aiohttp.ClientTimeout(total=10)

# Role shorthand aliases
_ROLE_ALIASES = {"o": "opus", "s": "sonnet", "h": "haiku"}
_VALID_ROLES = ("opus", "sonnet", "haiku")
_ROLE_ENV_MAP = {
    "opus": "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "sonnet": "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "haiku": "ANTHROPIC_DEFAULT_HAIKU_MODEL",
}


def _parse_context_suffix(model: str) -> Tuple[str, int]:
    """Strip context-window suffix and return (clean_name, window_size).

    Examples:
        "claude-sonnet-4[1m]"     -> ("claude-sonnet-4", 1_000_000)
        "claude-opus-4[200k]"     -> ("claude-opus-4", 200_000)
        "claude-sonnet-4"         -> ("claude-sonnet-4", 200_000)
    """
    m = _CONTEXT_SUFFIX_RE.search(model)
    if not m:
        return model, _DEFAULT_CONTEXT_WINDOW
    num = int(m.group(1))
    unit = m.group(2).lower()
    multiplier = 1_000_000 if unit == "m" else 1_000
    return model[: m.start()], num * multiplier


def _parse_models_response(data: dict) -> list:
    """Extract, deduplicate, and sort model IDs from an OpenAI-compatible response."""
    models = data.get("data", [])
    ids = sorted(
        {m["id"] for m in models if isinstance(m, dict) and "id" in m}
    )
    return ids


@dataclass
class ProviderProfile:
    name: str
    base_url: Optional[str] = None
    auth_token: Optional[str] = None
    api_key: Optional[str] = None
    default_model: Optional[str] = None
    description: Optional[str] = None
    opus_model: Optional[str] = None
    sonnet_model: Optional[str] = None
    haiku_model: Optional[str] = None


class ProviderManager:
    """Manages provider profiles and active selection at runtime.

    Profiles are persisted in a JSON file. The active profile determines which
    environment variables (ANTHROPIC_BASE_URL, ANTHROPIC_AUTH_TOKEN, etc.) are
    injected before each Claude SDK subprocess call.
    """

    def __init__(
        self,
        config,
        storage_path: Path = Path("data/providers.json"),
    ):
        self._config = config
        self._storage_path = storage_path
        self._profiles: Dict[str, ProviderProfile] = {}
        self._active_name: Optional[str] = None
        self._load()

    # ── Persistence ──────────────────────────────────────────────

    def _load(self) -> None:
        if self._storage_path.exists():
            try:
                data = json.loads(self._storage_path.read_text(encoding="utf-8"))
                self._active_name = data.get("active")
                for name, pdata in data.get("profiles", {}).items():
                    self._profiles[name] = ProviderProfile(**pdata)
                logger.info(
                    "Loaded provider profiles",
                    count=len(self._profiles),
                    active=self._active_name,
                )
                self._write_overlay()
                return
            except (json.JSONDecodeError, TypeError) as exc:
                logger.warning(
                    "Failed to parse providers.json, recreating", error=str(exc)
                )

        self._auto_create_default()

    def _save(self) -> None:
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "active": self._active_name,
            "profiles": {n: asdict(p) for n, p in self._profiles.items()},
        }
        self._storage_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        self._write_overlay()
        logger.debug("Saved provider profiles", path=str(self._storage_path))

    def _auto_create_default(self) -> None:
        """Create initial default profile from current .env values."""
        profile = ProviderProfile(
            name="default",
            base_url=os.environ.get("ANTHROPIC_BASE_URL"),
            auth_token=os.environ.get("ANTHROPIC_AUTH_TOKEN"),
            api_key=getattr(self._config, "anthropic_api_key_str", None),
            default_model=getattr(self._config, "claude_model", None),
            description="Auto-created from .env",
        )
        self._profiles["default"] = profile
        self._active_name = "default"
        self._save()
        logger.info("Auto-created default provider profile from .env")

    # ── Profile CRUD ─────────────────────────────────────────────

    def list_profiles(self) -> List[ProviderProfile]:
        return list(self._profiles.values())

    def get_profile(self, name: str) -> Optional[ProviderProfile]:
        return self._profiles.get(name)

    # ── Active selection ─────────────────────────────────────────

    def switch_profile(self, name: str) -> ProviderProfile:
        if name not in self._profiles:
            raise KeyError(
                f"Provider '{name}' not found. Available: {', '.join(self._profiles)}"
            )
        self._active_name = name
        self._save()
        logger.info("Switched provider", provider=name)
        return self._profiles[name]

    def get_active(self) -> Optional[ProviderProfile]:
        if self._active_name:
            return self._profiles.get(self._active_name)
        return None

    def get_active_name(self) -> Optional[str]:
        return self._active_name

    # ── Default model ────────────────────────────────────────────

    def set_default_model(self, model: Optional[str]) -> None:
        """Set the default model on the active profile (writes to profile.default_model)."""
        active = self.get_active()
        if not active:
            raise RuntimeError("No active provider profile")
        active.default_model = model
        self._save()
        if model:
            logger.info("Default model set", model=model)
        else:
            logger.info("Default model cleared")

    async def fetch_models(self) -> list:
        """Fetch available models from the active provider's /v1/models endpoint.

        Returns a sorted, deduplicated list of model IDs. Returns an empty list
        on failure (network error, timeout, or unexpected response format).
        """
        active = self.get_active()
        if not active or not active.base_url:
            logger.warning("fetch_models: no active profile or base_url")
            return []

        url = active.base_url.rstrip("/") + "/v1/models"
        headers = {}
        if active.api_key:
            headers["Authorization"] = f"Bearer {active.api_key}"
        elif active.auth_token:
            headers["Authorization"] = f"Bearer {active.auth_token}"

        try:
            async with aiohttp.ClientSession(timeout=_MODELS_TIMEOUT) as session:
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        models = _parse_models_response(data)
                        logger.info(
                            "fetch_models succeeded",
                            url=url,
                            count=len(models),
                        )
                        return models
                    else:
                        logger.warning(
                            "fetch_models: non-200 response",
                            url=url,
                            status=resp.status,
                        )
                        return []
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("fetch_models failed", url=url, error=str(exc))
            return []

    def get_effective_model(self) -> Optional[str]:
        """Return the effective model name (may include [1m] suffix)."""
        active = self.get_active()
        if active and active.default_model:
            return active.default_model
        return getattr(self._config, "claude_model", None)

    def get_clean_model(self) -> Optional[str]:
        """Return the effective model name with context suffix stripped."""
        raw = self.get_effective_model()
        if raw is None:
            return None
        clean, _ = _parse_context_suffix(raw)
        return clean

    def get_context_window(self) -> int:
        """Return the context window size for the effective model."""
        raw = self.get_effective_model()
        if raw is None:
            return _DEFAULT_CONTEXT_WINDOW
        _, window = _parse_context_suffix(raw)
        return window

    def get_model_source(self) -> str:
        """Return where the effective model comes from."""
        active = self.get_active()
        if active and active.default_model:
            return "profile"
        return "settings"

    # ── Per-role model overrides ────────────────────────────────

    @staticmethod
    def resolve_role(name: str) -> Optional[str]:
        """Resolve role alias to canonical name. Returns None if invalid."""
        if name in _VALID_ROLES:
            return name
        return _ROLE_ALIASES.get(name.lower())

    def set_role_model(self, role: str, model: Optional[str]) -> None:
        """Set (or clear) the model for a role on the active profile."""
        active = self.get_active()
        if not active:
            raise RuntimeError("No active provider profile")
        setattr(active, f"{role}_model", model)
        self._save()
        if model:
            logger.info("Role model set", role=role, model=model)
        else:
            logger.info("Role model cleared", role=role)

    def get_role_models(self) -> Dict[str, Optional[str]]:
        """Return {opus, sonnet, haiku} model mapping for the active profile."""
        active = self.get_active()
        if not active:
            return {r: None for r in _VALID_ROLES}
        return {
            "opus": active.opus_model,
            "sonnet": active.sonnet_model,
            "haiku": active.haiku_model,
        }

    # ── Environment injection ────────────────────────────────────

    def apply_to_environ(self) -> Dict[str, Optional[str]]:
        """Temporarily set os.environ for the active profile.

        Returns previous values for later restoration via restore_environ().
        """
        saved: Dict[str, Optional[str]] = {}
        active = self.get_active()
        if not active:
            return saved

        mapping = {
            "ANTHROPIC_BASE_URL": active.base_url,
            "ANTHROPIC_AUTH_TOKEN": active.auth_token,
            "ANTHROPIC_API_KEY": active.api_key,
            "ANTHROPIC_MODEL": self.get_effective_model(),
        }
        # Inject per-role model env vars
        for role, env_key in _ROLE_ENV_MAP.items():
            role_model = getattr(active, f"{role}_model", None)
            if role_model:
                mapping[env_key] = role_model
        for key, value in mapping.items():
            saved[key] = os.environ.get(key)
            if value is not None:
                os.environ[key] = value
            elif key in os.environ and value is None and saved[key] is not None:
                # Only delete if the profile explicitly has None and env had a value
                pass  # keep existing env var to avoid breaking default config

        return saved

    def restore_environ(self, saved: Dict[str, Optional[str]]) -> None:
        """Restore os.environ to previous state."""
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # ── Settings overlay (highest-priority "flag" layer) ─────────

    def settings_overlay_path(self) -> Path:
        """Path to the generated Claude ``--settings`` overlay file.

        Passed to ``ClaudeAgentOptions.settings`` so the active profile's
        provider/model env vars sit in the highest-priority settings layer,
        overriding ``~/.claude/settings.json`` while ``setting_sources`` still
        inherits skills/memory/plugins from user settings.
        """
        return self._storage_path.parent / "provider-cli-settings.json"

    def build_env_overlay(self) -> Dict[str, str]:
        """Build the ``env`` block pinning the active profile's provider/model.

        Only fields the active profile defines are included; unset (None)
        fields are omitted so they keep inheriting from user settings.
        """
        active = self.get_active()
        if not active:
            return {}
        env: Dict[str, str] = {}
        if active.base_url:
            env["ANTHROPIC_BASE_URL"] = active.base_url
        if active.auth_token:
            env["ANTHROPIC_AUTH_TOKEN"] = active.auth_token
        if active.api_key:
            env["ANTHROPIC_API_KEY"] = active.api_key
        model = self.get_effective_model()
        if model:
            env["ANTHROPIC_MODEL"] = model
        for role, env_key in _ROLE_ENV_MAP.items():
            role_model = getattr(active, f"{role}_model", None)
            if role_model:
                env[env_key] = role_model
        return env

    def _write_overlay(self) -> None:
        """Persist (or remove) the ``--settings`` overlay JSON file."""
        overlay_path = self.settings_overlay_path()
        env = self.build_env_overlay()
        try:
            if env:
                overlay_path.parent.mkdir(parents=True, exist_ok=True)
                overlay_path.write_text(
                    json.dumps({"env": env}, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            else:
                overlay_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Failed to write provider settings overlay", error=str(exc))
