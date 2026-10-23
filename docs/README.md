# Documentation map

Start with the task you want to complete. Commands in this index run in Command
Prompt and in a macOS/Linux shell: paths use forward slashes, each `.bat`
launcher has an `.sh` twin (for example `./start-local.sh`), and `python` is
`python3` on macOS/Linux. Python and pip come from your team's approved package
source.

| Task | Read | Expected result |
| --- | --- | --- |
| Test the included business agent | [Local startup and testing](local-startup.md) | Set the root `.env`, start the stack, test memos and same-thread follow-ups, inspect traces, and stop safely |
| Build a new agent | [Getting started](getting-started.md) | Create the `tutorial` workspace, add a tool, and test permissions and behavior |
| Learn the architecture | [Developer guide](developer-guide.html#principles) | Understand interfaces, dependency injection, providers, and the LangGraph boundary |
| Connect an existing agent to the simulator | [Agent Lab setup](../packages/ai-agent-lib-ui/README.md#1-prepare-the-ui-packages) | Register an HTTP agent, start the UI, and resume saved chats |
| Find a configuration variable | [Searchable configuration table](developer-guide.html#variables) | See its purpose, local/AWS values, and provider options |
| Inspect model context and tool use | [Tracing and ReAct walkthrough](developer-guide.html#model-debugging) | Read model input/output and checkpoint history |
| Trace agent runs with Phoenix | [Trace agents with Phoenix](phoenix-trial.md) | Turn tracing on with one setting, run Phoenix locally or send traces to a team-hosted server, and follow Agent Lab's trace links |
| Learn by tracing a real run | [Verified Anthropic run](verified-anthropic-run.md), then [Ask Anthropic two questions](anthropic-example.md) | Read captured model input and checkpoints first, then reproduce them with your own key and model |
| Measure an agent offline and online | [Evals guide](evals.md) | Run generated evals, accept a baseline, capture feedback, and score sampled production answers |
| Understand who is calling | [Identity guide](identity.md) | Follow a signed token from sign-in through the agent and MCP server to policy, and learn why a call was refused |
| Keep turns on one thread in order | [Thread admission](http-thread-admission.md) | Learn when an overlapping request gets HTTP 409 `thread_busy` and which coordination fits your deployment |
| Deploy an agent to AWS Bedrock AgentCore | [AgentCore trial](agentcore-trial.md) | Generate the AgentCore deployment with `--target agentcore`, pass preflight, deploy the rmprep agent and its MCP server, and check the recorded run |
| Understand the business example | [rmprep](../examples/rmprep/README.md) and [context management](../examples/rmprep/docs/context-management.md) | Follow client selection, memo generation, and same-thread questions |
| Upgrade a generated workspace | [Keep generated files up to date](getting-started.md#9-keep-generated-files-up-to-date) and [updating generated code](../packages/ai-agent-lib-cli/README.md#updating-generated-code) | Preview with `agentcraft2 update --plan`, which writes nothing, then run `agentcraft2 update` |
| Change the library | [Contributor setup](../README.md#working-on-the-library) | Install development dependencies and run the appropriate checks |

Add `--diff` to `agentcraft2 update --plan` to see the proposed changes to files you
edited. Without `--plan`, `update --diff` **writes updates** to untouched files.

The README, getting-started tutorial, and HTML developer guide use the same
`tutorial` workspace. Continue from your last completed step when switching
documents. The launcher runs the separate, checked-in `examples/rmprep`
workspace; it does not launch the agent you generated in `tutorial`.

## Find the code

An **interface** describes what a component needs. An **adapter** implements it
for a particular provider. The **composition root** creates those adapters and
injects them into the components that use them.

| Location | Responsibility | Change it when… |
| --- | --- | --- |
| `packages/ai-agent-lib-core/src/ai_agent_lib_core/contracts/` | Framework-independent values and interfaces | Defining a shared capability or boundary |
| `…/config/`, `…/di/` in core | Resolve settings; construct and close providers | Adding a provider or changing configuration/lifecycle behavior |
| `…/pipeline/`, `…/adapters/` in core | Apply policy, limits, audit, and local provider behavior | Changing a cross-cutting concern |
| `…/integrations/` in core | Connect the library to LangGraph, MCP, and HTTP | Changing framework or transport behavior |
| `…/integrations/http/` in core | `AgentService` and `run_agent` (`composition.py`) wire configuration, tracing, lifecycle, and serving around an agent's graph | Changing how every agent service starts, answers, or stops |
| `…/evaluation/` in core | Eval cases, scorers, offline runs, baselines, and sampled online scoring | Changing how an agent is measured; see the [evals guide](evals.md) |
| `packages/ai-agent-lib-aws/` | AWS implementations of library interfaces | Adding cloud-specific behavior |
| `packages/ai-agent-lib-cli/` | Workspace commands and generated templates | Changing what developers generate, install, check, or deploy |
| `packages/ai-agent-lib-ui/src/ai_agent_lib_ui/application/` | Chat admission, dispatch coordination, and response projection | Changing the simulator's workflow |
| `…/contracts/` in UI | Values and ports for catalog, conversations, artifacts, feedback, and diagnostics; imports nothing else in the package | Changing what the application layer may ask of its adapters |
| `…/adapters/` in UI | HTTP, registry, SQLite, documents, diagnostic readers, and the online-eval export | Changing how the UI talks to services or stores state |
| `…/integrations/`, `…/assets/` in UI | FastAPI/Chainlit hooks, pages, and styling | Changing browser behavior or presentation |
| `examples/rmprep/agents/rmprep/src/rmprep/` | Routing, memo/Q&A context selection, and business checks | Changing the RM preparation workflow |
| `examples/rmprep/mcp-servers/rmprep-data/` | Evidence queries over the example data | Changing business data retrieval |
| `tools/local_dev.py`, `tools/local_processes.py` | Repository setup and process supervision | Changing the startup scenarios |

Follow one request in this order:

1. Agent Lab admits a question in `application/chat.py` and sends it through
   `adapters/http.py` to the selected agent's `POST /invoke` endpoint.
2. Core's `integrations/http/agent.py` authenticates the caller and creates the
   request context. The agent's `service.py` declares an `AgentService`, which
   `run_agent` serves; its `respond` function hands the question to the graph.
3. LangGraph runs business nodes. The nodes use injected model and tool wrappers;
   library pipelines apply the configured permissions, limits, and recording.
4. The checkpointer saves graph state. The agent returns an accepted answer and
   optional artifacts/trace metadata. Agent Lab saves and displays that result.

### Know which state you are looking at

| State | Owner | Purpose |
| --- | --- | --- |
| UI conversation history | Agent Lab's `conversations.sqlite` | Display admitted questions, accepted answers, metrics, and memo versions |
| Graph checkpoints | The agent's configured checkpoint provider | Restore workflow state for the next question on the same scoped thread |
| Model input | The agent's graph/context policy | Choose which saved messages, memo snapshots, and evidence reach this model call |
| Terminal outcome journal | The agent's HTTP service, when configured | Recover a saved outcome after a lost response without rerunning the question |
| Model traces and audit records | The agent's diagnostic sinks; Phoenix when tracing is enabled | Explain model attempts and tool calls |

A thread ID links turns; it does not make the entire transcript fit in a model
request. Checkpoint storage and context selection have different jobs. Agent Lab
sends each new question once and lets the agent assemble model context.

## Use the same Python with the right packages

Run these commands from the repository root. Use `setup-local.bat --dev` for
rmprep and UI checks; the full library and guide checks require the additional
[contributor dependencies](../README.md#working-on-the-library).

| Work | Interpreter | Focused check |
| --- | --- | --- |
| Library and CLI (contributor setup) | `python` | `python -m pytest tests/architecture` |
| rmprep (local setup) | `python` with agent packages | First `cd examples/rmprep`, then `python ../../tools/package_runtime.py agent -m pytest`; return with `cd ../..`. |
| Agent Lab | `python` with the UI package folder | First `cd packages/ai-agent-lib-ui`, then `python ../../tools/package_runtime.py ui -m pytest`; return with `cd ../..`. |
| Developer guide (contributor setup) | `python` | `python docs/guide/build_guide.py --check` |
| Local stack readiness | Launcher | With services stopped, run `start-local.bat --smoke-test`; it checks startup and shuts down without sending model requests. |

The UI and MCP services use different dependency versions and communicate over
HTTP. Setup isolates their packages while all processes use the same Python.
See each package README for its full test and type-check commands.

## Current behavior and design material

Use setup guides and [release scope](release-scope.md) for supported behavior.
The [specification](Specification.md), [frontend specification](frontend-specification.md),
and [frontend implementation plan](frontend-implementation-plan.md) also describe
future work. The [ADRs](adr/README.md) record decisions; the
[Phase 0 report](frontend-phase0-review.md) records the initial Chainlit trial.

The [code structure and reliability review](design/code-structure-review-2026-10-13.md)
records three resolved issues, reproduction evidence, and proposed simplifications.
Its proposed commands are recommendations, not commands available in this checkout.
The [code structure, exception handling, and test review](design/code-review-2026-10-22.md)
records the follow-up pass over the whole checkout with prioritized findings and a
suggested fix order. The
[implementation plan](design/implementation-plan-2026-10-22.md) sequences those
fixes and the changes needed to deploy agents and MCP servers to Amazon Bedrock
AgentCore; its status block says which phases are implemented and where the code
departs from the plan's text. [ADR 0005](adr/0005-agentcore-target.md) records
the AgentCore decisions, and the [AgentCore trial](agentcore-trial.md) the pilot.

The [enterprise architecture review](design/enterprise-architecture-review-2026-10-18.md)
(18 October 2026) judges the library against its goals, and the
[improvement specification](design/improvement-specification-2026-10-18.md)
turns its findings into requirements. Both are design records: use the guides above
for behavior in this checkout.

For Agent Lab's package boundaries, presentation manifest, and registry rules, read
[Agent Lab contracts](frontend-contracts.md).

## Keep documentation in sync

- Edit `docs/guide/guide.src.html`, then run
  `python docs/guide/build_guide.py`. The builder regenerates
  the HTML guide and its template-derived companion files.
- Edit generated workspace/service instructions in
  `packages/ai-agent-lib-cli/src/ai_agent_lib_cli/templates/`. Test generation
  before treating an existing generated project's README as authoritative.
- Edit package READMEs and this index directly. Link to the detailed guide for
  advanced setup instead of copying it into every README.
