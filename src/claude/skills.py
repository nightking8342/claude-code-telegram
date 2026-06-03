"""Discover installed Claude Code skills from the filesystem."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import structlog

logger = structlog.get_logger(__name__)

# Default setting_sources — must match sdk_integration.py
DEFAULT_SETTING_SOURCES = ["user", "project"]


@dataclass(frozen=True)
class SkillInfo:
    """A discovered skill with its metadata."""

    name: str  # Fully qualified: "plugin:skill" or "skill-name"
    description: str
    source: str  # "plugin:<name>", "user", or "project"


def discover_skills(
    claude_home: Optional[Path] = None,
    working_directory: Optional[Path] = None,
    setting_sources: Optional[List[str]] = None,
) -> List[SkillInfo]:
    """Scan the filesystem and return available skills.

    Scans locations based on ``setting_sources`` (must match the sources
    passed to ``ClaudeAgentOptions`` in ``sdk_integration.py``):

    - ``"user"`` → ``~/.claude/skills/*/SKILL.md``
    - ``"project"`` → ``<workdir>/../<dir>/.claude/skills/*/SKILL.md``
      (walks from working_directory up to nearest git root)
    - Plugins are always scanned (loaded via ``installed_plugins.json``)

    Args:
        claude_home: Path to ~/.claude directory.
        working_directory: Current working directory for project skill discovery.
        setting_sources: List of setting sources to include. Defaults to
            ``["user", "project"]`` to match ``sdk_integration.py``.
    """
    if claude_home is None:
        claude_home = Path.home() / ".claude"
    if setting_sources is None:
        setting_sources = DEFAULT_SETTING_SOURCES

    skills: List[SkillInfo] = []

    if "user" in setting_sources:
        skills.extend(_scan_user_skills(claude_home / "skills"))

    if "project" in setting_sources and working_directory is not None:
        skills.extend(_scan_project_skills(working_directory))

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


def _scan_project_skills(working_directory: Path) -> List[SkillInfo]:
    """Scan project-level ``.claude/skills/*/SKILL.md`` and
    ``.claude/commands/*.md`` (legacy).

    Walks from ``working_directory`` up to the nearest ``.git`` root
    (or filesystem root if not in a git repo), matching CLI behaviour
    in ``getProjectDirsUpToHome()``.

    Results are ordered from most specific (CWD) to least specific (root),
    so deeper skills take precedence when names collide.
    """
    results: List[SkillInfo] = []
    current = working_directory.resolve()
    home = Path.home().resolve()

    while True:
        # Stop at home dir (user-level skills handled separately)
        if current == home:
            break

        # Skills (directory format)
        skills_dir = current / ".claude" / "skills"
        if skills_dir.is_dir():
            for skill_md in skills_dir.glob("*/SKILL.md"):
                name, description = _parse_frontmatter(skill_md)
                results.append(
                    SkillInfo(name=name, description=description, source="project")
                )

        # Legacy commands (single .md file format)
        commands_dir = current / ".claude" / "commands"
        if commands_dir.is_dir():
            for cmd_md in commands_dir.glob("*.md"):
                name, description = _parse_frontmatter(cmd_md)
                results.append(
                    SkillInfo(name=name, description=description, source="project")
                )

        # Stop at git root
        if (current / ".git").exists():
            break

        parent = current.parent
        if parent == current:
            break  # reached filesystem root
        current = parent

    return results


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
