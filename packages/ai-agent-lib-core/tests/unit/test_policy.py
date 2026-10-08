"""Policy decisions: the input, the rules provider, the OPA provider, the cache and the stage."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from ai_agent_lib_core.adapters import (
    CachingPolicyDecisionPoint,
    OpaPolicyDecisionPoint,
    OpaPolicyOptions,
    PolicyCacheOptions,
    RulesPolicyDecisionPoint,
    RulesPolicyOptions,
)
from ai_agent_lib_core.adapters.policy_documents import parse_obligations, parse_rules_document
from ai_agent_lib_core.contracts import (
    AgentEntry,
    Classification,
    ConfigurationError,
    Decision,
    DeploymentEnv,
    Obligations,
    PolicyAction,
    PolicyDecisionPoint,
    PolicyDenied,
    PolicyRequest,
    PolicyResource,
    Principal,
    ProviderSelection,
    RequestContext,
    RowFilter,
    Section,
    ServerEntry,
    ServiceConfig,
    ToolEntry,
)
from ai_agent_lib_core.di import ServiceContainer, ServiceProviders
from ai_agent_lib_core.pipeline import (
    ModelCall,
    ModelStage,
    Pipeline,
    ToolCall,
    ToolStage,
    build_model_pipeline,
    build_tool_pipeline,
)
from ai_agent_lib_core.testing import (
    FakePolicyDecisionPoint,
    FakeRegistry,
    Fakes,
    FakeSecretsProvider,
    FrozenClock,
    InMemoryAuditSink,
    RecordingTelemetry,
    SequentialIds,
)

PRINCIPAL = Principal(subject="u-123", tenant="t-9", roles=frozenset({"analyst", "admin"}))


def question(**overrides: object) -> PolicyRequest:
    values: dict[str, object] = {
        "principal": PRINCIPAL,
        "action": PolicyAction.DATA_QUERY,
        "resource": PolicyResource(
            kind="query", name="accounts.by_region", classification=Classification.RESTRICTED
        ),
        "application": "accounts-mcp",
        "environment": "prod",
        "attributes": {"model_provider": "bedrock"},
        **overrides,
    }
    return PolicyRequest(**values)  # type: ignore[arg-type]


# ------------------------------------------------------------- value types


def test_the_decision_input_has_the_versioned_shape_from_the_specification() -> None:
    assert question().to_input() == {
        "schema": "agentlib.decision/v1",
        "principal": {
            "subject": "u-123",
            "tenant": "t-9",
            "roles": ["admin", "analyst"],
            "kind": "user",
            "actors": [],
        },
        "action": "data.query",
        "resource": {
            "kind": "query",
            "name": "accounts.by_region",
            "classification": "restricted",
        },
        "context": {
            "application": "accounts-mcp",
            "environment": "prod",
            "model_provider": "bedrock",
        },
    }


def test_resource_attributes_cannot_replace_the_fixed_fields() -> None:
    resource = PolicyResource(kind="tool", name="lookup", attributes={"name": "x", "server": "s"})
    asked = question(resource=resource, attributes={"application": "forged"}).to_input()
    assert asked["resource"] == {"kind": "tool", "name": "lookup", "server": "s"}
    assert asked["context"] == {"application": "accounts-mcp", "environment": "prod"}


def test_a_deny_never_carries_obligations() -> None:
    with pytest.raises(ValueError, match="a deny carries no obligations"):
        Decision(
            allow=False, reason_code="no", decision_id="d", obligations=Obligations(max_rows=1)
        )
    with pytest.raises(TypeError, match="allow must be a bool"):
        Decision(allow="true", reason_code="x", decision_id="d")  # type: ignore[arg-type]
    assert Decision.allowed("d").allow
    assert Decision.denied("d", "no_matching_rule").reason_code == "no_matching_rule"


# -------------------------------------------------------------- obligations


def test_obligations_are_parsed_into_the_typed_form() -> None:
    assert parse_obligations(None) == Obligations()
    assert parse_obligations({}) == Obligations()
    assert parse_obligations(
        {
            "row_filter": {"region": ["east"], "tier": [1, 2]},
            "mask_columns": ["Holder"],
            "max_rows": 10,
            "require_approval": True,
        }
    ) == Obligations(
        row_filters=(RowFilter("region", ("east",)), RowFilter("tier", (1, 2))),
        mask_columns=frozenset({"holder"}),
        max_rows=10,
        require_approval=True,
    )


@pytest.mark.parametrize(
    "raw",
    [
        {"redact_rows": True},
        {"row_filter": ["region"]},
        {"row_filter": {"region": "east"}},
        {"row_filter": {"region": [{"nested": 1}]}},
        {"mask_columns": "holder"},
        {"mask_columns": [1]},
        {"max_rows": "10"},
        {"max_rows": True},
        {"max_rows": -1},
        {"require_approval": "yes"},
        ["mask_columns"],
    ],
)
def test_an_obligation_that_is_not_understood_is_an_error_never_ignored(raw: object) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - any message; the point is that it raises
        parse_obligations(raw)


# -------------------------------------------------------------------- rules

RULES = """\
schema: agentlib.rules/v1
rules:
  - id: analysts-read
    actions: [data.query]
    roles: [analyst]
    obligations: {mask_columns: [holder]}
