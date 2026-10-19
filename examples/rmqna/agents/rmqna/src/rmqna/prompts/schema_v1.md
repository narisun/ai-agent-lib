# Schema document v1

These are all the tables and columns the SQL can read. A name that is not listed here does not exist. 31 tables in 11 domains.

## Conventions
- Tables are named by their file: write FROM accounts, never FROM crm.accounts. A domain is only a grouping label.
- uen is the client key, text such as 'ALLO-0001', shared by every client-level table. Look a client up by name in accounts.name first; never guess a uen.
- Types are the types DuckDB gives the loaded data. Where the dictionary names another type it is shown as (dictionary: ...); write each comparison for the type shown, for example cast a BIGINT code before substr().
- Value lists are profiled from the loaded data: 'Values (all)' is the complete set of distinct values, 'Values (e.g.)' is three samples spread across the sorted distinct values, and 'free text' means prose with no list. Where a description's 'Allowed:' prose disagrees with a complete list, the list is what is stored.
- Booleans are true/false (a description that says Yes|No quotes the source system); benchmarks.higher_is_better is 1/0. An empty cell is NULL. Columns ending _pct are on a 0-100 scale, not 0-1.
- Money: facilities, opportunities, relationship, product_profitability and the benchmarks usd rows are USD. balances, working_capital, borrowing_base, transactions and payment_summaries carry a currency column; never add amounts across currencies without converting through fx (USD/CAD is CAD per 1 USD).
- Risk ratings are a word scale in credit_metrics (risk_rating, prior_risk_rating) and facilities (facility_risk_rating), and a number in catalog.max_risk_rating. xref maps one to the other, with xref_type = 'risk_rating', xref_name the word and xref_value the number as text, where 1 is best. xref keeps one row per accepted spelling, so each word has several rows (the word, a lowercase form, alternates such as 'Pass-1', and the number): a plain join on xref_name multiplies rows several times. Join on xref_name and keep only the number row with TRY_CAST(xref_value AS INT) IS NOT NULL, which leaves one row per rating; a plain CAST raises on the word rows.
- A client's NAICS code is 6 digits in accounts.naics_code and 5 digits in benchmarks.naics_industry, both loaded as BIGINT: join on accounts.naics_code // 10 = benchmarks.naics_industry. A client with no naics_code has no benchmark rows. Descriptions that show substr(naics_code,1,5) or a domain-qualified name such as crm.accounts describe the dictionary, not the loaded tables.

## Join paths
Join only along the paths declared in the [Relationships] sections. A bare edge is a join key (equal values). A 'soft reference' joins on equal text but is not enforced, so expect unmatched rows. 'reconciliation' and 'consistency' edges say that two stored figures agree: compare them, never join on them. 'eligibility gate' edges compare a client figure with a product threshold. 'via first 5 digits' is a calculated join (see the NAICS convention). An edge to xref.xref_name names the vocabulary (xref_type) whose canonical names the column holds. Every client-level table has an edge to accounts.uen, but utilization and covenants carry no uen: reach their client through facilities.facility_id. Two many-row tables joined on uen multiply rows, so aggregate each to one row per client before joining. If no declared path links two tables, there is no join: say so rather than inventing one.

## Metric definitions
- **exposure** — Exposure is committed credit exposure: our bank's share of the commitment, not the drawn balance. Per facility it is facilities.our_hold, which the dictionary calls the true exposure; per client it is SUM(facilities.our_hold) over the client's facilities. relationship.total_exposure is the credit system's client-level aggregate, at least that sum and often more, so name which of the two an answer uses. Drawn amounts are a different measure: facilities.outstanding and utilization.drawn cover the whole facility, not our share. Facility amounts are USD.
- **utilization** — Utilization is drawn divided by committed, on a 0-100 scale. The stored measure is utilization.utilization_pct, one row per facility per month-end; current utilization is the row at MAX(as_of) for that facility. For a client or a portfolio, divide summed drawn by summed commitment (utilization.drawn with facilities.commitment) and never average the percentages. utilization.available is commitment less drawn, adjusted for borrowing-base limits. facilities.outstanding is a separate balance and can differ from the latest utilization.drawn.
- **weighted pipeline** — Weighted pipeline is SUM(amount * probability / 100) over open opportunities only, that is stage IN ('Prospecting', 'Qualification', 'Proposal', 'Negotiation'). probability is already 0-100 with no % sign. Closed Won (probability 100) is booked business and Closed Lost (0) is gone: both are excluded from pipeline, weighted or not. Unweighted pipeline is SUM(amount) over the same open stages. Amounts are USD.
- **covenant breach** — A covenant breach is a covenants row with in_compliance = false. That is the compliance workflow's tested verdict: use it as stored and do not re-derive it from threshold (text that includes the operator, such as '>= 1.25') and current_value. Covenants attach to facilities, so a client's covenants are reached through facilities.uen; a client is in breach when any covenant on any of its facilities has in_compliance = false. Count breaches per covenants row unless asked per client. next_test_date is the next scheduled test, not a breach date.
- **cash conversion cycle** — Cash conversion cycle in days is DSO + DIO - DPO. working_capital stores it as cash_conversion_cycle_days beside dso_days, dio_days and dpo_days, one row per client per quarter-end (period_end): use the stored value. A client's current cycle is its row at MAX(period_end); a lengthening cycle is a higher value than at an earlier period_end. Not every client has working_capital rows. dio_days is 0 for a business with no inventory.
- **as-of rule** — The data is one frozen snapshot whose latest month-end is 2026-06-30. Every balance and metric row is as of that snapshot: 'current' and 'latest' mean the row with the greatest date for its key (MAX(as_of) per facility, per account and account type, MAX(period_end) per client), never CURRENT_DATE or now(), and balances are never added across months. Date filters and trends use the row's own date columns (as_of, period_end, value_date, due_date and so on). utilization, balances and payment_summaries keep 24 month-ends per key; rates, fx, commodities and forward_curve hold one dated observation per name, not a history. Some tables hold rows dated shortly after the month-end (borrowing_base, credit_exceptions, source_readiness, commitments): they are part of the snapshot, not the future. A relative window such as overdue, next 90 days or last quarter is measured from the date the user gives, or else from 2026-06-30; say which date was used.

## Domain: crm
The client master and the relationship record around it. accounts holds one row per client (a legal entity) and is the root every client-level table joins to through uen. relationship is the one-row-per-client summary of revenue, deposits, exposure and engagement. The remaining tables are the coverage team's working record: contacts, the sales pipeline, logged activities, service tickets and promises made in meetings.

### accounts
Domain: crm. Role: The client master: one row per client (legal entity), with its uen, name, industry, location, currency, segment and relationship manager.
Rows: 46. Key: uen. Unique: name.
- uen VARCHAR PK — Unique Entity Number - the client identifier at legal-entity level and the universal join key of the model. Produced by identity mastering (resolve every source system's ids and name variants to one legal entity); stable across refreshes; never taken from a single source column. Values (e.g.): 'ADUD-0013', 'KING-0023', 'WAWO-0046'
- connected_uen VARCHAR FK→accounts.uen (each entry; may be outside the book) nullable — Semicolon-separated list of related legal entities (parent, subsidiaries, affiliates) forming the connected relationship group. Related entities stay separate rows - never merged. Values: all NULL
- name VARCHAR UK — Client display name (legal name incl. suffixes). Must be unique across clients: name-based lookup resolves exact match then unique substring. Values (e.g.): 'A. Duda & Sons, Inc.', 'King Arthur Baking Company, Inc.', 'Wawona Packing Co., LLC'
- industry VARCHAR nullable — Free-text industry description. Advisor context and news-search seasoning. Values (e.g.): 'Agricultural Production', 'Hand Tool Manufacturing', 'Workplace Health Services'
- sic_code VARCHAR nullable — SIC classification kept for cross-referencing older bank/credit systems. String - leading zeros matter. Values (all): '0100', '1600', '2000', '2600', '3500', '5900', '6000', '8000', '8700', '9900'
- naics_code BIGINT (dictionary: string) FK→benchmarks.naics_industry (via first 5 digits) nullable — NAICS classification. Calculated join: substr(naics_code,1,5) selects the client's industry benchmark set. Empty naics_code means no industry comparison (honest empty state). Values (e.g.): 111219, 314910, 722513
- annual_revenue DOUBLE nullable — The CLIENT's annual business revenue in USD (their sales, not the bank's revenue from them). Source: prefer latest fiscal-year Revenues fact from financials; else the CRM-entered figure. Gates product eligibility (min_annual_revenue). Values (e.g.): 25978400, 201376559.37, 948510000
- employees BIGINT nullable — Headcount. Context only. Values (e.g.): 60, 500, 4000
- city VARCHAR nullable — Headquarters city. Values (e.g.): 'Boston', 'Milwaukie', 'Westport'
- state VARCHAR nullable — HQ state; carries the PROVINCE code for Canadian clients (e.g. ON, BC). Values (e.g.): 'AZ', 'MT', 'WI'
- country VARCHAR — HQ country. Allowed: USA | Canada. Required - drives currency declaration and vocabulary expectations. Values (all): 'Canada', 'USA'
- currency VARCHAR — Declared: the client's primary operating currency, set once from domicile (CAD for Canadian clients, else USD) and never re-derived downstream. Allowed: USD | CAD. Transactions/accounts still carry their own currency. Values (all): 'CAD', 'USD'
- relationship_manager VARCHAR nullable — Covering RM's name. Memo header. Values: personal data, no example values
- segment VARCHAR nullable — Bank segment label (e.g. Middle Market). Values (all): 'Lower Mid-Market', 'Mid-Market', 'Upper Mid-Market'
- customer_since DATE nullable — Relationship start date (first account-open if CRM lacks it). Tenure framing. Values (e.g.): '2012-08-26', '2018-11-09', '2024-11-07'
- founded_year BIGINT nullable — Company founding year (memo shows the year only). Values (e.g.): 1790, 1948, 2011
- business_description VARCHAR nullable — 1-3 sentence description of what the client does. Advisor grounding. Values: free text
- locations VARCHAR nullable — Operating footprint, HQ first (e.g. 'Stamford CT (HQ); 278 showrooms nationwide'). Values: free text
- nps VARCHAR nullable — Derived: NPS BAND mapped from the raw score (9-10 Promoter, 7-8 Passive, 0-6 Detractor). Allowed: Promoter | Passive | Detractor. Detractor fires the attrition-risk watch signal. Values (all): 'Detractor', 'Passive', 'Promoter'
- logo_url VARCHAR nullable — Client logo URL for the memo header. Cosmetic. Values: all NULL

