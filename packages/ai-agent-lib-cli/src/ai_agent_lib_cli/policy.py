"""Trying sample requests against the rules, without starting a service.

A sample says who asks for what and what the answer should be. The samples of
a workspace are its policy tests: they are decided by the same in-process
engine the services use locally and, when asked, by OPA with the platform's
Rego bundle as well, which is what a deployed service uses. The two must agree.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import httpx
import yaml
from pydantic import Field, ValidationError

from ai_agent_lib_cli.errors import CliError
from ai_agent_lib_cli.yaml_lists import extended, yaml_flow, yaml_scalar
from ai_agent_lib_core.adapters import (
    OpaPolicyDecisionPoint,
    OpaPolicyOptions,
    RulesPolicyDecisionPoint,
    RulesPolicyOptions,
)
from ai_agent_lib_core.adapters.system import UuidGenerator
from ai_agent_lib_core.contracts import (
    AgentLibError,
    Classification,
    OptionsModel,
    PolicyAction,
    PolicyDecisionPoint,
    PolicyRequest,
    PolicyResource,
    Principal,
    PrincipalKind,
)

__all__ = [
    "SAMPLES_FILE",
    "SAMPLES_SCHEMA",
    "Outcome",
    "Sample",
    "decide_samples",
    "load_samples",
    "opa_server",
    "rules_engine",
    "samples_text",
]

SAMPLES_FILE = PurePosixPath("tests/policy-samples.yaml")
SAMPLES_SCHEMA = "agentlib.policy-samples/v1"

_HEADER = f"""\
# Sample requests for the rules in policies/agentlib/rules/data.yaml.
# Each says who asks for what, and what the answer should be.
#
#   agentlib policy test          decide them with the rules, as the services do locally
#   agentlib policy test --opa    also with OPA, as deployed services do; the two must agree
#
# This file is yours. agentlib appends samples for a new service and never
# changes or removes one that is here.
schema: {SAMPLES_SCHEMA}
samples: []
"""
_RESOURCE_KINDS = {
    PolicyAction.TOOL_CALL: "tool",
    PolicyAction.DATA_QUERY: "query",
    PolicyAction.MODEL_ROUTE: "model",
}
_ClassificationName = Literal["public", "internal", "confidential", "restricted"]


class Sample(OptionsModel):
    """One request and the answer it should get.

    Attributes:
        name: What the sample shows, in a few words.
        action: What is asked for, for example ``tool.call``.
        application: The service that asks the policy: an agent or an MCP server.
        resource: The name of the thing: a tool, ``<server>/<tool>``, a query
            ``<source>.<query>``, or a model alias.
        classification: How sensitive the resource is, when the service knows.
        roles: The roles of the caller.
        agent: The agent the caller came through, when there is one.
        kind: Whether the caller is a person or a service acting for itself.
        expect: Whether the request should be allowed or denied.
        reason: The reason code the decision should carry: the ID of the rule
            that allows it, or why it is denied. Optional.
    """

    name: str = Field(min_length=1, max_length=120)
    action: PolicyAction
    application: str = Field(min_length=1)
    resource: str = Field(min_length=1)
    classification: _ClassificationName | None = None
    roles: tuple[str, ...] = ()
    agent: str | None = None
    kind: PrincipalKind = PrincipalKind.USER
    expect: Literal["allow", "deny"]
    reason: str | None = None

    def request(self) -> PolicyRequest:
        """Return the question this sample puts to a policy engine."""
        classification = (
            Classification[self.classification.upper()] if self.classification else None
        )
        return PolicyRequest(
            principal=Principal(
                subject="sample-caller",
                tenant="sample-tenant",
                roles=frozenset(self.roles),
                delegation_chain=(self.agent,) if self.agent else (),
                kind=self.kind,
            ),
            action=self.action,
            resource=PolicyResource(
                kind=_RESOURCE_KINDS.get(self.action, "resource"),
                name=self.resource,
                classification=classification,
            ),
            application=self.application,
            environment="local",
        )


class _SamplesDocument(OptionsModel):
    schema_: Literal["agentlib.policy-samples/v1"] = Field(alias="schema")
    samples: list[Sample] = Field(default_factory=list)


def _parse(text: str, what: str) -> list[Sample]:
    try:
        document = _SamplesDocument.model_validate(yaml.safe_load(text))
    except yaml.YAMLError as exc:
        raise CliError(f"{what} is not valid YAML ({type(exc).__name__})") from None
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        )
        raise CliError(f"{what} is not a samples document: {problems}") from None
    names = [sample.name for sample in document.samples]
    repeated = sorted({name for name in names if names.count(name) > 1})
    if repeated:
        raise CliError(f"{what}: these sample names are used more than once: {repeated}")
    return document.samples


def load_samples(path: Path) -> list[Sample]:
    """Read the samples file.

    Raises:
        CliError: If it is missing or not a samples document.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        raise CliError(f"{path} cannot be read; there are no samples to try") from None
    return _parse(text, str(path))


