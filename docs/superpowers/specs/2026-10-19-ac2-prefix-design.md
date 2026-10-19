# AC2_ variable prefix — design

19 October 2026. Maintainer decision: rename the library-owned environment
variable prefix from `EAP_*` to `AC2_*`. **Nothing else changes** — the
repository, distribution and import names, the `agentlib` CLI, the
`.agentlib/` state directory, the `agentlib.*/v*` schema identifiers, the OPA
`agentlib/authz` package, span attributes, MCP `_meta` keys, deploy resource
defaults (`eap-audit`, `eap/{name}`), and `RMPREP_CONTEXT_*` all stay exactly
as they are. Third-party names (`AWS_*`, `PHOENIX_*`, `ANTHROPIC_API_KEY`,
`OTEL_*`, proxies) are untouched by definition.

## Intent

`AC2_<SECTION>_<SETTING>` becomes the canonical spelling of every
library-owned variable, with the old `EAP_` spellings kept working for one
minor release exactly as the specification's own rename policy prescribes:
"an old name keeps working, with a warning, for one minor release."

## Goals

1. Every bound variable is canonically `AC2_*`; generated references
   (`docs/variables.md`, `.env.example`s, the developer guide) show `AC2_*`.
2. Every old `EAP_*` spelling keeps working for one minor release and warns.
   This includes the three families outside the binding table: the
   `*_SECRET_<NAME>` family, `*_PHOENIX_{ENABLED,CAPTURE_CONTENT,ENV_FILE}`,
   and the UI's literal reads (`*_PROFILE`, `*_REGISTRY_*`, `*_IDENTITY_*`).
3. Fail-closed is preserved in both vocabularies: an unknown `AC2_*` name
   fails startup (as unknown `EAP_*` does today), and an unknown `EAP_*`
   name ALSO fails startup during the deprecation window — a legacy typo or
   stale removed variable must never be silently ignored.
4. Live docs, examples, templates, tests, and tools speak `AC2_*`; frozen
   records (Specification, ADRs' historical text, design reviews, executed
   plans, dated verified-run records) are not rewritten.
5. The whole suite stays green, and a live run against the maintainer's
   existing root `.env` (still `EAP_` spelled) works with visible deprecation
   warnings — that file is the alias acceptance test.

## Non-goals / anti-goals