### contacts
Domain: crm. Role: People at each client: several rows per client, with title, buying role, department and contact details, and a primary-contact flag.
Rows: 119. Key: uen, first_name, last_name (composite).
- uen VARCHAR PK FK→accounts.uen — Client key. Contacts carry no synthetic id - a client simply has multiple contact rows; raw CRM contact ids are dropped at mapping time. Values (e.g.): 'ADUD-0013', 'KING-0023', 'WAWO-0046'
- first_name VARCHAR PK — Contact first name. Values: personal data, no example values
- last_name VARCHAR PK — Contact last name. Values: personal data, no example values
- title VARCHAR nullable — Job title. Values (all): 'CFO', 'Controller', 'Owner', 'Treasurer', 'VP Finance'
- email VARCHAR nullable — Email address. Values: personal data, no example values
- phone VARCHAR nullable — Phone number. Values: personal data, no example values
- is_primary BOOLEAN nullable — Primary relationship contact flag (Yes|No); sorts contacts in the memo. Exactly one Yes per client preferred. Values (all): false, true
- role VARCHAR nullable — Buying role (e.g. 'Economic buyer', 'Treasury lead') - who owns which decision. Values (all): 'Decision-maker', 'Economic buyer', 'Influencer', 'User'
- department VARCHAR nullable — Organizational unit. Values (all): 'Executive', 'Finance', 'Operations', 'Treasury'

### opportunities
Domain: crm. Role: The sales pipeline: one row per deal, with product, stage, amount, win probability, expected close date, competitor and owner.
Rows: 153. Key: opportunity_id.
- opportunity_id VARCHAR PK — Stable deal key from the source CRM. Values (e.g.): 'OPP-9001', 'OPP-9547', 'OPP000000004900'
- uen VARCHAR FK→accounts.uen — Client key. Values (e.g.): 'ADUD-0013', 'KING-0023', 'WAWO-0046'
- name VARCHAR — Deal name as the RM knows it. Values (all): 'Commercial Card Program — AP automation', 'FX Forward Program — cross-currency hedging', 'Investment Sweep — idle cash deployment', 'Lockbox — receivables automation', 'Positive Pay — payment fraud protection', 'Revolving Credit Facility — working capital', 'Treasury Management — operating account consolidation'
- product VARCHAR FK→catalog.name (soft reference) nullable — Bank product being sold; align with catalog names so pipeline ties to whitespace analysis. Values (all): 'Card Program', 'FX Hedging', 'Lockbox', 'Positive Pay', 'Revolving Credit', 'Sweep Account', 'Treasury Management'
- stage VARCHAR FK→xref.xref_name (xref_type = 'opportunity_stage') — Pipeline stage. Allowed: Prospecting | Qualification | Proposal | Negotiation | Closed Won | Closed Lost. Translate source stages via xref. Values (all): 'Closed Lost', 'Closed Won', 'Negotiation', 'Proposal', 'Prospecting', 'Qualification'
- amount DOUBLE nullable — Deal size, USD. Values (e.g.): 40781.61, 208570.82, 62991694.95
- probability BIGINT nullable — Win probability 0-100 (no % sign). Weighted-pipeline math. Values (e.g.): 0, 41, 100
- close_date DATE nullable — Expected close date. Values (e.g.): '2024-03-01', '2025-10-27', '2027-02-22'
- competitor VARCHAR nullable — Named competitor on the deal. Displacement talk track. Values (all): 'Bank of America', 'Citizens Bank', 'Fifth Third', 'JPMorgan Chase', 'PNC', 'RBC', 'TD Bank', 'Truist', 'U.S. Bank', 'Wells Fargo'
- owner VARCHAR nullable — Deal owner on the bank side. Values: personal data, no example values
- created_date DATE nullable — Creation date. The pipeline-stagnation signal ages open deals by this (45-day threshold). Values (e.g.): '2023-12-17', '2025-06-26', '2026-07-10'
- next_step VARCHAR nullable — Free-text next action; shown verbatim in the memo. Values (all): 'Agreement in legal review; credit approval in progress', 'Confirming decision process and budget with the CFO', 'Discovery meeting with the treasurer being scheduled', 'Pricing proposal delivered — awaiting client feedback'

### activities
Domain: crm. Role: Logged client interactions (calls, emails, meetings): one row per activity, with date, subject, owner and notes.
Rows: 252. Key: activity_id.
- activity_id VARCHAR PK — Stable key from the source CRM. Values (e.g.): 'INT000000000000', 'INT000000002802', 'INT000000004906'
- uen VARCHAR FK→accounts.uen — Client key. Values (e.g.): 'ADUD-0013', 'KING-0023', 'WAWO-0046'
- type VARCHAR FK→xref.xref_name (xref_type = 'activity_type') — Interaction kind. Allowed: Call | Email | Meeting. Drop internal-only activity types at mapping. Values (all): 'Call', 'Email', 'Meeting'
- subject VARCHAR nullable — What happened, condensed. Values (all): 'Covenant remediation discussion', 'FX and hedging review', 'Implementation kickoff — Positive Pay', 'Implementation kickoff — Sweep Account', 'Liquidity strategy session', 'Merchant services review', 'Pricing discussion', 'Quarterly relationship review'
- activity_date DATE — When it happened - THE field the relationship-neglect signal reads (90-day threshold). Values (e.g.): '2024-07-05', '2025-06-30', '2026-06-28'
- owner VARCHAR nullable — Who logged it. Values: personal data, no example values
- notes VARCHAR nullable — Details, truncated to a sentence or two. Values (all): 'Discussed the coming covenant test and a ninety-day cash-flow plan; client to deliver weekly liquidity reporting; tone constructive.', 'Kickoff with treasury and the implementation team; entitlements, file formats and testing timeline agreed.', 'Mapped cross-border supplier and receipts flows; client interested in forwards for committed purchases — send an indicative structure.', 'Reviewed card acceptance volumes and settlement timing; discussed interchange optimization and payables card on the spend side.', 'Reviewed excess operating balances and sweep options; treasurer wants yield without losing same-day access.', 'Reviewed operating account structure, service levels and near-term plans; client engaged and open to consolidating more activity with us.', 'Walked through fee schedule versus incumbent banks; client pushing on analysis fees — follow up with a sharper package before renewal.'

### service_requests
Domain: crm. Role: Client service tickets: one row per request, with product, category, priority, status, and opened and closed dates. Open means a status other than 'Closed'.
Rows: 83. Key: uen, request (composite).
- uen VARCHAR PK FK→accounts.uen — Client key. No synthetic ticket id in the canonical model. Values (e.g.): 'BUSH-0009', 'PEND-0033', 'WAWO-0046'
- request VARCHAR PK — What the client asked for / reported (ticket subject). Values (all): 'ACH batch rejected', 'Account analysis statement question', 'Add signing authority', 'Add user entitlement', 'Address / KYC information update', 'Analysis fee review request', 'Dispute on service charge', 'Lockbox implementation & onboarding', 'OLBB alert configuration', 'OLBB login issue', 'Payment trace request', 'Positive Pay implementation & onboarding', 'Reset security token', 'Sweep Account implementation & onboarding', 'Unable to initiate wire', 'Update authorized signers', 'Wire fee inquiry', 'e-Statement delivery setup'
- product VARCHAR FK→catalog.name (soft reference) nullable — Affected product. Values (all): 'ACH Origination', 'Analyzed Operating DDA', 'Commercial Demand Deposit Account', 'Lockbox', 'Online Banking for Business', 'Positive Pay', 'Sweep Account', 'Wire Transfer'
- close_date DATE nullable — Close date; empty while open. Values (e.g.): '2024-09-11', '2025-10-19', '2026-08-01'
- status VARCHAR — Ticket state. Allowed: Open | Closed (map in-flight workflow states to Open). The operational-friction signal counts Open rows (threshold: 2 or more). Values (all): 'Closed', 'In Progress', 'New'
- category VARCHAR nullable — Classification for triage. Values (all): 'Access', 'Account Maintenance', 'Fees', 'Online Banking', 'Payments'
- priority VARCHAR nullable — Urgency for triage. Values (all): '1 - Critical', '2 - High', '3 - Moderate', '4 - Low'
- opened_date DATE nullable — Open date, for aging. Values (e.g.): '2024-08-04', '2025-07-12', '2026-07-14'

### relationship
Domain: crm. Role: One row per client summarizing the relationship: bank revenue (nii, nir), deposits, credit exposure, product-depth flags, ROE and RAROC, and review dates.
Rows: 46. Key: uen.
- uen VARCHAR PK FK→accounts.uen — Client key; one row per client. Values (e.g.): 'ADUD-0013', 'KING-0023', 'WAWO-0046'
- nii DOUBLE FK→product_profitability.revenue_nii (reconciliation) nullable — Source-of-record: net interest income attributed to the relationship (FTP system), USD, current period. Never recompute from product data. Invariant: equals the per-period sum of profitability revenue_nii exactly. Total Revenue KPI = nii + nir (the BANK's revenue, not the client's). Values (e.g.): 49452.04, 430474.94, 2187597.38
- nir DOUBLE FK→product_profitability.revenue_fee (reconciliation) nullable — Source-of-record: non-interest (fee) revenue attributed to the relationship (FTP system), USD. Invariant: equals the per-period sum of profitability revenue_fee exactly. Values (e.g.): 16982.64, 173235.66, 2265331.46
- total_deposits DOUBLE FK→balances (consistency) nullable — Aggregated: client's total deposit balances with the bank, USD - consistent with the sum of latest positions in deposits/balances. Values (e.g.): 2165880.6, 8404926.64, 18521755.06
- roe DOUBLE nullable — Relationship return on equity, %. FTP system figure. Values (e.g.): 13.7, 36.5, 144.4
- coverage_team VARCHAR nullable — Semicolon list of 'Name (Role)' covering the client beyond the RM. Values: personal data, no example values
- strategic_priorities VARCHAR nullable — Semicolon list of the client's stated priorities; the advisor aligns recommendations to these. Values: free text
- ask VARCHAR nullable — One sentence: what the bank currently wants from the relationship. Anchors recommendations. Values: free text
- commercial BOOLEAN nullable — Derived engagement flag (Yes|No): any active product or material revenue in Commercial Banking. Part of the relationship-depth vector for whitespace analysis. Values (all): false, true
- tps BOOLEAN nullable — Derived engagement flag (Yes|No) for Treasury & Payment Solutions. Values (all): true
- capital_markets BOOLEAN nullable — Derived engagement flag (Yes|No) for Capital Markets. Values (all): false, true
- wealth BOOLEAN nullable — Derived engagement flag (Yes|No) for Wealth. Values (all): false
- retail BOOLEAN nullable — Derived engagement flag (Yes|No) for Retail. Values (all): false
- products_held VARCHAR FK→catalog.name (soft reference) nullable — Semicolon list of product names the client uses (catalog display names). Whitespace and payment-adoption analysis. Values: free text
- total_exposure DOUBLE FK→facilities.our_hold (consistency) nullable — Aggregated: total committed credit exposure to the client, USD (credit-system aggregate). Invariant: at least the sum of facility our_hold amounts. Values (e.g.): 0, 49439975.46, 132219872.31
- primary_operating_account BOOLEAN nullable — Derived (Yes|No): does the client's main operating account sit with us (balance/volume dominance in the deposit system)? The anchor of relationship stickiness. Values (all): false, true
- last_review_date DATE nullable — Last annual review. Values (e.g.): '2025-07-04', '2026-02-24', '2026-07-26'
- next_review_date DATE nullable — Next annual review; overdue becomes a watch item. Values (e.g.): '2026-07-05', '2027-02-16', '2027-07-16'
- wallet_share_pct DOUBLE nullable — Estimated share of the client's banking wallet, %. FTP/analytics estimate; empty if the bank does not compute one. Values (e.g.): 16.25, 41.91, 66.04
- raroc_pct DOUBLE nullable — Relationship risk-adjusted return on capital, %. FTP system figure; consistent with profitability totals. Values (e.g.): 13.7, 36.5, 144.4

