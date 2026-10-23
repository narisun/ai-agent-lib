# The RM data snapshot

The MCP reads 33 CSV files from `examples/rmprep/data`, covering 46 client
UENs. Client records join on `uen`; utilization and covenants join through
`facilities.facility_id`. The reviewed files have no orphan client or facility
keys. That establishes join consistency, not independent verification of the
business figures.

The files include real company names and example financial/operational records.
Use the snapshot for development and evaluation. It is not a live source of
client information or external news.

## Tables and coverage

The following counts describe the supplied snapshot. They change if you replace
the files. The runtime loads CSVs at startup, so restart the MCP after edits.

| Table | Rows |
| --- | ---: |
| `accounts` | 46 |
| `activities` | 252 |
| `balances` | 2,450 |
| `benchmarks` | 476 |
| `borrowing_base` | 4 |
| `catalog` | 13 |
| `commitments` | 11 |
| `commodities` | 8 |
| `company` | 46 |
| `contacts` | 119 |
| `counterparties` | 1,340 |
| `covenants` | 56 |
| `credit_exceptions` | 8 |
| `credit_metrics` | 46 |
| `data_model` | 310 |
| `facilities` | 54 |
| `financial_facts` | 1,323 |
| `forward_curve` | 7 |
| `fx` | 6 |
| `operational_service_cases` | 6 |
| `opportunities` | 153 |
| `payment_summaries` | 9,072 |
| `product_profitability` | 210 |
| `rates` | 10 |
| `relationship` | 46 |
| `rm_scenarios` | 10 |
| `service_requests` | 83 |
| `source_readiness` | 13 |
| `transactions` | 30,771 |
| `treasury_controls` | 12 |
| `utilization` | 1,296 |
| `working_capital` | 27 |
| `xref` | 113 |

`rm_scenarios` contains 10 evaluation cases and expected signal codes. Only
tests read it. None of the named evidence queries selects it, and the
`data_model` query excludes its dictionary rows. Those expectations must never
become evidence in a model prompt.

The four tools select from 35 reviewed SQL queries. The section
`payment_totals` aggregates raw transactions; `deposits` selects the latest
available month; `deposit_history` returns dated balance history. All other
client sections expose scoped source records, sometimes joining facilities or
the client's industry to establish the relationship.

## Read the dates before interpreting a figure

| Evidence | What the date means |
| --- | --- |
| Balances and utilization | A monthly snapshot. The latest supplied month is June 2026. |
| Transactions | Value date; supplied transactions run from July 2024 through June 2026. |
| Borrowing base | Certificate `as_of`; the supplied certificates are dated July 31, 2026. |
| Financial facts | Period end, including future December 31, 2026 rows. The query excludes period ends after the requested cutoff. Publication dates are absent. |
| Credit metrics | `risk_rating_as_of` dates the rating, not necessarily every financial ratio. `annual_review_date` is a review milestone. |
| Covenants | `next_test_date` is the next test, not the date of the reported result. No test observation date is supplied. |
| Commitments, exceptions, controls and readiness | Dates describe the recorded action or update. Current status is not a history of every prior status. |
| Accounts, relationship, contacts and counterparties | Current supplied snapshots without a complete observation date. Review/relationship dates do not establish freshness. |
| Profitability | FY and TTM labels; exact observation dates are absent. Periods can overlap. |
| Market reference tables | Each row's own `as_of` and source label, not a live market quote. |

The meeting/end date defaults to today in UTC. For repeatable scenario work,
use `2026-08-03`. Date filters constrain available dated rows. They cannot
prove what was published or known on a historical date for sources without
publication/status history.

Every evidence page carries retrieval metadata, a source-table name, a note
about interpretation, and a `source_ref` for each record. For example,
`credit_metrics[uen=JONE-0022]` identifies the credit row. A retrieval timestamp
means the process read the CSV at that time; it does not make the CSV current.

## Measurements that need care

