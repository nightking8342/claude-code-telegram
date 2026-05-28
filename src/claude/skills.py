"""Discover installed Claude Code skills from the filesystem."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import structlog

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class SkillInfo:
    """A discovered skill with its metadata."""

    name: str  # Fully qualified: "plugin:skill" or "skill-name"
    description: str
    source: str  # "plugin:<name>" or "user"


def discover_skills(claude_home: Optional[Path] = None) -> List[SkillInfo]:
    """Scan the filesystem and return all available skills.

    Scans two locations:
    - User-level: ``~/.claude/skills/*/SKILL.md``
    - Plugin-level: each installed plugin's ``skills/*/SKILL.md``
    """
    if claude_home is None:
        claude_home = Path.home() / ".claude"

    skills: List[SkillInfo] = []
    skills.extend(_scan_user_skills(claude_home / "skills"))
    skills.extend(_scan_plugin_skills(claude_home / "plugins"))
    return skills


def _parse_frontmatter(path: Path) -> tuple[str, str]:
    """Extract name and description from SKILL.md YAML frontmatter.

    Returns (name, description). Falls back to directory name / empty string.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return path.parent.name, ""

    if not text.startswith("---"):
        return path.parent.name, ""

    # Find closing ---
    end = text.find("---", 3)
    if end == -1:
        return path.parent.name, ""

    frontmatter = text[3:end]
    name = path.parent.name
    description = ""

    for line in frontmatter.splitlines():
        line = line.strip()
        if line.startswith("name:"):
            name = line[5:].strip().strip("\"'")
        elif line.startswith("description:"):
            desc = line[12:].strip().strip("\"'")
            # Truncate long descriptions
            if len(desc) > 120:
                desc = desc[:117] + "..."
            description = desc

    return name, description


def _scan_user_skills(skills_dir: Path) -> List[SkillInfo]:
    """Scan ~/.claude/skills/*/SKILL.md for user-level skills."""
    if not skills_dir.is_dir():
        return []

    results: List[SkillInfo] = []
    for skill_md in skills_dir.glob("*/SKILL.md"):
        name, description = _parse_frontmatter(skill_md)
        results.append(SkillInfo(name=name, description=description, source="user"))

    return results


def _scan_plugin_skills(plugins_dir: Path) -> List[SkillInfo]:
    """Scan installed plugins for skills.

    Reads installed_plugins.json, checks enabledPlugins, then scans each
    plugin's skills/ directory.
    """
    installed_path = plugins_dir / "installed_plugins.json"
    if not installed_path.is_file():
        return []

    try:
        data = json.loads(installed_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    # Read enabled plugins from settings
    enabled = _load_enabled_plugins(plugins_dir.parent)

    results: List[SkillInfo] = []
    plugins = data.get("plugins", {})

    for plugin_key, entries in plugins.items():
        # plugin_key like "superpowers@claude-plugins-official"
        # Check if this plugin is enabled
        if plugin_key in enabled and not enabled[plugin_key]:
            continue  # disabled

        # Extract plugin name (before @)
        plugin_name = plugin_key.split("@")[0] if "@" in plugin_key else plugin_key

        for entry in entries:
            install_path = Path(entry.get("installPath", ""))
            if not install_path.is_dir():
                continue

            for skill_md in install_path.glob("skills/*/SKILL.md"):
                name, description = _parse_frontmatter(skill_md)
                qualified = f"{plugin_name}:{name}"
                results.append(
                    SkillInfo(
                        name=qualified,
                        description=description,
                        source=f"plugin:{plugin_name}",
                    )
                )

    return results


def _load_enabled_plugins(settings_path: Path) -> dict[str, bool]:
    """Load enabledPlugins map from ~/.claude/settings.json."""
    settings_file = settings_path / "settings.json"
    if not settings_file.is_file():
        return {}

    try:
        data = json.loads(settings_file.read_text(encoding="utf-8"))
        return data.get("enabledPlugins", {})
    except (OSError, json.JSONDecodeError):
        return {}