### commitments
Domain: crm. Role: Promises made in client meetings: what was promised, by which side (client or bank), who owns it, when it is due and whether it is open.
Rows: 11. Key: commitment_id.
- commitment_id VARCHAR PK — Stable commitment key (COM-XXXX-nnn style). Values (all): 'COM-ALLO-001', 'COM-ALLO-002', 'COM-BARG-001', 'COM-BOST-001', 'COM-BUSH-001', 'COM-CANA-001', 'COM-FARI-001', 'COM-JONE-001', 'COM-MALO-001', 'COM-PEND-001', 'COM-SHIN-001'
- uen VARCHAR FK→accounts.uen — Client key. Values (all): 'ALLO-0001', 'BARG-0002', 'BOST-0007', 'BUSH-0009', 'CANA-0010', 'FARI-0016', 'JONE-0022', 'MALO-0027', 'PEND-0033', 'SHIN-0039'
- meeting_date DATE — Meeting where the commitment was made. Values (all): '2026-06-12', '2026-06-25', '2026-07-08', '2026-07-09', '2026-07-10', '2026-07-15', '2026-07-18', '2026-07-22', '2026-07-24', '2026-07-28', '2026-07-30'
- owner VARCHAR nullable — Who owns the commitment - a bank employee or a client role (e.g. 'Client CFO'). Values: personal data, no example values
- commitment VARCHAR — What was promised, one sentence. Values (all): 'Complete the renewal package and circulate approval conditions', 'Confirm onboarding completion and close the implementation checklist', 'Deliver a consolidated response and credit for the disputed wire and analysis fees', 'Deliver audited financial statements and the compliance certificate', 'Deliver the thirteen-week cash-flow forecast supporting the revolver request', 'Issue the covenant waiver term sheet with reporting conditions', 'Obtain board approval for the foreign-exchange risk policy', 'Provide a revised treasury fee package and implementation timeline', 'Provide merchant settlement and chargeback detail for the pricing review', 'Provide the twelve-month foreign-currency purchase forecast', 'Provide weekly liquidity reporting and a thirteen-week cash-flow forecast'
- due_date DATE nullable — When the commitment is due; overdue open commitments feed the unresolved-client-commitment signal. Values (all): '2026-06-30', '2026-07-10', '2026-07-20', '2026-07-24', '2026-07-25', '2026-07-28', '2026-07-31', '2026-08-05', '2026-08-07', '2026-08-14', '2026-08-15'
- status VARCHAR — Commitment state. Allowed: Open | Complete. Values (all): 'Complete', 'Open'
- client_or_bank VARCHAR nullable — Which side owes the commitment. Allowed: Client | Bank. Values (all): 'Bank', 'Client'
- priority VARCHAR nullable — Urgency for follow-up. Allowed: Critical | High | Medium | Low. Values (all): 'Critical', 'High', 'Low', 'Medium'
- last_updated DATE nullable — Last touch date of the record. Values (all): '2026-07-24', '2026-07-28', '2026-07-29', '2026-07-30', '2026-07-31', '2026-08-01'

### operational_service_cases
Domain: crm. Role: Operational service cases severe enough to be surfaced to the relationship manager, with product, category, priority, status and dates. Open means a status other than 'Closed'.
Rows: 6. Key: uen, request (composite).
- uen VARCHAR PK FK→accounts.uen — Client key. Like service_requests, rows carry no synthetic ticket id. Values (all): 'ALLO-0001', 'BUSH-0009', 'CANA-0010'
- request VARCHAR PK — What the client asked for / reported - operational cases severe enough to surface to the RM. Values (all): 'Analysis fee dispute escalation', 'Online banking entitlement remediation', 'Payment approval entitlement cleanup', 'Treasury portal entitlement remediation', 'Unable to release urgent supplier wire', 'Wire fee investigation and pricing credit'
- product VARCHAR FK→catalog.name (soft reference) nullable — Affected product. Values (all): 'Analyzed Operating DDA', 'Online Banking for Business', 'Wire Transfer'
- close_date VARCHAR (dictionary: date) nullable — Close date; empty while the case is open. Values: all NULL
- status VARCHAR — Case workflow state. Allowed: New | In Progress | Closed. Values (all): 'In Progress', 'New'
- category VARCHAR nullable — Classification for triage (e.g. Fees, Access, Payments). Values (all): 'Access', 'Fees', 'Payments'
- priority VARCHAR nullable — Ranked urgency label. Allowed: 1 - Critical | 2 - High | 3 - Medium | 4 - Low. Critical open cases feed the critical-service-issue signal. Values (all): '1 - Critical', '2 - High'
- opened_date DATE nullable — Open date, for aging. Values (all): '2026-07-12', '2026-07-17', '2026-07-20', '2026-07-21', '2026-07-22', '2026-07-24'

#### [Relationships]
- accounts.connected_uen → accounts.uen (each entry; may be outside the book)
- accounts.naics_code → benchmarks.naics_industry (via first 5 digits)
- contacts.uen → accounts.uen
- opportunities.uen → accounts.uen
- opportunities.product → catalog.name (soft reference)
- opportunities.stage → xref.xref_name (xref_type = 'opportunity_stage')
- activities.uen → accounts.uen
- activities.type → xref.xref_name (xref_type = 'activity_type')
- service_requests.uen → accounts.uen
- service_requests.product → catalog.name (soft reference)
- relationship.uen → accounts.uen
- relationship.nii → product_profitability.revenue_nii (reconciliation)
- relationship.nir → product_profitability.revenue_fee (reconciliation)
- relationship.total_deposits → balances (consistency)
- relationship.products_held → catalog.name (soft reference)
- relationship.total_exposure → facilities.our_hold (consistency)
- commitments.uen → accounts.uen
- operational_service_cases.uen → accounts.uen
- operational_service_cases.product → catalog.name (soft reference)

## Domain: lending
Credit exposure and its monitoring. facilities holds each credit line. utilization (24 month-ends), covenants, borrowing_base and credit_exceptions hang off a facility through facility_id. credit_metrics (one row per client) carries the obligor rating, spread financial ratios and risk-model outputs, and working_capital holds quarterly receivable, inventory and payable days for some clients. Facility amounts are USD; borrowing_base and working_capital carry a currency column.

### facilities
Domain: lending. Role: Credit facilities (revolving, term_loan, equipment_finance): commitment, outstanding balance, our_hold share, pricing, maturity and collateral, one row per facility.
Rows: 54. Key: facility_id.
- facility_id VARCHAR PK — Stable facility key; joins utilization and covenants. Values (e.g.): 'FAC000000000', 'FAC000000170', 'FAC000000330'
- uen VARCHAR FK→accounts.uen — Borrowing client. Values (e.g.): 'ADUD-0013', 'LIFE-0025', 'WAWO-0046'
- lender VARCHAR nullable — Lender of record - our bank, or the agent bank on syndications. Values (all): 'Regional Syndicate (agent)', 'This Bank (agent)', 'This Bank (sole lender)'
- facility_type VARCHAR FK→xref.xref_name (xref_type = 'facility_type') — Structural type. Allowed: revolving | term_loan | equipment_finance. LCs ride under revolving; equipment-secured term debt is equipment_finance (check collateral, not just the name). Values (all): 'equipment_finance', 'revolving', 'term_loan'
- commitment DOUBLE — Total committed amount, USD - the utilization denominator. Invariant: >= outstanding. Values (e.g.): 10277779.02, 35658774.9, 61981785.76
- outstanding DOUBLE nullable — Drawn balance, USD, as of the file date. Values (e.g.): 1859467.18, 16893303.61, 46476718.84
- interest_rate_basis VARCHAR nullable — Pricing basis as text (e.g. 'SOFR + 275bps'); compose from index + spread if not stored as text. Values (e.g.): 'FedFunds + 1.47%', 'Prime + 2.28%', 'SOFR + 3.69%'
- origination_date DATE nullable — Facility start. Values (e.g.): '2018-02-09', '2020-09-28', '2023-11-20'
- maturity_date DATE nullable — CURRENT maturity after amendments (not origination schedule). Refinancing-urgency signal fires within 12 months of it. Values (e.g.): '2026-08-07', '2028-09-01', '2031-05-07'
- purpose VARCHAR nullable — What the facility finances. Values (all): 'Acquisition financing', 'Equipment acquisition', 'General corporate purposes', 'Refinance existing debt', 'Working capital'
- collateral_type VARCHAR nullable — Security package type; equipment collateral informs facility_type. Values (all): 'Blanket Lien', 'Equipment', 'Inventory', 'Real Estate', 'Receivables'
- collateral_value DOUBLE nullable — Collateral value, USD. Values (e.g.): 5039364.87, 30125143.51, 75698245.93
- ltv_pct DOUBLE nullable — Loan-to-value, %. Calculated as outstanding/collateral_value if the source does not store it. Values (e.g.): 5.1, 46.59, 313.46
- our_hold DOUBLE nullable — Our bank's share of the commitment on syndications, USD - the true exposure. Equals commitment on bilateral deals. Values (e.g.): 5029895.53, 23010337.5, 56873225.28
- facility_risk_rating VARCHAR nullable — Facility-level rating, bank scale (1 = best). Values (all): 'Excellent', 'Good', 'Unstable', 'Very Good', 'Watch'
- spread_bps BIGINT nullable — Credit spread over the index, basis points. Values (e.g.): 147, 257, 369
- all_in_rate_pct DOUBLE nullable — All-in rate, %. Values (e.g.): 6.8, 8.4, 12.16
- unused_fee_bps BIGINT nullable — Unused/commitment fee, basis points. Values (e.g.): 15, 31, 50