| Measurement | How this example treats it |
| --- | --- |
| Client annual revenue | Client sales in USD according to the dictionary. It is not the bank's income. |
| Relationship NII/NIR and profitability | Bank-attributed income in USD. Keep NII, NIR, expenses, risk cost and allocated capital separate. Avoid combining overlapping FY and TTM values. |
| Facility amounts | USD per the dictionary. Syndicated commitment and `our_hold` are different. Facility outstanding and monthly utilization can describe different snapshots. |
| Borrowing-base availability | Supplied excess availability after reserves. Divide by facility commitment only when currency is USD; otherwise leave the percentage unavailable. |
| Deposits | Native currency, separated by account type. Average collected balance, period-end balance and idle estimate are distinct. Monthly balances are not additive across months. |
| Payments | Sum raw transactions by canonical rail, direction and native currency using decimal arithmetic. Label both the requested date range and last observed transaction date from `coverage`. Gross fees differ from attributed NIR. |
| Source payment summaries | Can combine native-currency amounts under a modal currency label. Retained for inspection with an explicit warning; use `payment_totals` for currency-safe totals. |
| Counterparties | The dictionary labels annual value USD while the currency field describes a corridor. Verify that mapping and the missing measurement period before currency calculations. |
| NPS | A category: Promoter, Passive or Detractor. It is not a numeric score in these files. |
| SIC and NAICS | Classification identifiers. SIC is rendered with four digits so leading zeros survive CSV numeric inference; NAICS is rendered as text. |
| Product catalog | Indicative criteria and pricing. Blank DSCR limits and textual client ratings are insufficient for an automatic eligibility decision. |
| Benchmarks | Matched by the first five NAICS digits. Units are preserved; cohort methodology and observation dates are absent. |

An empty section means evidence is unavailable. It does not establish no
activity, zero balances, or an absence of risk. Source-readiness rows can
explicitly mark a domain stale or under review; absence of a readiness row does
not mean that domain is ready. Some source text also contains encoding artifacts;
verify the original source before quoting it externally.

## What the evidence can establish

Client pages expose these limits in `evidence_limits`, so both specialists receive
the same interpretation rules with the data.

| Record | Supported interpretation |
| --- | --- |
| Waiver exception | Report the requirement, status, owner, due date and recorded expiry. These establish neither execution nor non-execution. Active coverage, approval conditions and blockers remain unknown; overdue statements do not establish a cause of delay. Ask whether an executed document or a documented blocker exists. |
| Open commitment or blank receipt date | Report recorded status. Verify delivery separately; these fields do not prove that nothing was delivered. Commitments have no `received_date` field. |
| Audited-statement requirement | Report the requirement. It does not identify a fiscal year or establish whether the supplied financial facts were audited or delivered. |
| Closed Won opportunity or Closed service request | Report the sales stage or request status. Successful implementation and client acceptance require further evidence. |
| Working-capital movement | Report the measured change. Put possible collection, customer or production explanations under questions to investigate. Actual cash flows are not established by ratios or balance changes. |
| Covenant and credit ratios | Apply contractual thresholds to the covenant-tested values. Keep credit-metric ratios separate; their observation dates and calculation methods are not supplied. |

`measurements.py` calculates changes using decimal arithmetic. Financial
comparisons require matching client, concept, statement, unit and annual/interim
classification. Working-capital comparisons retain units, so a move from 14%
to 23% is **9 percentage points**. Each comparison supplies both complete
`source_refs`. Missing or masked values are left unavailable; incomplete pages
do not produce comparisons. Percentage changes require a positive baseline and
are rounded to two decimal places without further rounding in the answer.

For JONE-0022, FY2024 gross profit of USD 63,172,556.33 fell to USD 43,283,347.46
in FY2025: a **31.48% decline**. The payment window requested for the August 3,
2026 meeting is August 3, 2025 through August 3, 2026, while the last observed
transaction is **June 28, 2026**. That gap does not establish zero later activity.

The agent checks that bracketed citations exactly match references in MCP tool
results and that the reply fits its word budget. It allows two revisions before
raising a validation error. This catches abbreviated references and excessive
length. It also catches citation placeholders such as "corresponding references"
and dated audited-statement wording when the exception's reporting year is
unknown. Waiver checks reject known execution/non-execution claims and invented
blockers, including hedges such as "overdue statements may be blocking execution".
They use the existing execution limit, so saved evidence from older checkpoints
is covered too. These are checks for known failure patterns, not a proof that every
claim follows from a cited row. Review the accepted prose against the records.
When an answer cites a financial page and names a common financial comparison,
it must include both references for that concept. This catches a paragraph that
reports a gross-profit change but cites only revenue. The supported names are
defined in `response_review.py`; extend them alongside new business vocabulary.