def _block(sample: Mapping[str, Any]) -> str:
    lines = [f"  - name: {yaml_scalar(sample['name'])}"]
    for key in ("action", "application", "resource", "classification", "agent", "kind"):
        if sample.get(key) is not None:
            lines.append(f"    {key}: {yaml_scalar(sample[key])}")
    if sample.get("roles"):
        lines.append(f"    roles: {yaml_flow(sample['roles'])}")
    lines.append(f"    expect: {sample['expect']}")
    if sample.get("reason"):
        lines.append(f"    reason: {yaml_scalar(sample['reason'])}")
    return "\n".join(lines) + "\n"


def samples_text(existing: str | None, samples: Sequence[Mapping[str, Any]]) -> str:
    """Return the samples file with ``samples`` appended, leaving what is there untouched.

    A sample whose name is already in the file is not added again.

    Raises:
        CliError: If the file is not a samples document, or its list is not
            the last thing in it.
    """
    what = f"the samples file ({SAMPLES_FILE})"
    return extended(
        existing if existing is not None else _HEADER,
        key="samples",
        additions={str(sample["name"]): _block(sample) for sample in samples},
        ids=lambda text: [sample.name for sample in _parse(text, what)],
        what=what,
    )


@dataclass(frozen=True, slots=True)
class Outcome:
    """What one engine decided for one sample.

    Attributes:
        sample: The sample.
        allow: What the engine decided.
        reason: The reason code of the decision.
        passed: Whether the decision is the one the sample expects.
    """

    sample: Sample
    allow: bool
    reason: str

    @property
    def passed(self) -> bool:
        """Whether the decision, and the reason if one is expected, match the sample."""
        expected = self.sample.expect == "allow"
        reason_matches = self.sample.reason is None or self.sample.reason == self.reason
        return self.allow == expected and reason_matches


async def decide_samples(
    samples: Sequence[Sample], engine: PolicyDecisionPoint
) -> tuple[Outcome, ...]:
    """Put every sample to ``engine`` and return what it decided."""
    outcomes = []
    for sample in samples:
        decision = await engine.decide(sample.request())
        outcomes.append(Outcome(sample, decision.allow, decision.reason_code))
    return tuple(outcomes)


def rules_engine(rules: Path) -> RulesPolicyDecisionPoint:
    """Return the in-process engine over a rules file.

    Raises:
        CliError: If the rules file is missing or invalid.
    """
    try:
        return RulesPolicyDecisionPoint(RulesPolicyOptions(path=rules), UuidGenerator())
    except AgentLibError as problem:
        raise CliError(str(problem)) from None


OpaStarter = Callable[[Path, Path], AbstractContextManager[OpaPolicyDecisionPoint]]
"""Given the platform's bundle and a workspace's policies folder, starts OPA for a while."""


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@contextmanager
def opa_server(
    bundle: Path,
    policies: Path,
    *,
    wait_seconds: float = 15.0,
    find_program: Callable[[str], str | None] = shutil.which,
) -> Iterator[OpaPolicyDecisionPoint]:
    """Start a local OPA with the platform's bundle and the workspace's rules.

    Args:
        bundle: The platform's Rego bundle.
        policies: The workspace's ``policies`` folder, which OPA loads as data.
        wait_seconds: How long OPA may take to become healthy.
        find_program: Finds a program by name. By default it looks on PATH.

    Raises:
        CliError: If the ``opa`` program or the bundle is missing, or OPA does not start.
    """
    binary = find_program("opa")
    if binary is None:
        raise CliError(
            "the opa program is not on PATH; install it from "
            "https://www.openpolicyagent.org/docs/latest/#running-opa or leave out --opa"
        )
    if not bundle.is_dir():
        raise CliError(f"the platform's Rego bundle was not found at {bundle}; give --bundle")
    url = f"http://127.0.0.1:{_free_port()}"
    process = subprocess.Popen(  # noqa: S603 - a fixed command line, no shell
        [binary, "run", "--server", "--addr", url.removeprefix("http://"),
         "--log-level", "error", "-b", str(bundle), str(policies)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )  # fmt: skip
    try:
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                if httpx.get(f"{url}/health", trust_env=False).status_code == httpx.codes.OK:
                    break
            except httpx.HTTPError:
                pass
            if process.poll() is not None or time.monotonic() > deadline:
                raise CliError("OPA did not start with the bundle and the rules of this workspace")
            time.sleep(0.05)
        yield OpaPolicyDecisionPoint(OpaPolicyOptions(url=url), UuidGenerator())
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