### utilization
Domain: lending. Role: Monthly utilization history of each facility: drawn, available and utilization_pct at each month-end, 24 rows per facility.
Rows: 1296. Key: facility_id, as_of (composite).
- facility_id VARCHAR PK FK→facilities.facility_id — Facility key. Values (e.g.): 'FAC000000000', 'FAC000000170', 'FAC000000330'
- as_of DATE PK — Monthly snapshot date; 24 monthly rows per facility (history depth is required - trend signals need the series). Values (all): '2024-07-31', '2024-08-31', '2024-09-30', '2024-10-31', '2024-11-30', '2024-12-31', '2025-01-31', '2025-02-28', '2025-03-31', '2025-04-30', '2025-05-31', '2025-06-30', '2025-07-31', '2025-08-31', '2025-09-30', '2025-10-31', '2025-11-30', '2025-12-31', '2026-01-31', '2026-02-28', '2026-03-31', '2026-04-30', '2026-05-31', '2026-06-30'
- drawn DOUBLE — Drawn amount at the snapshot, USD (monthly balance snapshot). Values (e.g.): 1814093.78, 12012477.18, 38208005.13
- available DOUBLE — Calculated: commitment - drawn (adjusted for borrowing-base limits where the source tracks them), USD. Values (e.g.): 1901003.24, 20544001.49, 46717735.18
- utilization_pct DOUBLE — Calculated: drawn / commitment x 100. Invariant: equals drawn/(drawn+available) within rounding. Credit-squeeze signal reads this (revolver >= 80% with rising trend). Values (e.g.): 5, 36.67, 91.37

### covenants
Domain: lending. Role: Financial covenants on facilities: threshold, last tested value, the compliance verdict and the next test date, one row per facility and covenant.
Rows: 56. Key: facility_id, name (composite).
- facility_id VARCHAR PK FK→facilities.facility_id — Facility key. Values (e.g.): 'FAC000000000', 'FAC000000180', 'FAC000000330'
- name VARCHAR PK — Covenant name (e.g. 'Minimum DSCR', 'Minimum availability'). Unique per facility (composite key with facility_id). Values (all): 'Current Ratio', 'DSCR', 'Fixed Charge Coverage', 'Leverage'
- threshold VARCHAR nullable — Required level, human-readable text (units vary: ratio, USD, %). Values (all): '<= 3.5', '>= 1.15', '>= 1.2', '>= 1.25'
- current_value DOUBLE (dictionary: string) nullable — Most recently tested value, same unit as threshold. Values (e.g.): 0.96, 1.6, 3.55
- in_compliance BOOLEAN — The tested verdict (Yes|No) from the compliance workflow - prefer it over re-deriving. Any No fires the urgent covenant-breach signal. Values (all): false, true
- next_test_date DATE nullable — Next compliance test; becomes a watch item. Values (all): '2026-09-28'

### credit_metrics
Domain: lending. Role: One row per client of credit risk: obligor risk rating and prior rating, outlook, watch status, spread ratios (DSCR, leverage, current ratio) and PD and LGD.
Rows: 46. Key: uen.
- uen VARCHAR PK FK→accounts.uen — Client key; one row per client. Values (e.g.): 'ADUD-0013', 'KING-0023', 'WAWO-0046'
- risk_rating VARCHAR nullable — Obligor rating, bank scale (1 = best; string - scales can be alphanumeric). Gates product eligibility vs catalog.max_risk_rating. Values (all): 'Excellent', 'Good', 'Unstable', 'Very Good', 'Watch'
- prior_risk_rating VARCHAR nullable — Previous rating - migration direction matters as much as level. Values (all): 'Excellent', 'Good', 'Unstable', 'Very Good', 'Watch'
- risk_rating_as_of DATE nullable — Rating date. Values (all): '2026-06-15', '2026-06-30', '2026-07-01', '2026-07-15', '2026-07-20'
- annual_review_date DATE nullable — Next annual review; overdue is a watch item. Values (e.g.): '2026-07-05', '2027-02-16', '2027-07-16'
- trigger_exceptions VARCHAR nullable — Free-text exceptions/triggers noted by credit. Values (all): 'Advance rate above policy', 'Covenant holiday granted', 'Financial statements past due', 'Guarantor support waived'
- ebitda DOUBLE nullable — EBITDA, USD, from the spreading system. Benchmark comparison. Values (e.g.): 1735002.85, 22609012.36, 132130359.6
- profit_margin_pct DOUBLE nullable — Spread ratio: profit margin, %. Values (e.g.): 1.26, 5.26, 13.64
- debt_to_net_worth DOUBLE nullable — Spread ratio: leverage (lower is better). Values (e.g.): 0.5, 2.02, 4.24
- fixed_charge_coverage DOUBLE nullable — Spread ratio: fixed charge coverage. Values (e.g.): 0.87, 1.6, 3.8
- debt_service_coverage DOUBLE nullable — Spread ratio: DSCR - the busiest field: product-eligibility floors (catalog.min_dscr) and the benchmark table both read it. Values (e.g.): 1.04, 1.68, 4
- current_ratio DOUBLE nullable — Spread ratio: current ratio (working capital). Values (e.g.): 0.8, 1.57, 2.96
- pd_pct DOUBLE nullable — Probability of default, %, from bank risk models. Values (e.g.): 0.16, 1.49, 9.13
- lgd_pct DOUBLE nullable — Loss given default, %, from bank risk models. Values (e.g.): 20.24, 39.65, 69.29
- rating_outlook VARCHAR nullable — Credit's directional view. Allowed: Stable | Negative | Positive. Negative fires the risk-outlook watch signal. Values (all): 'Negative', 'Positive', 'Stable'
- watch_status VARCHAR nullable — Regulatory classification. Allowed: Pass | Watch | Special Mention | classified grades. Non-Pass sharpens risk framing (context, not the trigger). Values (all): 'None', 'Special Mention', 'Watch'

### borrowing_base
Domain: lending. Role: Borrowing-base certificates of asset-based revolvers: eligible receivables and inventory, advance rates and the remaining availability.
Rows: 4. Key: facility_id, as_of (composite).
- facility_id VARCHAR PK FK→facilities.facility_id — Facility key - the asset-based revolver the certificate covers. Values (all): 'FAC000000020', 'FAC000000080', 'FAC000000162', 'FAC000000190'
- uen VARCHAR FK→accounts.uen — Borrowing client. Values (all): 'ALLO-0001', 'BUSH-0009', 'SHIN-0039', 'WAWO-0046'
- as_of DATE PK — Certificate/snapshot date (composite key with facility_id). Values (all): '2026-07-31'
- eligible_ar BIGINT — Eligible accounts receivable in the borrowing base, USD. Values (all): 12000000, 30000000, 35000000, 50000000
- eligible_inventory BIGINT nullable — Eligible inventory in the borrowing base, USD. Values (all): 9000000, 14000000, 20000000, 30000000
- advance_rate_ar_pct BIGINT nullable — Advance rate applied to eligible AR, %. Values (all): 85
- advance_rate_inventory_pct BIGINT nullable — Advance rate applied to eligible inventory, %. Values (all): 50
- availability BIGINT — Excess availability after outstandings and reserves, USD. Thin availability vs commitment fires the borrowing-base-pressure signal. Values (all): 760000, 3800000, 12000000, 16700000
- currency VARCHAR nullable — Certificate currency. Allowed: USD | CAD. Values (all): 'USD'

### credit_exceptions
Domain: lending. Role: Credit and documentation exceptions (missing certificates and statements, waivers, covenant defaults) with due date, status, severity and cure plan.
Rows: 8. Key: exception_id.
- exception_id VARCHAR PK — Stable exception key (CE-XXXX-nnn style). Values (all): 'CE-ALLO-001', 'CE-BARG-001', 'CE-BUSH-001', 'CE-JONE-001', 'CE-JONE-002', 'CE-MALO-001', 'CE-SHIN-001', 'CE-WAWO-001'
- uen VARCHAR FK→accounts.uen — Client key. Values (all): 'ALLO-0001', 'BARG-0002', 'BUSH-0009', 'JONE-0022', 'MALO-0027', 'SHIN-0039', 'WAWO-0046'
- facility_id VARCHAR FK→facilities.facility_id nullable — Facility the exception attaches to; empty for relationship-level exceptions (e.g. a waiver spanning facilities). Values (all): 'FAC000000020', 'FAC000000080', 'FAC000000130', 'FAC000000162', 'FAC000000190'
- exception_type VARCHAR — Kind of documentation/credit exception (e.g. Financial Statements, Covenant Default, Borrowing Base Certificate, Insurance Certificate). Values (all): 'Borrowing Base Certificate', 'Compliance Certificate', 'Covenant Default', 'Covenant Waiver', 'Financial Statements', 'Insurance Certificate', 'Renewal Package'
- requirement VARCHAR — What must be delivered or executed to clear the exception. Values (all): 'Complete financial spreading collateral review and approval memorandum', 'Deliver audited statements and covenant compliance certificate', 'Deliver audited statements and updated projections', 'Deliver the July borrowing-base certificate and receivables aging', 'Deliver the quarterly covenant compliance certificate', 'Execute a covenant waiver and approve the liquidity remediation plan', 'Finalize waiver terms and enhanced reporting requirements', 'Renew property and casualty insurance evidence'
- due_date DATE nullable — When the item was/is due; past-due open exceptions feed the credit-document-exception signal. Values (all): '2026-06-30', '2026-07-01', '2026-07-08', '2026-07-15', '2026-07-20', '2026-07-25'
- received_date DATE nullable — When the item was received; empty while outstanding. Values (all): '2026-06-28', '2026-07-29'
- status VARCHAR — Exception state. Allowed: Open | Resolved | Closed. Values (all): 'Closed', 'Open', 'Resolved'
- severity VARCHAR nullable — Credit's severity grade. Allowed: Critical | High | Medium | Low. Values (all): 'Critical', 'High', 'Low', 'Medium'
- owner VARCHAR nullable — Bank-side owner chasing the exception. Values: personal data, no example values
- waiver_expiry DATE nullable — Expiry of any waiver granted; becomes a watch item as it approaches. Values (all): '2026-08-15', '2026-09-30'
- cure_plan VARCHAR nullable — Agreed remediation path, one sentence. Values (all): 'Certificate received and reviewed without exception', 'Client committed to provide the package before the next credit call', 'Daily coordination between the RM and credit team until approval', 'Escalated to the CFO and guarantor', 'Evidence accepted before expiration', 'Freeze discretionary advances until reporting is current', 'Monthly borrowing and backlog reporting until leverage returns to plan', 'Weekly liquidity reporting and no additional debt without approval'
- last_updated DATE nullable — Last touch date of the record. Values (all): '2026-06-28', '2026-07-28', '2026-07-29', '2026-07-30', '2026-07-31'