## Deterministic review findings

The MCP calculates these findings in `signals.py`. The model explains them and
proposes questions; it does not decide whether a client is approved for lending.
These thresholds are explicit example rules for discussion with business owners.

| Code | Trigger |
| --- | --- |
| `COVENANT_BREACH_RISK` | Recorded `in_compliance` is false. Use that verdict, since different covenants have different comparison directions. |
| `REFINANCING_URGENCY` | Maturity is within 90 days of the meeting date, including dates already past. The date difference is calculated in SQL. |
| `BORROWING_BASE_PRESSURE` | Latest supplied availability is below 10% of the matching USD commitment. |
| `ANNUAL_REVIEW_DUE` | Annual review is due within 30 days, or already past. |
| `WATCH_STATUS` | A supplied watch status other than None or Pass. |
| `WEAK_DEBT_SERVICE_COVERAGE` | Credit-metrics DSCR is below 1.10. Keep it separate from covenant-tested DSCR. |
| `WORKING_CAPITAL_STRAIN` | Current ratio is below 1.0. |
| `WORKING_CAPITAL_DETERIORATION` | Latest versus previous period: DSO rises by at least 5 days or cash conversion cycle by at least 10 days, with matching currency. |
| `CREDIT_DOCUMENT_EXCEPTION` | An unresolved credit exception is past its due date. |
| `UNRESOLVED_CLIENT_COMMITMENT` | An unresolved commitment is past its due date. |
| `TREASURY_CONTROL_ISSUE` | An unresolved treasury control observation. |
| `SOURCE_READINESS_ISSUE` | The latest readiness observation for a domain is not marked ready. |
| `OPERATIONAL_FRICTION` | An open priority 1 or 2 service case. |
| `CRITICAL_SERVICE_ISSUE` | An open priority 1 service case. |
| `DETRACTOR_NPS` | The supplied NPS category is Detractor. |

Each finding links to the source rows that caused it. Duplicate service cases
across the two service tables are counted once. Latest borrowing-base and
readiness observations supersede earlier ones. Missing or masked numbers are
not converted to zero.

Findings are based on the evidence pages returned to this caller. If a section
is truncated, retrieve further pages before making an exhaustive assessment.
Covenant failures, imminent maturity, constrained availability and critical
service issues appear as urgent agenda items ahead of optional opportunities.
A healthy relationship can have no new findings.

## Examples to verify

These checks use the supplied snapshot with meeting date August 3, 2026:

| Client | Example result |
| --- | --- |
| JONE-0022 | Recorded covenant failures, overdue reporting, watch status and worsening working capital; the memo must qualify stale evidence. |
| SHIN-0039 | FAC000000020 matures on August 28: 25 days away. USD 3,800,000 availability / USD 49,891,183.24 commitment = 7.62%. |
| WAWO-0046 | USD 760,000 availability / USD 15,841,693.67 commitment = 4.80%, alongside late reporting and control/readiness issues. |
| ALLO-0001 | Service recovery, unresolved commitments and the Detractor category take priority over pipeline discussion. |
| CANA-0010 | CAD relationship and service/control issues; payment totals keep each native currency separate. |
| FARI-0016 and PEND-0033 | Control cases with no new review findings under these rules. Do not manufacture an alert. |

The scenario tests check expected findings against source-derived calculations.
They do not pass scenario descriptions or expected codes to the agent. The
scripted graph tests verify MCP evidence delivery and checkpoint reuse.
Assess generated memo quality separately with a configured model and RM reviewers.

## Extending the example

For a new business question, first identify the source and measurement meaning.
Add or adjust a parameterized query, keep client scoping and provenance, then
add its section to the enum and metadata. Add tests for dates, currency, missing
records and authorization. Update the prompts only after the evidence contract
is clear. Refresh tool pins when the public MCP schema changes.

For production, map the reviewed queries to curated Redshift views, implement
portfolio entitlements, agree the review thresholds with Credit and Operations,
and add an approved news source with publication dates and links. The existing
provider boundary allows those changes without moving database access or
authentication into the LangGraph nodes.

