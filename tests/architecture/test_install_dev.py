"""The pip setup includes nested CLI example services, not their workspace root."""

from pathlib import Path

from tools.install_dev import editable_requirements


def test_installable_project_keeps_all_extras(tmp_path: Path) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text(
        '[project]\nname = "example"\n[project.optional-dependencies]\nz = []\na = []\n',
        encoding="utf-8",
    )
    assert editable_requirements(project) == [f"{tmp_path}[a,z]"]


def test_ui_is_installed_only_in_its_own_environment() -> None:
    root = Path(__file__).resolve().parents[2]
    assert editable_requirements(root / "packages/agentcraft2-ui/pyproject.toml") == []


def test_workspace_expands_declared_agent_and_mcp_members(tmp_path: Path) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text(
        "[tool.uv]\npackage = false\n[tool.uv.workspace]\n"
        'members = ["agents/*", "mcp-servers/*"]\n',
        encoding="utf-8",
    )
    members = [tmp_path / "agents/agent", tmp_path / "mcp-servers/server"]
    for member in members:
        member.mkdir(parents=True)
        (member / "pyproject.toml").write_text('[project]\nname = "service"\n', encoding="utf-8")
    assert editable_requirements(project) == [str(member) for member in members]