### working_capital
Domain: lending. Role: Quarterly working-capital metrics for some clients: DSO, DIO, DPO, cash conversion cycle, aged receivables and inventory, and net working capital.
Rows: 27. Key: uen, period_end (composite).
- uen VARCHAR PK FK→accounts.uen — Client key (composite key with period_end). Values (all): 'ALLO-0001', 'BARG-0002', 'BOST-0007', 'BUSH-0009', 'CANA-0010', 'FARI-0016', 'JONE-0022', 'MALO-0027', 'PEND-0033'
- period_end DATE PK — Quarter-end the metrics are measured at; multiple periods per client enable trend signals. Values (all): '2025-12-31', '2026-03-31', '2026-06-30'
- dso_days BIGINT nullable — Days sales outstanding - receivables collection speed. Values (all): 24, 25, 26, 37, 38, 39, 40, 41, 42, 43, 44, 45, 47, 48, 52, 54, 55, 58, 60, 65, 66, 72, 76
- dio_days BIGINT nullable — Days inventory outstanding. Values (all): 0, 4, 50, 54, 55, 59, 61, 64, 65, 66, 68, 69, 70, 72, 75, 76, 82, 84, 90, 116, 118, 120
- dpo_days BIGINT nullable — Days payables outstanding. Values (all): 15, 16, 30, 31, 32, 36, 37, 38, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 55, 56, 58
- cash_conversion_cycle_days BIGINT nullable — Cash conversion cycle = DSO + DIO - DPO. A lengthening cycle feeds the working-capital-deterioration signal. Values (all): 9, 10, 30, 38, 46, 54, 62, 63, 64, 66, 70, 71, 74, 80, 87, 88, 95, 101, 103, 105, 106, 110
- ar_over_90_pct DOUBLE nullable — Share of receivables aged over 90 days, %. Values (all): 2, 2.1, 2.2, 3.5, 3.8, 4, 4.2, 4.5, 5, 5.2, 5.4, 6, 7, 8, 8.5, 9, 10, 11, 13, 14, 14.5, 16, 23
- inventory_aging_pct DOUBLE nullable — Share of aged/slow-moving inventory, %; empty for service businesses with no inventory. Values (all): 7, 7.5, 8, 8.2, 9, 9.5, 10, 11, 12, 14, 16, 17, 18, 22
- net_working_capital BIGINT nullable — Net working capital, in the row's currency. Values (e.g.): 6000000, 32000000, 184000000
- currency VARCHAR nullable — Measurement currency. Allowed: USD | CAD. Values (all): 'CAD', 'USD'
- data_quality VARCHAR nullable — Spreading-data quality flag from the source (e.g. Complete). Values (all): 'Complete'

#### [Relationships]
- facilities.uen → accounts.uen
- facilities.facility_type → xref.xref_name (xref_type = 'facility_type')
- utilization.facility_id → facilities.facility_id
- covenants.facility_id → facilities.facility_id
- credit_metrics.uen → accounts.uen
- borrowing_base.facility_id → facilities.facility_id
- borrowing_base.uen → accounts.uen
- credit_exceptions.uen → accounts.uen
- credit_exceptions.facility_id → facilities.facility_id
- working_capital.uen → accounts.uen

## Domain: payments
How clients move money. payment_summaries is the pre-aggregated monthly view (client, rail, direction, month, currency; 24 months), transactions is the payment-level ledger beneath it, and counterparties lists the named parties and corridors. Direction is relative to the client: inbound is receivables, outbound is payables. Amounts are in each row's own currency; a summary's total_value is notional across the currencies in its bucket (currency is the modal one), so per-currency totals come from transactions.

### payment_summaries
Domain: payments. Role: Monthly payment totals per client, rail, direction and currency: transaction count, total value and gross rail fees.
Rows: 9072. Key: uen, rail, direction, period_start, period_end, currency (composite).
- uen VARCHAR PK FK→accounts.uen — Client key. Values (e.g.): 'ADUD-0013', 'KING-0023', 'WAWO-0046'
- rail VARCHAR PK FK→xref.xref_name (xref_type = 'payment_rail') — Payment rail. Allowed: ach | eft | wire | card | check | rtp. eft is Canada's domestic batch rail. Canonicalize via xref BEFORE aggregating so source spellings merge into one bucket. Values (all): 'ach', 'card', 'eft', 'rtp', 'wire'
- direction VARCHAR PK FK→xref.xref_name (xref_type = 'payment_direction') — Allowed: inbound (receivables) | outbound (payables), relative to the client. Values (all): 'inbound', 'outbound'
- period_start DATE PK — First day of the covered calendar month. 24 months of history per active rail x direction. Values (all): '2024-07-01', '2024-08-01', '2024-09-01', '2024-10-01', '2024-11-01', '2024-12-01', '2025-01-01', '2025-02-01', '2025-03-01', '2025-04-01', '2025-05-01', '2025-06-01', '2025-07-01', '2025-08-01', '2025-09-01', '2025-10-01', '2025-11-01', '2025-12-01', '2026-01-01', '2026-02-01', '2026-03-01', '2026-04-01', '2026-05-01', '2026-06-01'
- period_end DATE PK — Last day of the covered month. Values (all): '2024-07-31', '2024-08-31', '2024-09-30', '2024-10-31', '2024-11-30', '2024-12-31', '2025-01-31', '2025-02-28', '2025-03-31', '2025-04-30', '2025-05-31', '2025-06-30', '2025-07-31', '2025-08-31', '2025-09-30', '2025-10-31', '2025-11-30', '2025-12-31', '2026-01-31', '2026-02-28', '2026-03-31', '2026-04-30', '2026-05-31', '2026-06-30'
- transaction_count BIGINT FK→transactions (reconciliation) — Aggregated: COUNT of the matching transactions.csv rows. Zero-fill placeholder rows (count=0) exist for clients with no payments - exclude them from averages. Values (e.g.): 1, 19, 40
- total_value DOUBLE FK→transactions (reconciliation) — Aggregated: SUM(amount) of the matching transaction rows. Notional across currencies within the bucket - per-currency detail lives in transactions. Values (e.g.): 832.68, 864224.16, 56015095.01
- currency VARCHAR PK — The bucket's modal currency (ISO code). Values (all): 'CAD', 'CNY', 'EUR', 'JPY', 'USD'
- fee_revenue DOUBLE FK→transactions (reconciliation) nullable — Aggregated: SUM(fee) of the matching transaction rows - GROSS rail fees the client paid. The bank's attributed share lives in profitability; do not confuse the two. Values (e.g.): 0, 591.13, 47077.9

