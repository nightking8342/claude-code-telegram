"""Tests for src/claude/skills.py skill discovery logic."""

from pathlib import Path

import pytest

from src.claude.skills import SkillInfo, discover_skills


@pytest.fixture
def claude_home(tmp_path: Path) -> Path:
    """Create a fake ~/.claude with user skills and plugins."""
    home = tmp_path / "claude"
    home.mkdir()

    # User skill
    user_skill = home / "skills" / "my-skill" / "SKILL.md"
    user_skill.parent.mkdir(parents=True)
    user_skill.write_text(
        "---\nname: my-skill\ndescription: A user skill\n---\nBody\n",
        encoding="utf-8",
    )

    # Plugin (installed_plugins.json)
    plugins_dir = home / "plugins"
    plugins_dir.mkdir()
    plugin_skill_dir = plugins_dir / "cache" / "test-plugin" / "skills" / "plug-skill"
    plugin_skill_dir.mkdir(parents=True)
    (plugin_skill_dir / "SKILL.md").write_text(
        "---\nname: plug-skill\ndescription: A plugin skill\n---\nBody\n",
        encoding="utf-8",
    )
    installed = {
        "plugins": {
            "test-plugin@market": [
                {"installPath": str(plugins_dir / "cache" / "test-plugin")}
            ]
        }
    }
    import json

    (plugins_dir / "installed_plugins.json").write_text(
        json.dumps(installed), encoding="utf-8"
    )

    return home


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """Create a fake project with .claude/skills/ and a git root."""
    project = tmp_path / "myproject"
    project.mkdir()
    (project / ".git").mkdir()

    skill_dir = project / ".claude" / "skills" / "proj-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: proj-skill\ndescription: A project skill\n---\nBody\n",
        encoding="utf-8",
    )

    return project


class TestDiscoverSkills:
    def test_user_and_project_by_default(
        self, claude_home: Path, project_dir: Path
    ) -> None:
        """Default setting_sources=["user","project"] discovers both."""
        skills = discover_skills(
            claude_home=claude_home,
            working_directory=project_dir,
            setting_sources=["user", "project"],
        )
        names = [s.name for s in skills]
        assert "my-skill" in names
        assert "proj-skill" in names

    def test_project_only_excludes_user(
        self, claude_home: Path, project_dir: Path
    ) -> None:
        """setting_sources=["project"] excludes user-level skills."""
        skills = discover_skills(
            claude_home=claude_home,
            working_directory=project_dir,
            setting_sources=["project"],
        )
        names = [s.name for s in skills]
        assert "proj-skill" in names
        assert "my-skill" not in names

    def test_user_only_excludes_project(
        self, claude_home: Path, project_dir: Path
    ) -> None:
        """setting_sources=["user"] excludes project-level skills."""
        skills = discover_skills(
            claude_home=claude_home,
            working_directory=project_dir,
            setting_sources=["user"],
        )
        names = [s.name for s in skills]
        assert "my-skill" in names
        assert "proj-skill" not in names

    def test_empty_sources_still_finds_plugins(
        self, claude_home: Path, project_dir: Path
    ) -> None:
        """setting_sources=[] still discovers plugins."""
        skills = discover_skills(
            claude_home=claude_home,
            working_directory=project_dir,
            setting_sources=[],
        )
        names = [s.name for s in skills]
        assert "test-plugin:plug-skill" in names
        assert "my-skill" not in names
        assert "proj-skill" not in names

    def test_no_working_directory_skips_project(
        self, claude_home: Path
    ) -> None:
        """Without working_directory, project skills are skipped."""
        skills = discover_skills(
            claude_home=claude_home,
            working_directory=None,
            setting_sources=["user", "project"],
        )
        names = [s.name for s in skills]
        assert "my-skill" in names
        # No project dir provided, so no proj-skill

    def test_walks_up_to_git_root(
        self, claude_home: Path, tmp_path: Path
    ) -> None:
        """Project skill discovery walks from subdir up to .git root."""
        project = tmp_path / "walktest"
        project.mkdir()
        (project / ".git").mkdir()

        skill_dir = project / ".claude" / "skills" / "root-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: root-skill\ndescription: Found at root\n---\n",
            encoding="utf-8",
        )

        nested = project / "src" / "deep"
        nested.mkdir(parents=True)

        skills = discover_skills(
            claude_home=claude_home,
            working_directory=nested,
            setting_sources=["project"],
        )
        names = [s.name for s in skills]
        assert "root-skill" in names

    def test_legacy_commands_discovered(
        self, claude_home: Path, project_dir: Path
    ) -> None:
        """Legacy .claude/commands/*.md files are also discovered."""
        cmd_dir = project_dir / ".claude" / "commands"
        cmd_dir.mkdir(parents=True, exist_ok=True)
        (cmd_dir / "my-cmd.md").write_text(
            "---\nname: my-cmd\ndescription: A legacy command\n---\n",
            encoding="utf-8",
        )

        skills = discover_skills(
            claude_home=claude_home,
            working_directory=project_dir,
            setting_sources=["project"],
        )
        names = [s.name for s in skills]
        assert "my-cmd" in names
