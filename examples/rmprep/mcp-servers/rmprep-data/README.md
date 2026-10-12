# rmprep-data MCP service

This service reads the workspace's `data` folder through 35 named SQL queries.
Four MCP tools expose client evidence to the agent. Start with the
[workspace walkthrough](../../README.md) for installation using Command Prompt,
Python and pip.

From `examples\rmprep`, with the Python environment active:

```bat
python -m rmprep_data
```

The endpoint is `http://127.0.0.1:8100/mcp`. Its `.env` sets
`data_dir` to `../../data` and `queries_dir` to `queries`. Restart the
service after changing CSV files: DuckDB loads them when the provider starts.

## Tools

All tool names have the prefix `rmprep-data.`.

| Tool | Arguments | Result |
| --- | --- | --- |
| `find_clients` | Required `search`. | Matches, `resolved_uen` and `resolved`, `ambiguous` or `not_found` status. |
| `get_memo_evidence` | Required `uen`; optional `meeting_date`. | 21 evidence sections, review findings with source references, missing sections and limitations. |
| `get_client_records` | Required `uen` and `section`; optional `start_date`, `end_date`, `offset`. | A page of named records, source metadata, masked columns and pagination. |
| `get_reference_data` | Required `section`; optional `search`, `end_date`, `offset`. | A page of shared reference records with the same provenance and pagination fields. |

Dates use `YYYY-MM-DD`. Meeting/end date defaults to today in UTC. Omitted
start date includes all available earlier dates; explicit bounds are inclusive.
Each page contains at most 50 rows. If `truncated` is true, repeat the same
request with `offset=next_offset`. Continue until `next_offset` is null before
claiming an exhaustive list. Do not total a truncated transaction page; request
`payment_totals` for deterministic totals over the full requested window.

The memo pack is intentionally bounded. It uses the latest deposit month and
a payment window from meeting date minus 365 days through meeting date. Other
sections retain their source periods. Truncated sections are explicitly listed.

## Section names

| Topic | Client sections |
| --- | --- |
| Client and relationship | `profile`, `company`, `relationship`, `contacts`, `activities`, `opportunities` |
| Credit and liquidity | `credit`, `facilities`, `covenants`, `utilization`, `borrowing_base`, `credit_exceptions`, `working_capital` |
| Commitments and controls | `commitments`, `treasury_controls`, `source_readiness` |
| Service | `service_requests`, `operational_service_cases` |
| Deposits and payments | `deposits`, `deposit_history`, `payment_totals`, `transactions`, `payment_summaries`, `counterparties` |
| Financial context | `financials`, `profitability`, `benchmarks` |

Reference sections are `catalog`, `rates`, `fx`, `commodities`,
`forward_curve`, `xref` and `data_model`. The optional search matches the
product/rate/commodity name, FX pair, curve, xref type, or dictionary table.
Scenario expectations are excluded from both evidence and the dictionary output.

A follow-up tool call can use:

```json
{"uen":"JONE-0022","section":"covenants","end_date":"2026-08-03","offset":0}
```

Each record includes a `source_ref`, such as
`credit_metrics[uen=JONE-0022]`. A section also describes its date and
measurement limitations. A retrieval timestamp says when this process read the
snapshot; use the record's own date when describing the fact.

Client pages also carry `evidence_limits`. Read these before interpreting a
status or date: `waiver_expiry` is a recorded date, `Closed Won` is a sales
stage, and a blank receipt date leaves receipt unknown. None establishes the
corresponding execution, implementation or delivery outcome.

Financial and working-capital pages include `comparisons` with both source
references, original values, absolute change, units and percentage change.
Percentages use decimal arithmetic rounded to two places. Comparisons require
compatible periods and units; masked or missing values and truncated pages
produce no comparison. A zero or negative baseline leaves percentage change
unavailable. Metric changes alone do not establish causes or actual cash flows.

Open exceptions and commitments include `days_overdue`, calculated against
the requested end date. Payment totals include `coverage`: requested start/end,
first/last observed transaction dates and an explicit limit on ledger completeness.
The requested end date is not a claim that transactions were supplied through it.

Unknown UENs produce empty detail pages or a `not_found` memo pack.
Invalid sections, negative offsets and inverted date ranges fail validation.
Queries bind parameters and scope client records by UEN, joining facilities
where needed. Tools accept neither raw SQL nor arbitrary table names.

## Design and checks

`server.py` declares transport interfaces. It passes the governed query source
to `ClientEvidence` in `evidence.py`. That class composes results without
opening files or knowing the database provider. `signals.py` calculates review
findings; `measurements.py` adds comparisons and evidence limits after policy
and masking have run. `sections.py` describes the allowed sections and
provenance, while SQL files define retrieval. This separation lets tests exercise
business calculations and real queries independently.

The library enforces tool and data policy and records audit events. The example
permits analysts and managers to read all clients. Portfolio entitlements and
production data mappings require your team's policy.

```bat
python -m pytest mcp-servers\rmprep-data\tests -q
python -m ai_agent_lib_cli check
```

After intentionally changing a tool interface, refresh its registry pins:

```bat
python -m ai_agent_lib_cli registry pin rmprep-data
```

See the [data review](../../docs/data-review.md) for table coverage, quality
limitations and the exact review thresholds.