### transactions
Domain: payments. Role: The payment ledger: one row per payment, with rail, direction, value date, amount and currency, fee, counterparty and memo.
Rows: 30771. Key: transaction_id.
- transaction_id VARCHAR PK — Stable payment key. Values (e.g.): 'TXN-CANA-000001', 'TXN-RIGLO-000221', 'TXN-STHU-000757'
- uen VARCHAR FK→accounts.uen — Client key. Values (e.g.): 'ADUD-0013', 'KING-0023', 'WAWO-0046'
- rail VARCHAR FK→xref.xref_name (xref_type = 'payment_rail') — Allowed: ach | eft | wire | card | check | rtp (xref-translated). Values (all): 'ach', 'card', 'eft', 'rtp', 'wire'
- direction VARCHAR FK→xref.xref_name (xref_type = 'payment_direction') — Allowed: inbound | outbound. Values (all): 'inbound', 'outbound'
- value_date DATE — Settlement date. Values (e.g.): '2024-07-01', '2025-06-28', '2026-06-28'
- amount DOUBLE — Payment amount in ITS OWN currency (a Canadian client's domestic payments are CAD on the eft rail). Values (e.g.): 242.24, 420875.05, 56015095.01
- currency VARCHAR — ISO currency code of this payment (USD, CAD, corridor currencies). Values (all): 'CAD', 'CNY', 'EUR', 'JPY', 'USD'
- fee DOUBLE nullable — Rail fee this payment carried, same currency. Summary fee_revenue is the sum of these. Values (e.g.): 0, 226.13, 6099.03
- counterparty VARCHAR nullable — Other party's name, if known. Values (e.g.): 'Abbott-Freeman', 'Khan-Taylor', 'Zuniga, Thompson and Roberts'
- memo VARCHAR nullable — Payment reference text; scrub anything sensitive. Values (e.g.): 'Card settlement', 'Grower & vendor disbursement', 'Wholesale collections'

### counterparties
Domain: payments. Role: Named customers and suppliers a client pays or is paid by, with direction, rail, corridor country and currency, annual value and the counterparty's bank. The table is populated, although a column description calls it optional and undelivered.
Rows: 1340. Key: uen, counterparty, direction, rail (composite).
- uen VARCHAR PK FK→accounts.uen — Client key. OPTIONAL TABLE - not currently delivered; when it arrives it activates supplier-concentration and FX-exposure analysis. Never fabricated: absent file degrades to an empty list. Values (e.g.): 'ADUD-0013', 'KING-0023', 'WAWO-0046'
- counterparty VARCHAR PK — Named counterparty. Values (e.g.): 'Adud Trading Partner 2 Ltd.', 'Johnson-Graham', 'Zimmerman, Underwood and Tyler'
- role VARCHAR nullable — Counterparty role (supplier, customer, ...). Values (all): 'Customer', 'Supplier'
- direction VARCHAR PK FK→xref.xref_name (xref_type = 'payment_direction') — Allowed: inbound | outbound. Values (all): 'inbound', 'outbound'
- rail VARCHAR PK FK→xref.xref_name (xref_type = 'payment_rail') — Allowed: ach | eft | wire | card | check | rtp. Values (all): 'eft', 'wire'
- country VARCHAR nullable — Corridor country - FX-exposure input. Values (all): 'Canada', 'China', 'Germany', 'Japan', 'USA'
- currency VARCHAR nullable — Corridor currency (ISO code). Values (all): 'CAD', 'CNY', 'EUR', 'JPY', 'USD'
- annual_value DOUBLE nullable — Aggregated: annual value moved with this counterparty, USD. Values (e.g.): 30740.72, 706679.01, 381911994.42
- counterparty_bank VARCHAR nullable — Who the counterparty banks with - displacement intelligence. Values (all): 'Bank of Montreal', 'China Merchants Bank', 'Commerzbank', 'DZ Bank', 'Deutsche Bank', 'First Horizon Bank', 'ICBC', 'MUFG Bank', 'Regions Bank', 'Royal Bank of Canada', 'TD Canada Trust', 'Zions Bancorporation'
- note VARCHAR nullable — Context. Values: all NULL

#### [Relationships]
- payment_summaries.uen → accounts.uen
- payment_summaries.rail → xref.xref_name (xref_type = 'payment_rail')
- payment_summaries.direction → xref.xref_name (xref_type = 'payment_direction')
- payment_summaries.transaction_count → transactions (reconciliation)
- payment_summaries.total_value → transactions (reconciliation)
- payment_summaries.fee_revenue → transactions (reconciliation)
- transactions.uen → accounts.uen
- transactions.rail → xref.xref_name (xref_type = 'payment_rail')
- transactions.direction → xref.xref_name (xref_type = 'payment_direction')
- counterparties.uen → accounts.uen
- counterparties.direction → xref.xref_name (xref_type = 'payment_direction')
- counterparties.rail → xref.xref_name (xref_type = 'payment_rail')

## Domain: deposits
Monthly deposit balances per client, account type and currency, 24 month-ends. avg_balance is the average collected balance for the month, period_end_balance the month-end ledger balance, and idle_estimate the bank's estimate of cash not needed for operations.

### balances
Domain: deposits. Role: Monthly deposit balances per client, account type and currency: average and month-end balance, idle estimate, rate paid, earnings credit rate and sweep enrollment.
Rows: 2450. Key: uen, as_of, account_type, avg_balance, currency (composite).
- uen VARCHAR PK FK→accounts.uen — Client key. Values (e.g.): 'ALLO-0001', 'KLEI-0024', 'WAWO-0046'
- as_of DATE PK — Monthly snapshot date; 24 months per account type. Values (all): '2024-07-31', '2024-08-31', '2024-09-30', '2024-10-31', '2024-11-30', '2024-12-31', '2025-01-31', '2025-02-28', '2025-03-31', '2025-04-30', '2025-05-31', '2025-06-30', '2025-07-31', '2025-08-31', '2025-09-30', '2025-10-31', '2025-11-30', '2025-12-31', '2026-01-31', '2026-02-28', '2026-03-31', '2026-04-30', '2026-05-31', '2026-06-30'
- account_type VARCHAR PK FK→xref.xref_name (xref_type = 'deposit_account_type') — Allowed: Operating DDA | Operating DDA (CAD) | Operating DDA (Analyzed) | Money Market | Sweep | Collateral / Escrow. The idle-cash signal looks for types starting with 'Operating'. Values (all): 'Money Market', 'Operating DDA', 'Operating DDA (Analyzed)', 'Sweep'
- avg_balance DOUBLE PK — Average collected balance for the month (account-analysis average, not point-in-time ledger), in the account's currency. Values (e.g.): 418910.77, 3653755.55, 9859703.48
- idle_estimate DOUBLE nullable — Derived by the bank: portion of the balance not needed for operations - THE input to the idle-cash yield-gap pitch (idle x (SOFR - apy_pct)). Use the bank's target/excess-balance computation; leave empty rather than guessing. Values (e.g.): 0, 3965460.93, 9744464.11
- apy_pct DOUBLE nullable — Rate currently paid on the balance, %. Values (e.g.): 0, 3.7263, 4.7332
- currency VARCHAR PK — Account currency. Allowed: USD | CAD. Values (all): 'CAD', 'USD'
- period_end_balance DOUBLE nullable — Ledger balance at month-end, complementary to the average. Values (e.g.): 433770.62, 3786390.91, 9516251.89
- earnings_credit_rate_pct DOUBLE nullable — ECR offsetting fees on analyzed accounts, %. Sweep-vs-ECR analysis. Values (e.g.): 0, 0.3071, 2.7058
- sweep_enabled BOOLEAN nullable — Product enrollment flag (Yes|No). 'No' plus idle cash = the sweep pitch. Values (all): false, true

#### [Relationships]
- balances.uen → accounts.uen
- balances.account_type → xref.xref_name (xref_type = 'deposit_account_type')

## Domain: financials
Client financial statements as facts rather than ratios. company gives each client's name on filings and SEC identifier; financial_facts is a long layout of one row per client, concept (an XBRL US-GAAP tag), statement and fiscal year, so filter on concept and fiscal_year and compute margins and growth in SQL.

### company
Domain: financials. Role: One row per client with its name as it appears on filings and its SEC CIK (empty for private companies).
Rows: 46. Key: uen. Unique: cik.
- uen VARCHAR PK FK→accounts.uen — Client key; one row per client. Values (e.g.): 'ADUD-0013', 'KING-0023', 'WAWO-0046'
- company_name VARCHAR — Name as it appears on filings. Values (e.g.): 'A. Duda & Sons, Inc.', 'King Arthur Baking Company, Inc.', 'Wawona Packing Co., LLC'
- cik VARCHAR UK nullable — SEC Central Index Key linking to public filings (provenance URLs). Empty for private companies; unique when present. Values: all NULL

### financial_facts
Domain: financials. Role: Annual financial statement facts per client as concept and value rows (revenue, gross profit, operating and net income, cash, debt, inventory) by fiscal year.
Rows: 1323. Key: uen, concept, statement, value, unit, fiscal_year, period_end (composite).
- uen VARCHAR PK FK→accounts.uen — Client key. Values (e.g.): 'ADUD-0013', 'KING-0023', 'WAWO-0046'
- concept VARCHAR PK — Canonical fact name - XBRL US-GAAP tags ARE the vocabulary, for public AND private clients (spreading line items map onto the same tags). Allowed: Revenues | GrossProfit | OperatingIncomeLoss | NetIncomeLoss | CashAndCashEquivalentsAtCarryingValue | DebtLongtermAndShorttermCombinedAmount. Values (all): 'CashAndCashEquivalentsAtCarryingValue', 'DebtLongtermAndShorttermCombinedAmount', 'GrossProfit', 'InventoryNet', 'NetIncomeLoss', 'OperatingIncomeLoss', 'Revenues'
- label VARCHAR nullable — Human-readable label for display (e.g. 'Net sales'). Values (all): 'Cash and cash equivalents', 'Gross profit', 'Inventory', 'Net income', 'Operating income', 'Total debt', 'Total revenue'
- statement VARCHAR PK FK→xref.xref_name (xref_type = 'statement_type') — Allowed: income_statement | balance_sheet | cash_flow. Values (all): 'balance_sheet', 'income_statement'
- value DOUBLE PK — The figure, full dollars (not thousands). Facts only - the platform calculates all ratios, margins and YoY itself. Values (e.g.): -83405283.45, 27286469.77, 1231501097
- unit VARCHAR PK — Unit of the value (normally USD). Values (all): 'USD'
- fiscal_year VARCHAR PK — Derived from period_end using the CLIENT's actual fiscal calendar (never assume calendar years). Label like FY2026. Provide at least 3 fiscal years. Values (all): 'FY2023', 'FY2024', 'FY2025', 'FY2026'
- period_end DATE PK — Period end date (part of the unique grain with uen + concept). Values (all): '2023-12-31', '2024-12-31', '2025-12-31', '2026-12-31'
- is_annual BOOLEAN — Annual vs interim flag (Yes|No or 1|0). Values (all): true

#### [Relationships]
- company.uen → accounts.uen
- financial_facts.uen → accounts.uen
- financial_facts.statement → xref.xref_name (xref_type = 'statement_type')

## Domain: industry
Peer benchmarks by 5-digit NAICS industry rather than by client: one row per industry and indicator, covering financial ratios and payment-method adoption. A client reaches its rows through naics_code.

### benchmarks
Domain: industry. Role: Peer benchmarks per 5-digit NAICS industry and indicator: external macro value, internal cohort average, unit and direction of goodness.
Rows: 476. Key: naics_industry, indicator (composite).
- naics_industry BIGINT (dictionary: string) PK — The 5-digit NAICS industry this row set belongs to - the file's key (NOT client-keyed). Clients join via substr(crm.accounts.naics_code,1,5). Clients sharing an industry share one set. Values (e.g.): 11121, 31521, 72251
- sector VARCHAR nullable — Display label of the 2-digit NAICS sector (e.g. 'Retail trade'). Values (all): 'Accommodation and food services', 'Agriculture, forestry, fishing and hunting', 'Construction', 'Finance and insurance', 'Health care and social assistance', 'Manufacturing (food, beverage, tobacco, textile, apparel, leather)', 'Manufacturing (metals, machinery, electronics, transportation equipment, furniture, misc)', 'Manufacturing (wood, paper, printing, petroleum, chemicals, plastics, nonmetallic mineral)', 'Professional, scientific, and technical services', 'Retail trade', 'Wholesale trade'
- indicator VARCHAR PK — The benchmarked metric - matched LITERALLY by the platform. Allowed (financial): Revenue | EBITDA | Profit Margin | Gross Margin | Debt/Net Worth (leverage) | Fixed Charge Coverage | Debt Service Coverage | Current Ratio. Allowed (adoption): OLBB | ACH Usage | EFT Usage | Wires Usage | Corporate Card | Cheques. Values (all): 'ACH Usage', 'Cheques', 'Corporate Card', 'Current Ratio', 'Debt Service Coverage', 'Debt/Net Worth (leverage)', 'EBITDA', 'EFT Usage', 'Fixed Charge Coverage', 'Gross Margin', 'OLBB', 'Profit Margin', 'Revenue', 'Wires Usage'
- external_macro DOUBLE nullable — The published broad-market/sector (2-digit) value for the indicator, from the external vendor. Values (e.g.): 1, 71.36, 697699970.73
- cohort_avg DOUBLE nullable — The internal peer-cohort average at industry grain - the primary comparison value for the Above/Below verdict. BENCHMARK data only: the client's own values are calculated at read time from financials/credit/payments, never stored here. Values (e.g.): 0.97, 63.45, 682500000
- unit VARCHAR — Allowed: usd | pct | ratio. Non-USD client figures convert at spot before comparison. Values (all): 'pct', 'ratio', 'usd'
- higher_is_better BIGINT (dictionary: boolean) — Direction of goodness (1|0); drives the Above/Below verdict. Leverage and Cheques are the 0s. Values (all): 0, 1

#### [Relationships]
- none declared from the tables of this domain

## Domain: market
Market reference data from external sources, one dated observation per name: rates, fx pairs, commodities and the SOFR forward_curve by tenor. These are snapshots, not time series, and no market table links to a client.

### rates
Domain: market. Role: Latest market interest rates (SOFR, Fed Funds, Prime, Treasuries), one row per rate.
Rows: 10. Key: name.
- name VARCHAR PK — Canonical rate name, matched literally. Allowed: SOFR | Fed Funds Target (upper) | Prime Rate | Term SOFR 1M | Term SOFR 3M | Treasury 2Y | Treasury 5Y | Treasury 10Y | Treasury 30Y. SOFR feeds the idle-cash signal. Values (all): 'Fed Funds Target (upper)', 'Prime Rate', 'SOFR', 'Term SOFR 3M', 'Treasury 10Y', 'Treasury 1M', 'Treasury 1Y', 'Treasury 2Y', 'Treasury 3M', 'Treasury 5Y'
- value DOUBLE — The rate. Latest observation only - one row per rate. Values (all): 3.49, 3.58, 3.67, 3.69, 3.7, 3.81, 4.2, 6.86
- unit VARCHAR — pct. Values (all): 'pct', 'percent'
- as_of DATE — Observation date. Values (all): '2026-06-30'
- source VARCHAR nullable — Source attribution (e.g. FRED, CME). Values (all): 'Derived by this project from FRED SOFR (Federal Reserve Bank of New York) and FRED Treasury Constant Maturity series (Board of Governors of the Federal Reserve System, H.15) â€” see reference/market_rates.yaml', 'FRED (Board of Governors of the Federal Reserve System, H.15)', 'FRED (Federal Reserve Bank of New York)'

### fx
Domain: market. Role: Latest FX spot rates with year-to-date change, forward points and implied volatility, one row per currency pair.
Rows: 6. Key: pair.
- pair VARCHAR PK — Currency pair (e.g. USD/CAD, EUR/USD). USD/CAD also converts CAD client figures to USD for benchmark comparison. Values (all): 'EUR/USD', 'GBP/USD', 'USD/CAD', 'USD/CNY', 'USD/JPY', 'USD/MXN'
- rate DOUBLE — Spot rate. Values (all): 1.18, 1.35, 1.4, 7.02, 18.12, 158.14
- as_of DATE — Observation date. Values (all): '2026-06-30'
- source VARCHAR nullable — Source attribution. Values (all): 'FRED series EXCAUS (Board of Governors of the Federal Reserve System, H.10)', 'FRED series EXCHUS (Board of Governors of the Federal Reserve System, H.10)', 'FRED series EXJPUS (Board of Governors of the Federal Reserve System, H.10)', 'FRED series EXMXUS (Board of Governors of the Federal Reserve System, H.10)', 'FRED series EXUSEU (Board of Governors of the Federal Reserve System, H.10)', 'FRED series EXUSUK (Board of Governors of the Federal Reserve System, H.10)'
- ytd_change_pct DOUBLE nullable — Year-to-date move, %. Values (all): -11.78, -3.85, -2.88, 1.06, 9.32, 14.14
- fwd_3m_pct DOUBLE nullable — 3-month forward points as % of spot (signed). Hedging talk track. Values (all): -4.3, -1.5, -1, -0.15, 1.2, 5.5
- fwd_12m_pct DOUBLE nullable — 12-month forward points as % of spot (signed). Values (all): -4.5, -1.7, -1.2, -0.1, 1.35, 5
- implied_vol_3m_pct DOUBLE nullable — 3-month implied volatility, %. Values (all): 4, 6, 7, 7.5, 9.5, 13

### commodities
Domain: market. Role: Latest commodity prices with unit and quarter-over-quarter change, one row per commodity.
Rows: 8. Key: name.
- name VARCHAR PK — Canonical commodity name (e.g. WTI Crude, Diesel, Copper, Steel HRC) - chosen for the industries the book contains. Values (all): 'Copper', 'Corn', 'Diesel', 'Lumber', 'Natural Gas', 'Steel HRC', 'WTI Crude', 'Wheat'
- value DOUBLE — Price. Values (all): 3.6, 4.29, 4.38, 5.28, 5.4, 57.89, 546.62, 920.64
- unit VARCHAR — Price unit (e.g. USD/ton, USD/bbl). Values (all): 'USD/1000 board feet', 'USD/MMBtu', 'USD/bbl', 'USD/bu', 'USD/gal', 'USD/lb', 'USD/short ton'
- as_of DATE — Observation date. Values (all): '2026-06-30'
- source VARCHAR nullable — Source attribution (e.g. EIA). Values (all): 'CME Group (CBOT) ZC=F front-month futures via Yahoo Finance chart API', 'CME Group (CBOT) ZW=F front-month futures via Yahoo Finance chart API', 'CME Group HRC=F front-month futures via Yahoo Finance chart API; the CME HRC contract settles against CRU''s US Midwest hot-rolled coil index', 'CME Group LBR=F front-month futures via Yahoo Finance chart API', 'EIA monthly retail diesel survey via FRED series GASDESM', 'EIA via FRED series MHHNGSP', 'EIA via FRED series WTISPLC', 'IMF Primary Commodity Price System via FRED series PCOPPUSDM'
- qoq_change_pct DOUBLE nullable — Quarter-over-quarter move, %. Values (all): -0.02, 0, 0.02, 0.11, 0.22, 0.49, 0.53, 1.15
- note VARCHAR nullable — Context for the talk track. Values: all NULL

### forward_curve
Domain: market. Role: The SOFR forward curve: one forward rate per tenor, latest observation.
Rows: 7. Key: curve, tenor (composite).
- curve VARCHAR PK — Which curve. Allowed: SOFR. Values (all): 'SOFR'
- tenor VARCHAR PK — Point on the curve (e.g. 3M, 6M, 1Y, 2Y). Values (all): '10Y', '1Y', '2Y', '3M', '3Y', '5Y', '6M'
- rate DOUBLE — Forward rate, %. Rate-path context for refinancing timing. Values (all): 3.6, 3.66, 3.69, 3.77, 3.78, 3.81, 4.31
- as_of DATE — Observation date. Values (all): '2026-06-30'
- source VARCHAR nullable — Source attribution (e.g. CME). Values (all): 'Derived by this project from FRED SOFR (Federal Reserve Bank of New York) and FRED Treasury Constant Maturity series (Board of Governors of the Federal Reserve System, H.15) â€” see reference/market_rates.yaml'

#### [Relationships]
- none declared from the tables of this domain

## Domain: products
The bank's product catalog: one row per product with the eligibility thresholds a client must meet (minimum annual revenue, minimum DSCR, worst acceptable risk rating) and indicative pricing.

### catalog
Domain: products. Role: The bank's product catalog: product family, description, client benefit, eligibility thresholds, revenue band and typical pricing.
Rows: 13. Key: product_id. Unique: name.
- product_id VARCHAR PK — Stable product key. Values (all): 'CARD-COM', 'DDA-COM', 'FX-FWD', 'FX-SPOT', 'LEND-RCF', 'LEND-TERM', 'MERCH-ACQ', 'TRADE-LC', 'TRS-ACH', 'TRS-LBX', 'TRS-PPAY', 'TRS-RDC', 'TRS-SWEEP'
- name VARCHAR UK — Display name; 'name [family]' strings appear in whitespace lists. Unique. Values (all): 'ACH Origination', 'Commercial Card', 'Commercial Demand Deposit Account', 'Commercial Letters of Credit', 'FX Forward Contracts', 'FX Spot Trading', 'Investment Sweep', 'Lockbox', 'Merchant Card Acquiring', 'Positive Pay', 'Remote Deposit Capture', 'Revolving Credit Facility', 'Term Loan'
- family VARCHAR — Product family - maps to a relationship-depth flag: Deposits & Liquidity, Lending -> commercial; Payments & TPS, Trade Finance -> tps; FX & Rates, Capital Markets -> capital_markets; Wealth -> wealth. Values (all): 'Deposits & Liquidity', 'FX & Rates', 'Lending', 'Payments & TPS', 'Trade Finance'
- description VARCHAR nullable — What the product is, condensed. Values (all): 'Amortizing fixed-term loan for capital expenditure or expansion financing.', 'Bank-issued payment guarantee supporting import/export trade transactions.', 'Bank-operated remittance processing for high-volume check receivables.', 'Card payment acceptance and settlement for businesses collecting customer card payments.', 'Check fraud control matching presented checks against an issued-check file.', 'Committed line of credit for working-capital needs, drawn and repaid as needed.', 'Corporate purchasing and travel card program with rebate potential.', 'Desktop or mobile scanning to deposit checks without a branch visit.', 'Electronic origination of ACH credits and debits for payroll and vendor payments.', 'Forward-dated FX contracts to hedge future foreign-currency cash flows.', 'Operating checking account for commercial clients.', 'Overnight sweep of idle operating balances into a money market position.', 'Spot purchase and sale of foreign currency for trade settlement.'
- client_benefit VARCHAR nullable — What it does FOR THE CLIENT, one sentence, outcome-first. The advisor quotes this verbatim - write deliberately. Values: free text
- min_annual_revenue DOUBLE FK→accounts.annual_revenue (eligibility gate) nullable — Revenue floor for eligibility, USD. Empty = no floor. Missing client data never disqualifies. Values (all): 10000000, 50000000, 250000000
- min_dscr VARCHAR (dictionary: decimal) FK→credit_metrics.debt_service_coverage (eligibility gate) nullable — Minimum debt-service coverage for eligibility. Values: all NULL
- max_risk_rating BIGINT (dictionary: string) FK→credit_metrics.risk_rating (eligibility gate) nullable — Worst acceptable risk rating (bank scale, 1 = best). Values (all): 2, 4, 5
- eligibility_criteria VARCHAR nullable — Free-text criteria beyond the structured gates; shown to the advisor and RM. Values (all): 'Revenue tier 10-50M-250M-1B; risk grade Unstable or better; industries: ALL', 'Revenue tier 10-50M-250M-1B; risk grade Watch or better; industries: ALL', 'Revenue tier 250M-1B-250M-1B; risk grade Very Good or better; industries: 11,31,32,33,42', 'Revenue tier 50-250M-250M-1B; risk grade Unstable or better; industries: 11,31,32,33,42', 'Revenue tier 50-250M-250M-1B; risk grade Very Good or better; industries: 11,31,32,33,42,48', 'Revenue tier 50-250M-250M-1B; risk grade Very Good or better; industries: ALL', 'Revenue tier 50-250M-250M-1B; risk grade Watch or better; industries: 42,45,48'
- revenue_to_bank VARCHAR nullable — How the bank earns on it, as a band (e.g. '$10k-40k/yr'). Prioritization anchor. Values (all): '$10k-40k/yr in sweep balance spread', '$10k-50k/yr in spot FX spread', '$15k-60k/yr in lockbox item and maintenance fees', '$1k-3k/yr in service fees', '$20k-100k/yr in forward points and dealer spread', '$20k-120k/yr in discount rate and per-transaction fees', '$25k-150k/yr in issuance and amendment fees', '$2k-8k/yr in RDC service fees', '$50k-250k/yr in spread and commitment fee income', '$5k-20k/yr in ACH transaction and maintenance fees', '$5k-25k/yr in balances and analysis fees', '$5k-30k/yr net of interchange rebate', '$75k-400k/yr in spread income over the loan term'
- typical_pricing VARCHAR nullable — Indicative pricing (e.g. '$250/mo'). Talk-track anchor. Values (all): '$0.20/txn + $50/mo', '$0.65/item + $150/mo', '$25/mo + $0.15/item', '$40/mo + $0.10/item', '$75/mo', '1.5-2.5%/yr of face value', '1.8-2.6% + $0.10/txn', '15-40 bps spread', '20-50 bps spread', '25 bps', 'SOFR + 175-300 bps', 'SOFR + 200-350 bps; 25 bps commitment fee', 'Up to 1.25% rebate'

#### [Relationships]
- catalog.min_annual_revenue → accounts.annual_revenue (eligibility gate)
- catalog.min_dscr → credit_metrics.debt_service_coverage (eligibility gate)
- catalog.max_risk_rating → credit_metrics.risk_rating (eligibility gate)

## Domain: profitability
What each relationship earns the bank. product_profitability attributes net interest income and fee revenue, direct expense, risk cost and allocated capital to client, period, line of business and product, for a trailing-twelve-month period and the prior fiscal year. These are the bank's figures, not the client's.

### product_profitability
Domain: profitability. Role: Bank-attributed economics per client, period, line of business and product: net interest income, fee revenue, direct expense, risk cost and allocated capital.
Rows: 210. Key: uen, period, line_of_business, product (composite).
- uen VARCHAR PK FK→accounts.uen — Client key. Values (e.g.): 'ADUD-0013', 'KING-0023', 'WAWO-0046'
- period VARCHAR PK — Attribution period label. Allowed forms: TTM-YYYY-MM (current trailing window) | FYYYYY (prior year). Two periods per client enable YoY. Values (all): 'FY2025', 'TTM-2026-06'
- line_of_business VARCHAR PK FK→xref.xref_name (xref_type = 'line_of_business') — Allowed: Commercial Banking | Treasury & Payment Solutions | Capital Markets | Wealth ('TPS' is an accepted source variant). Values (all): 'Capital Markets', 'Commercial Banking', 'Treasury & Payment Solutions'
- product VARCHAR PK FK→catalog.name (soft reference) — Product the revenue is attributed to; align names with the catalog. Values (all): 'FX Services', 'Lending & Credit Facilities', 'Merchant & Card Services', 'Treasury Management'
- revenue_nii DOUBLE FK→relationship.nii (reconciliation) nullable — Source-of-record: attributed net interest income (FTP system), USD. Never derive from volumes. Invariant: per-period sum equals relationship.nii exactly. Values (e.g.): 0, 224745.58, 1962851.8
- revenue_fee DOUBLE FK→relationship.nir (reconciliation) nullable — Source-of-record: attributed fee revenue (FTP system), USD. Invariant: per-period sum equals relationship.nir exactly. Values (e.g.): 6574.67, 60812.79, 1329063.72
- expense_direct DOUBLE nullable — Direct cost allocated by the FTP system, USD. Values (e.g.): 12223.25, 85758.01, 969742.47
- risk_cost DOUBLE nullable — Expected-loss charge, USD. Values (e.g.): 303.99, 2565.59, 446410.01
- allocated_capital DOUBLE nullable — Capital allocated to the position, USD - the RAROC denominator. RAROC itself is calculated by the platform, never stored. Values (e.g.): 15199.47, 132134.51, 7745997.65

#### [Relationships]
- product_profitability.uen → accounts.uen
- product_profitability.line_of_business → xref.xref_name (xref_type = 'line_of_business')
- product_profitability.product → catalog.name (soft reference)
- product_profitability.revenue_nii → relationship.nii (reconciliation)
- product_profitability.revenue_fee → relationship.nir (reconciliation)

## Domain: operations
Housekeeping the coverage team acts on. source_readiness records whether each data domain is complete, reconciled and usable for a client; treasury_controls records control findings and implementation items on a client's treasury products.

### source_readiness
Domain: operations. Role: Data-readiness checks per client and data domain: freshness date, completeness, reconciliation and quality status, and the issue found.
Rows: 13. Key: readiness_id.
- readiness_id VARCHAR PK — Stable readiness-check key (RDY-nnnn style). Values (all): 'RDY-0001', 'RDY-0002', 'RDY-0003', 'RDY-0004', 'RDY-0005', 'RDY-0006', 'RDY-0007', 'RDY-0008', 'RDY-0009', 'RDY-0010', 'RDY-0011', 'RDY-0012', 'RDY-0013'
- uen VARCHAR FK→accounts.uen — Client key. Values (all): 'ALLO-0001', 'BARG-0002', 'BENC-0003', 'BOBS-0006', 'BOST-0007', 'CANA-0010', 'FARI-0016', 'JONE-0022', 'MCCO-0030', 'REMB-0036', 'VERM-0045', 'WAWO-0046'
- domain VARCHAR — Data domain the readiness check covers. Allowed: crm | lending | payments | deposits | financials | market | merchant. Values (all): 'crm', 'deposits', 'financials', 'lending', 'market', 'merchant', 'payments'
- as_of_date DATE — Freshness date of the domain's data for this client. Values (all): '2025-12-31', '2026-03-31', '2026-06-30', '2026-07-10', '2026-07-20', '2026-07-31', '2026-08-01', '2026-08-02'
- completeness_pct BIGINT nullable — How complete the domain's delivery is for this client, %. Values (all): 65, 70, 72, 83, 85, 88, 90, 92, 94, 96, 100
- reconciliation_status VARCHAR — Reconciliation verdict. Allowed: reconciled | partial | not_reconciled | exception. Values (all): 'exception', 'not_reconciled', 'partial', 'reconciled'
- quality_status VARCHAR — Usability verdict for advisory use. Allowed: ready | review | stale. Non-ready rows fire the source-readiness-issue signal. Values (all): 'ready', 'review', 'stale'
- issue VARCHAR nullable — What is wrong, one sentence; empty when the source is clean. Values (all): 'Annual review is in progress and the risk rating may change', 'Audited statements and covenant package are overdue', 'Borrowing-base certificate and collateral report are missing', 'CAD balances require CAD benchmark rates for yield analysis', 'Covenant waiver status is pending final approval', 'Project backlog and margin schedule are not current', 'Quarterly statements and aging schedules are overdue', 'Sponsor support and remediation milestones are not documented', 'Two foreign-currency corridors lack complete counterparty classification'
- owner VARCHAR nullable — Data steward responsible for the source. Values (all): 'CRM Data Steward', 'Credit Data Steward', 'Deposits Data Steward', 'Market Data Steward', 'Merchant Data Steward', 'Payments Data Steward'
- last_updated DATE nullable — Last check date. Values (all): '2026-08-02', '2026-08-03'

### treasury_controls
Domain: operations. Role: Treasury control findings and implementation items per client and product: type, status, severity, observation, owner and target date.
Rows: 12. Key: control_id.
- control_id VARCHAR PK — Stable control-finding key (CTL-nnnn style). Values (all): 'CTL-0001', 'CTL-0002', 'CTL-0003', 'CTL-0004', 'CTL-0005', 'CTL-0006', 'CTL-0007', 'CTL-0008', 'CTL-0009', 'CTL-0010', 'CTL-0011', 'CTL-0012'
- uen VARCHAR FK→accounts.uen — Client key. Values (all): 'ALLO-0001', 'BARG-0002', 'BENC-0003', 'BOST-0007', 'BUSH-0009', 'CANA-0010', 'FARI-0016', 'JONE-0022', 'MCCO-0030', 'REMB-0036', 'VERM-0045', 'WAWO-0046'
- product VARCHAR FK→catalog.name (soft reference) nullable — Treasury product the control finding concerns. Values (all): 'ACH Origination', 'Analyzed Operating DDA', 'Commercial Card', 'FX Services', 'Merchant Services', 'Online Banking for Business', 'Positive Pay', 'Wire Transfer'
- control_type VARCHAR — Kind of control (e.g. user_entitlement, security_token, positive_pay, dual_approval, implementation). Values (all): 'chargeback_trend', 'control_gap', 'dual_control', 'fee_exception', 'hedge_documentation', 'implementation', 'payment_access', 'program_adoption', 'return_rate', 'security_token', 'user_entitlement'
- status VARCHAR — Finding state. Allowed: open | pending_remediation | implementation_in_progress | not_enabled | underutilized | watch | ready. Non-ready rows feed the treasury-control-issue signal. Values (all): 'implementation_in_progress', 'not_enabled', 'open', 'pending_remediation', 'ready', 'underutilized', 'watch'
- severity VARCHAR nullable — Risk severity. Allowed: critical | high | medium | low. Values (all): 'critical', 'high', 'low', 'medium'
- observation VARCHAR nullable — What was observed, one sentence. Values (all): 'ACH return rate exceeded the client''s recent baseline and coincides with liquidity stress', 'Administrator access remains active for a departed controller while the relationship is on Watch', 'Chargeback rate increased for card-not-present transactions during the last two months', 'Client reported an inability to initiate wires during a supplier payment window', 'Forecast and authorized-signer documentation are complete for an indicative forward discussion', 'Issued-check file testing passed format validation but exception-workflow training is incomplete', 'One payment workflow permits the same user to create and release wires below the secondary threshold', 'Only a small share of eligible supplier spend is routed through the commercial-card program', 'Positive Pay is not enabled despite meaningful check issuance and a recent altered-check inquiry', 'Repeated analysis-fee disputes remain unresolved and are blocking balance-consolidation discussions', 'Two critical token-reset requests remain unresolved and one user cannot approve payments', 'Two former employees remain on the wire entitlement review pending client confirmation'
- detected_date DATE nullable — When the finding was detected. Values (all): '2026-07-15', '2026-07-18', '2026-07-19', '2026-07-20', '2026-07-22', '2026-07-24', '2026-07-26', '2026-07-28', '2026-07-29', '2026-07-30', '2026-07-31'
- owner VARCHAR nullable — Bank team that owns remediation. Values (all): 'Capital Markets', 'Card Sales', 'Digital Support', 'Merchant Risk', 'Payments Operations', 'TPS Implementation', 'TPS Risk', 'TPS Sales'
- target_date DATE nullable — Target remediation date; overdue becomes a watch item. Values (all): '2026-08-04', '2026-08-07', '2026-08-08', '2026-08-09', '2026-08-10', '2026-08-12', '2026-08-16', '2026-08-20', '2026-08-30', '2026-09-01'
- last_updated DATE nullable — Last touch date of the record. Values (all): '2026-08-01', '2026-08-02', '2026-08-03'

#### [Relationships]
- source_readiness.uen → accounts.uen
- treasury_controls.uen → accounts.uen
- treasury_controls.product → catalog.name (soft reference)

## Domain: reference
The vocabulary map. xref lists, for each vocabulary (xref_type), the canonical names and the alternative spellings that map to them. Values in the data tables are already canonical, so queries rarely need it; use it to translate a user's wording or a risk-rating number.

### xref
Domain: reference. Role: The vocabulary map: canonical names (xref_name) and accepted spellings (xref_value) per vocabulary (xref_type).
Rows: 113. Key: xref_id.
- xref_id VARCHAR PK — Row key (XR-0001 style). Values (e.g.): 'XR-0001', 'XR-0057', 'XR-0113'
- xref_type VARCHAR — Which vocabulary the row belongs to. Allowed: payment_rail | payment_direction | facility_type | opportunity_stage | statement_type | source_type | deposit_account_type | activity_type | nps_band | line_of_business | signal_severity. Composite unique with xref_value. Values (all): 'activity_type', 'client_segment', 'deposit_account_type', 'facility_type', 'line_of_business', 'nps_band', 'opportunity_stage', 'payment_direction', 'payment_rail', 'risk_rating', 'signal_severity', 'source_type', 'statement_type'
- xref_name VARCHAR — The canonical value within the vocabulary. Adding a NEW canonical name is a deliberate model change (code and reference move together). Values (e.g.): 'Call', 'Treasury & Payment Solutions', 'wire'
- xref_value VARCHAR — An accepted incoming spelling that maps to xref_name (canonical rows repeat the name). Matching is case-insensitive; every source spelling encountered during mapping is recorded here. Composite unique with xref_type. Values (e.g.): '1', 'P&L', 'wire'
- xref_description VARCHAR nullable — Business meaning of the canonical value (populated on canonical rows). Values: free text

#### [Relationships]
- none declared from the tables of this domain
