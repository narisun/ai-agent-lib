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

PROFILES = ("core", "aws", "cli", "generated-fake", "generated-anthropic", "generated-bedrock")


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

        packages = ["core"]
        if args.profile in {"aws", "generated-bedrock"}:
            packages.append("aws")
        if args.profile not in {"core", "aws"}:
            packages.append("cli")
        # Direct wheel paths guarantee this checkout is tested even when the
        # package index also has a release bearing the same version number.
        distributions = [
            str(next(wheels.glob(f"ai_agent_lib_{package}-{version}-*.whl")))
            for package in packages
        ]
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