"""


def rules_file(tmp_path: Path, text: str = RULES, name: str = "rules.yaml") -> RulesPolicyOptions:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return RulesPolicyOptions(path=path)


async def test_the_rules_provider_decides_from_its_file(tmp_path: Path) -> None:
    policy = RulesPolicyDecisionPoint(rules_file(tmp_path), SequentialIds("d"))
    allowed = await policy.decide(question())
    denied = await policy.decide(question(action=PolicyAction.TOOL_CALL))
    assert (allowed.allow, allowed.reason_code, allowed.decision_id) == (
        True,
        "analysts-read",
        "d-1",
    )
    assert allowed.obligations.mask_columns == {"holder"}
    assert (denied.allow, denied.reason_code, denied.decision_id) == (
        False,
        "no_matching_rule",
        "d-2",
    )
    assert allowed.bundle_revision == denied.bundle_revision == policy.revision
    assert policy.revision.startswith("sha256:")


def test_a_missing_rules_file_is_an_error_not_an_empty_policy(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="the file does not exist"):
        RulesPolicyDecisionPoint(RulesPolicyOptions(path=tmp_path / "absent.yaml"), SequentialIds())


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        (RULES.replace("agentlib.rules/v1", "agentlib.rules/v0"), "schema"),
        (RULES.replace("actions: [data.query]", "actions: [data.delete]"), "actions"),
        (RULES.replace("actions: [data.query]", "actions: []"), "actions"),
        (
            RULES.replace("    roles: [analyst]\n", "    roles: [analyst]\n    effect: deny\n"),
            "effect",
        ),
        (
            RULES.replace("mask_columns: [holder]", "hide_columns: [holder]"),
            "unrecognised obligation",
        ),
        (
            RULES.replace("mask_columns: [holder]", "max_rows: lots"),
            "max_rows must be a whole number",
        ),
        (RULES + RULES.split("rules:\n")[1], "two rules share an ID"),
        (RULES.replace("id: analysts-read", "id: Analysts Read"), "rules.0.id"),
        (RULES + "    resources: ['accounts.(by|x)']\n", "resources"),
        (RULES + "    max_classification: secret\n", "max_classification"),
    ],
)
def test_an_invalid_rules_file_stops_startup(tmp_path: Path, text: str, problem: str) -> None:
    with pytest.raises(ConfigurationError, match=problem) as caught:
        RulesPolicyDecisionPoint(rules_file(tmp_path, text), SequentialIds())
    assert "rules.yaml" in str(caught.value)


def test_a_pattern_star_matches_any_text_and_nothing_else_is_special() -> None:
    (rule,) = parse_rules_document(
        {
            "schema": "agentlib.rules/v1",
            "rules": [
                {"id": "r", "actions": ["tool.call"], "resources": ["accounts/*", "rates.latest"]}
            ],
        }
    )

    def covers(name: str) -> bool:
        return rule.matches(
            question(action=PolicyAction.TOOL_CALL, resource=PolicyResource(kind="tool", name=name))
        )

    assert covers("accounts/lookup")
    assert covers("accounts/")
    assert covers("rates.latest")
    assert not covers("accounts")
    assert not covers("ratesXlatest")
    assert not covers("x-accounts/lookup")


# ---------------------------------------------------------------------- opa

Handler = Callable[[httpx.Request], httpx.Response]


def opa(handler: Handler, **options: object) -> tuple[OpaPolicyDecisionPoint, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    token = options.pop("token", None)
    policy = OpaPolicyDecisionPoint(
        OpaPolicyOptions.model_validate(options),
        SequentialIds("local"),
        token=SecretStr(str(token)) if token is not None else None,
        transport=httpx.MockTransport(recording),
    )
    return policy, seen


def replying(document: object, status: int = 200) -> Handler:
    return lambda request: httpx.Response(status, json=document)


async def test_opa_is_asked_with_the_decision_input_and_its_answer_is_used() -> None:
    reply = {
        "decision_id": "opa-7f3a",
        "provenance": {
            "bundles": {
                "/srv/policies/bundle/": {"revision": "0.1.0"},
                "C:\\svc\\rules": {"revision": "2026.10.1"},
                "unversioned": {},
            }
        },
        "result": {
            "allow": True,
            "reason_code": "analysts-read",
            "obligations": {"mask_columns": ["holder"], "max_rows": 5},
        },
    }
    policy, seen = opa(replying(reply), token="t0ken", auth_secret="opa_token")
    decision = await policy.decide(question())
    await policy.aclose()

    assert decision == Decision.allowed(
        "opa-7f3a",
        reason_code="analysts-read",
        obligations=Obligations(mask_columns=frozenset({"holder"}), max_rows=5),
        bundle_revision="bundle@0.1.0,rules@2026.10.1",
    )
    (request,) = seen
    assert request.method == "POST"
    assert (
        str(request.url) == "http://localhost:8181/v1/data/agentlib/authz/decision?provenance=true"
    )
    assert json.loads(request.content) == {"input": question().to_input()}
    assert request.headers["authorization"] == "Bearer t0ken"
    assert "t0ken" not in repr(policy)


async def test_an_opa_deny_keeps_its_reason_and_drops_any_obligations() -> None:
    reply = {"result": {"allow": False, "reason_code": "no_matching_rule", "obligations": {"x": 1}}}
    policy, _ = opa(
        replying(reply), url="https://opa.internal.test", package="bank/authz", rule="d"
    )
    decision = await policy.decide(question())
    assert decision == Decision.denied("local-1", "no_matching_rule")
    await policy.aclose()


def _raise(error: Exception) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    return handler


@pytest.mark.parametrize(
    ("handler", "reason"),
    [
        (_raise(httpx.ReadTimeout("slow")), "policy_unavailable"),
        (_raise(httpx.ConnectError("refused")), "policy_unavailable"),
        (replying({"result": {"allow": True}}, status=500), "policy_unavailable"),
        (replying({"result": {"allow": True}}, status=302), "policy_unavailable"),
        (lambda request: httpx.Response(200, text="<html>"), "policy_malformed"),
        (replying([{"allow": True}]), "policy_malformed"),
        (replying({}), "policy_malformed"),
        (replying({"result": True}), "policy_malformed"),
        (replying({"result": {"reason_code": "ok"}}), "policy_malformed"),
        (replying({"result": {"allow": "true"}}), "policy_malformed"),
        (replying({"result": {"allow": 1}}), "policy_malformed"),
        (replying({"result": {"allow": True, "reason_code": 7}}), "policy_malformed"),
        (replying({"result": {"allow": True, "reason_code": "two\nlines"}}), "policy_malformed"),
        (replying({"result": {"allow": True, "reason_code": " padded "}}), "policy_malformed"),
        (
            replying({"result": {"allow": True, "obligations": {"redact": ["x"]}}}),
            "obligation_unrecognised",
        ),
        (
            replying({"result": {"allow": True, "obligations": {"max_rows": "9"}}}),
            "obligation_unrecognised",
        ),
    ],
    ids=[
        "timeout",
        "unreachable",
        "http-500",
        "redirect",
        "not-json",
        "not-an-object",
        "undefined-rule",
        "bare-true",
        "no-allow",
        "allow-as-text",
        "allow-as-number",
        "bad-reason",
        "reason-with-newline",
        "padded-reason",
        "unknown-obligation",
        "bad-obligation",
    ],
)
async def test_anything_but_a_well_formed_allow_is_a_deny(handler: Handler, reason: str) -> None:
    policy, _ = opa(handler)
    decision = await policy.decide(question())
    await policy.aclose()
    assert not decision.allow
    assert decision.reason_code == reason
    assert decision.obligations.empty


async def test_opa_validation_checks_the_health_endpoint() -> None:
    healthy, seen = opa(replying({}))
    await healthy.validate()
    assert str(seen[0].url) == "http://localhost:8181/health"
    await healthy.aclose()

    unhealthy, _ = opa(replying({}, status=500))
    with pytest.raises(ConfigurationError, match="health check returned HTTP 500"):
        await unhealthy.validate()
    await unhealthy.aclose()

    unreachable, _ = opa(_raise(httpx.ConnectError("refused")))
    with pytest.raises(ConfigurationError, match="could not be reached"):
        await unreachable.validate()
    await unreachable.aclose()


@pytest.mark.parametrize(
    ("options", "problem"),
    [
        ({"url": "http://opa.internal.test:8181"}, "must use https"),
        ({"url": "https://user:pw@opa.internal.test"}, "must not hold credentials"),
        ({"auth_secret": "opa_token"}, "secret for its token is missing"),
    ],
)
def test_unsafe_opa_options_are_refused(options: dict[str, object], problem: str) -> None:
    with pytest.raises(ConfigurationError, match=problem):
        OpaPolicyDecisionPoint(OpaPolicyOptions.model_validate(options), SequentialIds())


def test_the_opa_package_and_rule_cannot_reach_another_path() -> None:
    for bad in (
        {"package": "../admin"},
        {"package": "a//b"},
        {"rule": "x/y"},
        {"rule": "d?pretty"},
    ):
        with pytest.raises(ConfigurationError):
            ProviderSelection("opa", bad).parse_options(OpaPolicyOptions)


# -------------------------------------------------------------------- cache


async def test_an_identical_question_is_answered_from_the_cache_for_a_short_time() -> None:
    clock = FrozenClock()
    inner = FakePolicyDecisionPoint()
    cached = CachingPolicyDecisionPoint(inner, clock, PolicyCacheOptions(cache_ttl_seconds=30))

    first = await cached.decide(question())
    again = await cached.decide(question())
    other = await cached.decide(question(application="reporting-mcp"))
    assert (first.cached, again.cached, other.cached) == (False, True, False)
    assert again.decision_id == first.decision_id
    assert len(inner.requests) == 2

    clock.advance(31)
    later = await cached.decide(question())
    assert not later.cached
    assert len(inner.requests) == 3


async def test_denies_are_cached_but_failures_are_not() -> None:
    answers: Iterator[str | bool] = iter(["policy_unavailable", "no_matching_rule", True])
    inner = FakePolicyDecisionPoint(lambda request: next(answers))
    cached = CachingPolicyDecisionPoint(
        inner, FrozenClock(), PolicyCacheOptions(cache_ttl_seconds=30)
    )
    assert (await cached.decide(question())).reason_code == "policy_unavailable"
    assert (await cached.decide(question())).reason_code == "no_matching_rule"
    assert (await cached.decide(question())).reason_code == "no_matching_rule"
    assert len(inner.requests) == 2


async def test_the_cache_keeps_a_bounded_number_of_decisions() -> None:
    inner = FakePolicyDecisionPoint()
    options = PolicyCacheOptions(cache_ttl_seconds=30, cache_max_entries=2)
    cached = CachingPolicyDecisionPoint(inner, FrozenClock(), options)
    for application in ("a", "b", "c", "a"):
        await cached.decide(question(application=application))
    assert len(inner.requests) == 4


async def test_the_cache_is_off_unless_configured(tmp_path: Path) -> None:
    options = rules_file(tmp_path)
    spec = ServiceProviders.default().lookup(Section.POLICY, "rules")
    assert spec.local_only
    assert not ServiceProviders.default().lookup(Section.POLICY, "opa").local_only
    registry = Fakes().providers().register(Section.POLICY, "rules", spec.factory)

    def config(**extra: object) -> ServiceConfig:
        selection = ProviderSelection("rules", {"path": str(options.path), **extra})
        return ServiceConfig.for_testing(sections={Section.POLICY: selection})

    async with ServiceContainer(config(), registry) as services:
        assert isinstance(services.policy, RulesPolicyDecisionPoint)
    registry = Fakes().providers().register(Section.POLICY, "rules", spec.factory)
    async with ServiceContainer(config(cache_ttl_seconds=5), registry) as services:
        assert isinstance(services.policy, CachingPolicyDecisionPoint)
        await services.validate()


async def test_the_container_gives_opa_its_token_and_refuses_rules_outside_local(
    tmp_path: Path,
) -> None:
    fakes = Fakes(secrets=FakeSecretsProvider({"opa_token": "t0p"}))
    defaults = ServiceProviders.default()
    registry = fakes.providers().register(
        Section.POLICY, "opa", defaults.lookup(Section.POLICY, "opa").factory
    )
    selection = ProviderSelection("opa", {"auth_secret": "opa_token"})
    config = ServiceConfig.for_testing(
        deployment_env=DeploymentEnv.PROD, sections={Section.POLICY: selection}
    )
    async with ServiceContainer(config, registry) as services:
        assert isinstance(services.policy, OpaPolicyDecisionPoint)

    rules = defaults.lookup(Section.POLICY, "rules")
    registry = (
        Fakes()
        .providers()
        .register(Section.POLICY, "rules", rules.factory, local_only=rules.local_only)
    )
    config = ServiceConfig.for_testing(
        deployment_env=DeploymentEnv.PROD,
        sections={Section.POLICY: ProviderSelection("rules", {"path": str(tmp_path / "r.yaml")})},
    )
    with pytest.raises(ConfigurationError, match="local development only"):
        await ServiceContainer(config, registry).start()


# -------------------------------------------------------------------- stage

CONTEXT = RequestContext(
    principal=PRINCIPAL,
    application="accounts-agent",
    request_id="r-1",
    thread_id="th-1",
    classification_ceiling=Classification.RESTRICTED,
)


class Stage:
    """Both pipelines over one fake policy, with the audit records they write."""

    def __init__(self, policy: PolicyDecisionPoint, registry: FakeRegistry | None = None) -> None:
        self.audit = InMemoryAuditSink()
        self.reached: list[object] = []
        common = {
            "audit": self.audit,
            "telemetry": RecordingTelemetry(),
            "clock": FrozenClock(),
            "ids": SequentialIds(),
            "policy": policy,
            "environment": "dev",
        }
        tools: Pipeline[ToolStage, ToolCall, str] = build_tool_pipeline(registry=registry, **common)  # type: ignore[arg-type]
        models: Pipeline[ModelStage, ModelCall, str] = build_model_pipeline(**common)  # type: ignore[arg-type]
        self.tool = tools.bind(self.terminal)
        self.model = models.bind(self.terminal)

    async def terminal(self, call: object) -> str:
        self.reached.append(call)
        return "done"


async def test_a_tool_call_is_put_to_the_policy_and_the_decision_is_evidence() -> None:
    policy = FakePolicyDecisionPoint()
    stage = Stage(policy)
    assert (
        await stage.tool(ToolCall(context=CONTEXT, tool="lookup_balance", read_only=True)) == "done"
    )
    (asked,) = policy.requests
    assert asked.to_input() == {
        "schema": "agentlib.decision/v1",
        "principal": {
            "subject": "u-123",
            "tenant": "t-9",
            "roles": ["admin", "analyst"],
            "kind": "user",
            "actors": [],
        },
        "action": "tool.call",
        "resource": {"kind": "tool", "name": "lookup_balance", "read_only": True},
        "context": {"application": "accounts-agent", "environment": "dev"},
    }
    attributes = stage.audit.records[0].attributes
    assert attributes["policy_decision_id"] == "decision-1"
    assert attributes["policy_bundle_revision"] == "fake"
    assert attributes["policy_reason_code"] == "allowed"
    assert attributes["policy_cached"] is False


async def test_an_mcp_tool_is_named_by_its_server_and_carries_its_registered_classification() -> (
    None
):
    tool = ToolEntry(
        name="lookup", version="1", classification=Classification.CONFIDENTIAL, read_only=True
    )
    registry = FakeRegistry(
        [
            AgentEntry(
                id="accounts-agent",
                owner="o",
                version="1",
                mcp_servers=("accounts",),
                classification_ceiling=Classification.RESTRICTED,
            )
        ],
        [ServerEntry(id="accounts", owner="o", url="http://localhost/mcp", tools=(tool,))],
    )
    policy = FakePolicyDecisionPoint()
    stage = Stage(policy, registry)
    await stage.tool(ToolCall(context=CONTEXT, tool="lookup", server="accounts"))
    assert policy.requests[0].to_input()["resource"] == {
        "kind": "tool",
        "name": "accounts/lookup",
        "classification": "confidential",
        "read_only": True,
        "server": "accounts",
    }


async def test_a_denied_tool_call_never_runs_and_is_audited_with_the_decision() -> None:
    stage = Stage(FakePolicyDecisionPoint(lambda request: "no_matching_rule"))
    with pytest.raises(PolicyDenied) as caught:
        await stage.tool(ToolCall(context=CONTEXT, tool="lookup_balance"))
    assert caught.value.reason_code == "no_matching_rule"
    assert stage.reached == []
    record = stage.audit.records[0]
    assert record.outcome.value == "denied"
    assert record.attributes["reason_code"] == "no_matching_rule"
    assert record.attributes["policy_decision_id"] == "decision-1"


@pytest.mark.parametrize(
    ("obligations", "reason"),
    [
        (Obligations(require_approval=True), "approval_unavailable"),
        (Obligations(mask_columns=frozenset({"holder"})), "obligation_unenforceable"),
        (Obligations(max_rows=5), "obligation_unenforceable"),
    ],
)
async def test_an_obligation_a_call_cannot_enforce_is_a_deny(
    obligations: Obligations, reason: str
) -> None:
    stage = Stage(FakePolicyDecisionPoint(lambda request: obligations))
    for call, handler in (
        (ToolCall(context=CONTEXT, tool="lookup_balance"), stage.tool),
        (ModelCall(context=CONTEXT, alias="default", provider="fake", model_id="m"), stage.model),
    ):
        with pytest.raises(PolicyDenied) as caught:
            await handler(call)  # type: ignore[arg-type]
        assert caught.value.reason_code == reason
    assert stage.reached == []


async def test_a_model_call_is_put_to_the_policy_as_a_routing_question() -> None:
    policy = FakePolicyDecisionPoint()
    stage = Stage(policy)
    call = ModelCall(context=CONTEXT, alias="judge", provider="bedrock", model_id="model-x")
    assert await stage.model(call) == "done"
    assert policy.requests[0].to_input() == {
        "schema": "agentlib.decision/v1",
        "principal": {
            "subject": "u-123",
            "tenant": "t-9",
            "roles": ["admin", "analyst"],
            "kind": "user",
            "actors": [],
        },
        "action": "model.route",
        "resource": {"kind": "model", "name": "judge", "model_id": "model-x"},
        "context": {
            "application": "accounts-agent",
            "environment": "dev",
            "model_provider": "bedrock",
        },
    }
    assert stage.audit.records[0].attributes["policy_decision_id"] == "decision-1"


async def test_the_policy_is_not_asked_about_an_unidentified_caller() -> None:
    policy = FakePolicyDecisionPoint()
    stage = Stage(policy)
    with pytest.raises(PolicyDenied) as caught:
        await stage.tool(ToolCall(context=None, tool="lookup_balance"))
    assert caught.value.reason_code == "identity_missing"
    assert policy.requests == []


async def test_an_unusable_decision_id_from_opa_is_replaced_not_trusted() -> None:
    for strange in ("", "has\nnewline", "x" * 500, 42, None):
        policy, _ = opa(replying({"decision_id": strange, "result": {"allow": True}}))
        decision = await policy.decide(question())
        await policy.aclose()
        assert decision.allow
        assert decision.decision_id == "local-1"


def test_the_decision_input_names_the_agents_acting_for_the_caller() -> None:
    from ai_agent_lib_core.contracts import PrincipalKind

    through_agents = Principal(
        subject="u-1", tenant="t-9", delegation_chain=("front-agent", "accounts-agent")
    )
    asked = question(principal=through_agents).to_input()["principal"]
    assert asked == {
        "subject": "u-1",
        "tenant": "t-9",
        "roles": [],
        "kind": "user",
        "actors": ["front-agent", "accounts-agent"],
    }
    service = Principal(subject="sp-1", tenant="t-9", kind=PrincipalKind.SERVICE)
    assert question(principal=service).to_input()["principal"]["kind"] == "service"  # type: ignore[index]


# ------------------------------------------------- a denial says how to fix it

NEAR_MISS_RULES = """\
schema: agentlib.rules/v1
rules:
  - id: managers-call-the-tools
    actions: [tool.call]
    applications: [accounts-agent]
    roles: [manager]
    resources: ["accounts/*"]
  - id: helper-uses-models
    actions: [model.route]
    applications: [helper]
    resources: [default]
