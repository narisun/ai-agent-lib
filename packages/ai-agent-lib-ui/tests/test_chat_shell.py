"""Environment translation and shell preparation for the installed Chainlit app."""

import os
import subprocess
import tomllib
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from python_runtime import python_command

from ai_agent_lib_ui.config import shell
from ai_agent_lib_ui.config.settings import LocalIdentity, Settings, agent_environment


def test_env_translation_is_explicit_and_does_not_export_model_keys(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        'AC2_REGISTRY_OPTIONS={"agents_path":"../agents.yaml","tools_path":"tools.yaml"}\n'
        'AC2_IDENTITY_OPTIONS={"subject":"ada","tenant":"bank","roles":["analyst"]}\n'
        "ANTHROPIC_API_KEY=not-a-real-key\n",
        encoding="utf-8",
    )
    agents, tools, identity = agent_environment(env)
    assert agents == (tmp_path.parent / "agents.yaml").resolve()
    assert tools == tmp_path / "tools.yaml"
    assert identity.subject == "ada"
    env.write_text("AC2_IDENTITY_PROVIDER=jwt\n")
    with pytest.raises(ValueError, match="static"):
        agent_environment(env)
    env.write_text('AC2_IDENTITY_OPTIONS={"audience":"memo","token_ttl_seconds":600}\n')
    _, _, defaults = agent_environment(env)
    assert defaults == LocalIdentity(subject="dev-user", tenant="dev-tenant", roles=())
    env.write_text("AC2_PROFILE=aws\n")
    with pytest.raises(ValueError, match="local"):
        agent_environment(env)


@pytest.mark.slow
def test_shell_ignores_working_directory_dotenv(settings: Settings, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("UI_IGNORED_SENTINEL=must-not-be-loaded\n")
    config = tmp_path / "config.json"
    config.write_text(settings.model_dump_json())
    script = (
        "import os; from pathlib import Path; "
        "from ai_agent_lib_ui.config.settings import Settings; "
        "from ai_agent_lib_ui.config.shell import prepare_shell; "
        "prepare_shell(Path('state'), Settings.read(Path('config.json'))); "
        "import chainlit; assert 'UI_IGNORED_SENTINEL' not in os.environ"
    )
    result = subprocess.run(  # noqa: S603 - fixed import probe in a temporary test folder
        python_command("-c", script),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={key: value for key, value in os.environ.items() if key != "LITERAL_API_KEY"},
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_shell_versions_assets_by_content_without_changing_saved_secrets(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A package copy lets the test simulate an upgrade across two local starts.
    package = tmp_path / "package"
    assets = package / "assets"
    assets.mkdir(parents=True)
    for name in ("chainlit.toml", "chat.css", "conversations.svg", "memos.js", "agent-lab.svg"):
        (assets / name).write_bytes(files("ai_agent_lib_ui").joinpath("assets", name).read_bytes())
    monkeypatch.setattr(shell, "files", lambda _: package)
    monkeypatch.setattr("ai_agent_lib_ui.config.shell.os.environ", {})
    state = tmp_path / "state"

    def prepare() -> tuple[tuple[str, str], dict[str, object]]:
        secrets = shell.prepare_shell(state, settings)
        config = tomllib.loads((state / "shell/.chainlit/config.toml").read_text())
        return secrets, config["UI"]

    first_secrets, first = prepare()
    second_secrets, second = prepare()
    assert second == first
    assert second_secrets == first_secrets
    assert urlsplit(str(first["custom_css"])).path == "/public/chat.css"
    assert urlsplit(str(first["custom_css"])).query.startswith("v=")
    assert urlsplit(str(first["custom_js"])).query.startswith("v=")
    css = (assets / "chat.css").read_bytes() + b"\n/* changed style */\n"
    (assets / "chat.css").write_bytes(css)
    third_secrets, third = prepare()
    assert third_secrets == first_secrets
    assert third["custom_css"] != first["custom_css"]
    assert third["custom_js"] == first["custom_js"]
    assert (state / "shell/public/chat.css").read_bytes() == css
