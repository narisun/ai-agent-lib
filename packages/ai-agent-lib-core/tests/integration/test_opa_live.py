"""Opt-in tests against a real OPA server running the repository's Rego bundle.

They need the ``opa`` binary on ``PATH``::

    uv run pytest -m integration packages/ai-agent-lib-core/tests/integration/test_opa_live.py
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator, Mapping
from pathlib import Path

import httpx
import pytest
from policy_corpus import documents, load_corpus

from ai_agent_lib_core.adapters import OpaPolicyDecisionPoint, OpaPolicyOptions
from ai_agent_lib_core.contracts import PolicyDecisionPoint
from ai_agent_lib_core.testing import SequentialIds
from ai_agent_lib_core.testing.contracts import PolicyDecisionPointContract

pytestmark = [pytest.mark.integration, pytest.mark.enable_socket]

BUNDLE = Path(__file__).parents[4] / "policies" / "bundle"


def opa_binary() -> str:
    binary = shutil.which("opa")
    if binary is None:
        pytest.skip("the opa binary is not on PATH")
    return binary


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class OpaServer:
    """One local OPA server per rules document."""

    def __init__(self) -> None:
        self._processes: list[subprocess.Popen[bytes]] = []

    def start(self, directory: Path, document: Mapping[str, object]) -> str:
        rules = directory / f"rules-{len(self._processes)}"
        (rules / "agentlib" / "rules").mkdir(parents=True)
        (rules / ".manifest").write_text(
            json.dumps({"revision": "test-rules-1", "roots": ["agentlib/rules"]}), encoding="utf-8"
        )
        (rules / "agentlib" / "rules" / "data.json").write_text(
            json.dumps(document), encoding="utf-8"
        )
        port = free_port()
        self._processes.append(
            subprocess.Popen(  # noqa: S603 - a fixed command line, no shell
                [
                    opa_binary(),
                    "run",
                    "--server",
                    "--addr",
                    f"127.0.0.1:{port}",
                    "--log-level",
                    "error",
                    "-b",
                    str(BUNDLE),
                    str(rules),
                ]
            )
        )
        url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{url}/health", trust_env=False).status_code == httpx.codes.OK:
                    return url
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        raise RuntimeError("the OPA server did not become healthy")

    def stop(self) -> None:
        for process in self._processes:
            process.terminate()
            process.wait(timeout=10)


@pytest.fixture
def opa_server() -> Iterator[OpaServer]:
    server = OpaServer()
    yield server
    server.stop()


class TestOpaPolicyDecisionPoint(PolicyDecisionPointContract):
    @pytest.fixture(autouse=True)
    def _server(self, opa_server: OpaServer) -> None:
        self._opa = opa_server

    async def make_policy(
        self, tmp_path: Path, document: Mapping[str, object]
    ) -> PolicyDecisionPoint:
        url = self._opa.start(tmp_path, document)
        policy = OpaPolicyDecisionPoint(OpaPolicyOptions(url=url), SequentialIds("local"))
        await policy.validate()
        return policy

    async def test_the_revisions_of_both_bundles_are_reported(self, tmp_path: Path) -> None:
        policy = await self.make_policy(tmp_path, self.rules_document)
        try:
            decision = await policy.decide(self.question())
        finally:
            await policy.aclose()  # type: ignore[attr-defined]
        assert decision.bundle_revision == "bundle@0.1.0,rules-0@test-rules-1"


def run_opa(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - a fixed command line, no shell
        [opa_binary(), *arguments, str(BUNDLE)], capture_output=True, text=True, check=False
    )


def test_the_rego_tests_pass() -> None:
    result = run_opa("test")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS:" in result.stdout


def test_the_bundle_is_formatted_and_passes_strict_checks() -> None:
    assert run_opa("fmt", "--fail", "--list").returncode == 0
    checked = run_opa("check", "--strict")
    assert checked.returncode == 0, checked.stderr


def test_opa_refuses_exactly_the_documents_the_local_engine_refuses(tmp_path: Path) -> None:
    """The shared corpus (see test_policy_parity), every document in one evaluation."""
    corpus = load_corpus()
    cases = [
        (group, label, document)
        for group in ("invalid", "valid")
        for label, document in documents(corpus, group)
    ]
    question = tmp_path / "input.json"
    question.write_text(
        json.dumps({"request": corpus["request"], "documents": [case[2] for case in cases]}),
        encoding="utf-8",
    )
    query = (
        "[decision | some document in input.documents; request := input.request; "
        "decision := data.agentlib.authz.decision "
        "with data.agentlib.rules as document with input as request]"
    )
    result = subprocess.run(  # noqa: S603 - a fixed command line, no shell
        [opa_binary(), "eval", "--format", "json", "-d", str(BUNDLE), "-i", str(question), query],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    decisions = json.loads(result.stdout)["result"][0]["expressions"][0]["value"]
    wrong = [
        f"{label}: {decision}"
        for (group, label, _), decision in zip(cases, decisions, strict=True)
        if (group == "invalid" and decision.get("reason_code") != "invalid_rules")
        or (group == "valid" and decision.get("allow") is not True)
    ]
    assert not wrong, "\n".join(wrong)