"""


async def test_the_rules_say_which_rule_came_closest_and_what_it_needs(tmp_path: Path) -> None:
    policy = RulesPolicyDecisionPoint(rules_file(tmp_path, NEAR_MISS_RULES), SequentialIds("d"))
    denied = await policy.decide(
        question(
            action=PolicyAction.TOOL_CALL,
            application="accounts-agent",
            resource=PolicyResource(kind="tool", name="accounts/lookup"),
        )
    )
    assert denied.explanation == (
        "no rule matched; the closest were rule 'managers-call-the-tools': it needs one of "
        "the roles 'manager'; the caller has 'admin', 'analyst' | rule 'helper-uses-models': "
        "it covers 'model.route', not tool.call; it covers application 'helper', not "
        "'accounts-agent'; it covers resources 'default', not 'accounts/lookup'"
    )
    assert "u-123" not in str(denied.explanation)
    nothing = await policy.decide(question(action=PolicyAction.MEMORY_READ))
    assert nothing.explanation == "no rule covers the action memory.read"


async def test_a_denial_names_what_was_refused_the_rule_that_would_allow_it_and_why(
    tmp_path: Path,
) -> None:
    policy = RulesPolicyDecisionPoint(rules_file(tmp_path, NEAR_MISS_RULES), SequentialIds("d"))
    stage = Stage(policy)
    with pytest.raises(PolicyDenied) as caught:
        await stage.tool(ToolCall(context=CONTEXT, tool="lookup_balance"))
    error = caught.value
    assert error.message == (
        "the policy does not allow tool.call on the tool 'lookup_balance' for the application "
        "'accounts-agent'"
    )
    assert error.expected == (
        "a rule with actions [tool.call], applications [accounts-agent] and resources "
        '["lookup_balance"], for one of the caller\'s roles [admin, analyst]'
    )
    assert error.actual is not None
    assert error.actual.startswith("no rule matched; the closest were rule 'managers-call")
    assert error.fix is not None
    assert "agentlib policy test" in error.fix
    assert "reason: no_matching_rule" in str(error)


def test_a_misspelt_field_in_a_rule_is_named_with_the_fields_of_a_rule() -> None:
    document = {"schema": "agentlib.rules/v1", "rules": [{"id": "a", "actons": ["tool.call"]}]}

    with pytest.raises(ConfigurationError) as caught:
        parse_rules_document(document, what="policy rules")

    assert "an unknown field 'rules.0.actons' (did you mean 'actions'?)" in str(caught.value.actual)
    assert "only known fields in rules.0: actions, agents" in str(caught.value.expected)
