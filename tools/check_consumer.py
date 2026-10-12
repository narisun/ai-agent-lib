"""Build wheels and verify a consumer in a fresh Python/pip environment.

The library is installed from wheels, without repository PYTHONPATH. Generated
services use the normal editable workspace installation. Run
``python tools/check_consumer.py --profile generated-anthropic``. This gate
needs a package index (or a configured pip wheelhouse), never Docker.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import venv
from pathlib import Path

PROFILES = (
    "core",
    "aws",
    "cli",
    "ui",
    "ui-chat",
    "generated-fake",
    "generated-anthropic",
    "generated-bedrock",
)


def distribution_paths(wheels: Path, packages: list[str], version: str, *, chat: bool) -> list[str]:
    """Select this checkout's wheels and request the optional chat extra explicitly."""
    return [
        str(next(wheels.glob(f"ai_agent_lib_{package}-{version}-*.whl")))
        + ("[chat]" if package == "ui" and chat else "")
        for package in packages
    ]


def profile_packages(profile: str) -> list[str]:
    """Keep UI and agent/MCP consumer dependency sets separate."""
    packages = ["core"]
    if profile in {"aws", "generated-bedrock"}:
        packages.append("aws")
    if profile in {"ui", "ui-chat"}:
        packages.append("ui")
    if profile not in {"core", "aws", "ui", "ui-chat"}:
        packages.append("cli")
    return packages


def main() -> None:
    """Build this checkout, then test only its installed wheels and generated code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=PROFILES, required=True)
    parser.add_argument("--constraints", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    version = tomllib.loads((root / "packages/ai-agent-lib-core/pyproject.toml").read_text())[
        "project"
    ]["version"]
    parent = root / ".agentlib" / "consumers"
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=parent) as temporary:
        work = Path(temporary)
        wheels = work / "wheels"
        wheels.mkdir()
        subprocess.run(  # noqa: S603 - declared local projects, no shell
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--wheel-dir",
                str(wheels),
                *(
                    str(p)
                    for p in sorted((root / "packages").iterdir())
                    if (p / "pyproject.toml").is_file()
                ),
            ],
            check=True,
            cwd=work,
        )
        environment = work / "env"
        venv.EnvBuilder(with_pip=True).create(environment)
        python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env["PIP_FIND_LINKS"] = str(wheels)
        env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
        if args.constraints:
            env["PIP_CONSTRAINT"] = str(args.constraints.resolve())

        def run(*arguments: str, cwd: Path = work) -> None:
            subprocess.run(  # noqa: S603 - interpreter and explicit argv, no shell
                [str(python), "-I", *arguments],
                check=True,
                cwd=cwd,
                env=env,
            )

        packages = profile_packages(args.profile)
        # Direct wheel paths guarantee this checkout is tested even when the
        # package index also has a release bearing the same version number.
        distributions = distribution_paths(
            wheels, packages, version, chat=args.profile == "ui-chat"
        )
        run("-m", "pip", "install", *distributions)
        run("-c", "import ai_agent_lib_core as c; assert 'site-packages' in c.__file__")
        if args.profile == "core":
            run(
                "-c",
                "from ai_agent_lib_core.di import ServiceProviders; "
                "assert ServiceProviders.default().lookup('checkpoint', 'sqlite').options; "
                "import importlib.util; "
                "assert importlib.util.find_spec('ai_agent_lib_aws') is None",
            )
        elif args.profile == "ui":
            run(
                "-c",
                "import sys, importlib.util; "
                "import ai_agent_lib_ui as ui; "
                "from ai_agent_lib_ui.application.question_answer import QuestionAnswerAdapter; "
                "from ai_agent_lib_ui.contracts.ports import DiagnosticReader; "
                "from ai_agent_lib_core.contracts.diagnostics import ModelAttempt; "
                "assert 'site-packages' in ui.__file__; "
                "assert not any(n.split('.')[0] in {'langgraph','langchain_core','mcp','chainlit'} "
                "for n in sys.modules); "
                "assert all(importlib.util.find_spec(n) is None "
                "for n in ('ai_agent_lib_cli','ai_agent_lib_aws','chainlit','mcp'))",
            )
        elif args.profile == "ui-chat":
            run(
                "-c",
                "import ai_agent_lib_ui as ui; from importlib.resources import files; "
                "assert 'site-packages' in ui.__file__; "
                "assert files(ui).joinpath('assets/chainlit.toml').is_file(); "
                "assert files(ui).joinpath('migrations/001_local.sql').is_file()",
            )
            run("-m", "pip", "install", "pytest>=8.2", "pytest-asyncio>=0.24", "aiohttp>=3.10")
            suite = work / "test_ui_protocol.py"
            shutil.copyfile(root / "packages/ai-agent-lib-ui/tests/test_browser_protocol.py", suite)
            config = work / "pytest.ini"
            config.write_text(
                "[pytest]\nasyncio_mode=auto\nasyncio_default_fixture_loop_scope=function\n"
                "filterwarnings=error\n",
                encoding="utf-8",
            )
            run("-m", "pytest", "-q", "-c", str(config), str(suite))
        elif args.profile == "aws":
            run(
                "-c",
                "from ai_agent_lib_core.di import ServiceProviders; "
                "assert ServiceProviders.default().lookup('checkpoint', 'postgres').options; "
                "import importlib.util; assert importlib.util.find_spec('psycopg') is None",
            )
        else:
            run(
                "-m",
                "ai_agent_lib_cli",
                "init",
                "demo",
                "--owner",
                "consumer",
                "--dir",
                str(work),
                "--lib-version",
                "==" + version,
            )
            workspace = work / "demo"
            if args.profile.startswith("generated-"):
                provider = args.profile.removeprefix("generated-")
                run(
                    "-m",
                    "ai_agent_lib_cli",
                    "new",
                    "agent",
                    "helper",
                    "--description",
                    "Consumer agent.",
                    "--model",
                    provider,
                    "--workspace",
                    str(workspace),
                )
                run(
                    "-m",
                    "ai_agent_lib_cli",
                    "new",
                    "mcp",
                    "people",
                    "--description",
                    "Consumer tools.",
                    "--no-pin",
                    "--workspace",
                    str(workspace),
                )
                run("-m", "ai_agent_lib_cli", "install", "--workspace", str(workspace))
                run(
                    "-c",
                    "import helper.graph, helper.service, people.server; "
                    "from ai_agent_lib_core.di import ServiceProviders; "
                    f"assert ServiceProviders.default().lookup('model', {provider!r})",
                )
                if provider == "anthropic":
                    run(
                        "-c",
                        "from ai_agent_lib_core.adapters import AnthropicChatModelProvider; "
                        "from pydantic import SecretStr; "
                        "AnthropicChatModelProvider(SecretStr('test')).create('test-model')",
                    )
                elif provider == "bedrock":
                    run(
                        "-c",
                        "from ai_agent_lib_aws.model_bedrock import BedrockChatModelProvider; "
                        "from ai_agent_lib_aws.testing import offline_sessions; "
                        "BedrockChatModelProvider(offline_sessions()).create('anthropic.claude-v2')",
                    )
                run("-m", "pytest", "-q", cwd=workspace)
                # Run the installed provider through native framework invocation,
                # tools and structured parsing at both dependency-version bounds.
                env["AGENTLIB_CONSUMER_MODEL"] = provider
                suite = work / "test_installed_model.py"
                shutil.copyfile(root / "tools/consumer_model_contract.py", suite)
                run("-m", "pytest", "-q", "--asyncio-mode=auto", str(suite))
        run("-m", "pip", "check")


if __name__ == "__main__":
    main()