- No renames outside variable names (see the opening paragraph's keep-list).
- No change to deploy resource-name defaults (`eap-*`): they are resource
  names, not variables.
- Anti-goal: a deployed or local environment that silently loses settings —
  the exact hazard the review flagged. Every `EAP_` value must either apply
  (with a warning) or fail loudly; never vanish.
- Anti-goal: rewriting the "Enterprise Agentic Platform" product description
  prose — only text that explains the prefix spelling itself changes.

## Decided design

### D1 — binding table (one constant + systematic aliases)

`config/bindings.py`: `PREFIX = "AC2_"` (all bound names, `_OWNED_NAME`, and
`SECRET_VARIABLE_PREFIX` derive from it); a new `LEGACY_PREFIX = "EAP_"`
constant; every binding gains its legacy spelling in `deprecated_names`
**systematically** (a post-pass or constructor hook mapping
`AC2_X → EAP_X` — never 40 hand-written tuples). The three prose literals in
binding details (`EAP_SECRET_<NAME>`, `EAP_MODEL_PROVIDER=fake`,
`EAP_DEPLOYMENT_ENV=local`) become `AC2_` spellings. The existing resolver
alias machinery (warnings, conflict detection when both spellings are set)
does the rest for bound names; the known-name set already includes
deprecated names via `Binding.names`.

### D2 — prefix policing covers both vocabularies

`config/resolver.py::_reject_unknown_names` polices `AC2_*` (via the derived
`_OWNED_NAME`) AND `EAP_*`-prefixed names: a legacy name that is some
binding's deprecated alias resolves (with the warning); any other
`EAP_*`-prefixed name fails startup with a did-you-mean pointing at the
`AC2_` vocabulary. The secrets `env` adapter's family scan accepts both
`AC2_SECRET_<NAME>` (canonical) and `EAP_SECRET_<NAME>` (deprecated, warn),
preferring the canonical spelling on conflict.

### D3 — the out-of-table families

- `config/phoenix.py`: `PHOENIX_VARIABLES`' three `EAP_PHOENIX_*` entries
  become `AC2_PHOENIX_*`; the loader reads the legacy spelling as a fallback
  (canonical wins on conflict) and surfaces the deprecation in its
  resolution origins/warnings the way the loader already reports origins.
  The two literal reads in the loader's env-file layering follow.
- UI `config/settings.py`: the six literal reads become dual-reads (AC2
  first, EAP fallback); error messages name the `AC2_` spelling.
- `tools/`: `local_dev.py`'s `EAP_MODEL_PROVIDER` probe and
  `phoenix_dev.py`'s `EAP_PHOENIX_ENV_FILE` rewrite become dual-reads;
  `local_config_report.py`'s visible-extras list gains the `AC2_PHOENIX_*`
  names (keeping the `EAP_` entries for the window); every child-env scrub
  prefix tuple (`phoenix_dev.py`, `local_config_report.py`,
  `live/launcher isolation lists`) gains `"AC2_"` alongside `"EAP_"`.

### D4 — tests

Existing fixtures and assertions move to the canonical `AC2_` spelling. New
coverage (the contract of this change): a legacy bound name applies with a
warning naming the replacement; both spellings set → the existing conflict
behavior; an unknown `EAP_*` name fails startup; `EAP_SECRET_<NAME>` and
`EAP_PHOENIX_*` legacy paths work and warn; UI dual-reads. Architecture
tests: the no-literal-outside-config scan forbids BOTH prefixes (the UI and
tools keep their narrowly allowed literal sites — follow the tests' existing
allowlist conventions); the docs vocabulary scan keeps passing because
`Binding.names` includes the aliases — live docs are nevertheless renamed
(goal 4), so only frozen records rely on that.

### D5 — docs, templates, generated artifacts

Live docs (~294 `EAP_` lines), the guide source, template READMEs/examples
(`EAP_MODEL_PROVIDER=fake ...` command lines), and the committed example
`.env.example` files move to `AC2_`. Generated artifacts regenerate from
their generators (variables.md, root and generated-agent `.env.example`s,
developer-guide.html). Where a live doc explains the prefix itself, it now
reads `AC2_<SECTION>_<SETTING>` with one migration line ("`EAP_` spellings
keep working for one minor release and warn"); release-scope.md gains the
migration note. `deploy.py` emits env names from the binding table (it holds
no `EAP_` literal), so generated task definitions follow automatically —
verified, not assumed, in the plan.

### D6 — the maintainer's environment

The root `.env` and the example service `.env` files on this machine keep
their `EAP_` spellings and MUST keep working (with warnings) — they are the
live acceptance test. After verification the summary offers a regenerated
`.env` (canonical spellings, key preserved) without touching the file
unasked.

## Builder-check resolutions (designer's calls)

1. **Conflicting spellings are a startup error, uniformly.** The resolver
   already errors when a deprecated name and its replacement carry
   *different* values; that rule now holds everywhere (bindings, secrets
   family, Phoenix, UI dual-reads) and across layers: equal values coexist
   silently, differing values fail with a message naming both spellings.
   No layer-aware alias precedence is introduced — guessing which spelling
   an operator meant is the hazard, not the fix. The live acceptance runs
   adjust accordingly: run 1 is the untouched `EAP_` `.env`; run 2 is a
   migrated temporary `.env` with `AC2_` spellings; a unit test pins the
   differing-values error.
2. **Deprecation warnings surface through the library logger, not only
   `warnings.warn`.** Resolution-time deprecations emit one WARNING line per
   name per process through the library's structured logging ("EAP_X is
   deprecated; use AC2_X") — `run_agent` logs `Resolution.warnings` right
   after `configure_logging`, the Phoenix loader and UI/tools dual-reads log
   through the same channel at resolution time, and `agentlib doctor` keeps
   returning them. The resolver's existing `DeprecationWarning` stays for
   API callers. Tests assert via log capture; the UI's
   `filterwarnings = ["error"]` is why the logging channel, not
   `warnings.warn`, is the mechanism there.
3. **`agentlib deploy` passes legacy names through and warns in the plan.**
   Deploy writes what the operator's `deploy.env` says (never a silent
   rewrite), but the reviewable plan output lists every legacy spelling with
   its `AC2_` replacement and the window in which it stops working. The
   starter `deploy.env` template generates `AC2_` names;
   `variables.tf.jinja` wording follows.
4. Generated reference: no per-row "replaces EAP_X" notes; one preamble line
   states the rename and the one-release window. The docs vocabulary scan is
   widened so `EAP_`-prefixed mentions in live docs are still checked
   (resolving through the aliases), not silently skipped once `PREFIX`
   changes.

## Verification

Full suite + UI suite + ruff/mypy/lint-imports + regeneration checks; the
new alias/policing tests; a fresh generated agent's `.env.example` shows
`AC2_*`; live smoke (accounts agent + MCP + Phoenix) twice — once with the
untouched `EAP_`-spelled root `.env` (warnings visible, everything works),
once with `AC2_` process-env overrides; `agentlib doctor` shows resolved
settings either way. Final grep: no `EAP_` literal outside config/, tests'
dedicated alias tests, frozen records, and the decided tool
scrub/visible-extras entries.

## Left to the builder

- The exact mechanism for systematic aliases (post-pass over
  `DEFAULT_BINDINGS` vs constructor parameter) — whichever reads cleaner and
  keeps custom tables (`render_reference(custom)`) working.
- Wording of the deprecation warning and did-you-mean messages (match the
  resolver's existing voice).
- Whether the UI dual-read is a tiny helper or six inline fallbacks.
- Order of the docs sweep, provided generated artifacts regenerate in the
  same commits as their generators.
