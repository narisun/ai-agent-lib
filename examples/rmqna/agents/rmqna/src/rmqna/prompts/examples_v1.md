Worked examples. Each shows the question, the SQL that was run, and the answer an RM gets. The
SQL below was run against this data and is accepted by the server; the figures are what it
returned. Copy the habits, not the wording: write the SQL the new question needs, and read the
schema document for the columns.

### Example 1 — Who is in breach of a covenant?
Question: Which clients have a covenant in breach?

A breach is a covenants row with in_compliance = false (a boolean), used as stored. Covenants
reach their client through facilities.

```sql
SELECT a.name AS client, f.facility_id, c.name AS covenant, c.threshold, c.current_value
FROM covenants c
JOIN facilities f ON f.facility_id = c.facility_id
JOIN accounts a ON a.uen = f.uen
WHERE c.in_compliance = false
ORDER BY a.name, f.facility_id, c.name
LIMIT 20
```

Answer:
> Three clients have a covenant in breach, four covenants in all:
>
> | Client | Facility | Covenant | Required | Tested |
> | --- | --- | --- | --- | --- |
> | Jones Dairy Farm | FAC000000130 | DSCR | >= 1.25 | 1.08 |
> | Jones Dairy Farm | FAC000000130 | Fixed Charge Coverage | >= 1.15 | 0.96 |
> | Life is Good, Inc. | FAC000000150 | Current Ratio | >= 1.2 | 1.19 |
> | McCownGordon Construction, LLC | FAC000000140 | Leverage | <= 3.5 | 3.55 |
>
> Basis: covenants joined to facilities and accounts; in_compliance = false, the compliance
> workflow's own verdict. As of the 2026-06-30 snapshot. I can show the SQL.

### Example 2 — Who is close to the limit on their credit lines?
Question: Which clients are using more than 80% of their credit lines?

"Current" is the latest utilization row of each facility. A client's utilization is its summed
drawn amount over its summed commitment, never an average of the facility percentages.

```sql
WITH latest AS (
  SELECT facility_id, drawn
  FROM utilization
  QUALIFY as_of = max(as_of) OVER (PARTITION BY facility_id)
)
SELECT a.name AS client,
       ROUND(SUM(l.drawn), 0) AS drawn,
       ROUND(SUM(f.commitment), 0) AS committed,
       ROUND(100 * SUM(l.drawn) / SUM(f.commitment), 1) AS utilization_pct
FROM latest l
JOIN facilities f ON f.facility_id = l.facility_id
JOIN accounts a ON a.uen = f.uen
GROUP BY a.name
HAVING SUM(l.drawn) / SUM(f.commitment) > 0.8
ORDER BY utilization_pct DESC
LIMIT 20
```

Answer:
> Three clients are above 80%:
>
> | Client | Drawn | Committed | Utilization |
> | --- | --- | --- | --- |
> | Wawona Packing Co., LLC | $13.9M | $15.8M | 88.0% |
> | Life is Good, Inc. | $16.0M | $18.4M | 86.9% |
> | Blount Fine Foods Corp. | $71.1M | $81.9M | 86.8% |
>
> Basis: the latest utilization row per facility (2026-06-30) over facilities.commitment, summed
> per client across all its facilities. USD. I can show the SQL.

### Example 3 — A date window: movement over a quarter
Question: Which clients' utilization rose the most last quarter?

"Last quarter" is the quarter that ended on the snapshot date: compare the month-ends
2026-03-31 and 2026-06-30. FILTER puts both dates in one pass, one column each.

```sql
WITH by_client AS (
  SELECT a.name AS client,
         SUM(u.drawn) FILTER (WHERE u.as_of = DATE '2026-03-31')
           / SUM(f.commitment) FILTER (WHERE u.as_of = DATE '2026-03-31') AS util_start,
         SUM(u.drawn) FILTER (WHERE u.as_of = DATE '2026-06-30')
           / SUM(f.commitment) FILTER (WHERE u.as_of = DATE '2026-06-30') AS util_end
  FROM utilization u
  JOIN facilities f ON f.facility_id = u.facility_id
  JOIN accounts a ON a.uen = f.uen
  WHERE u.as_of IN (DATE '2026-03-31', DATE '2026-06-30')
  GROUP BY a.name
)
SELECT client,
       ROUND(100 * util_start, 1) AS utilization_pct_2026_03_31,
       ROUND(100 * util_end, 1) AS utilization_pct_2026_06_30,
       ROUND(100 * (util_end - util_start), 1) AS change_pts
FROM by_client
WHERE util_end > util_start
ORDER BY change_pts DESC
LIMIT 10
```

Answer:
> Wawona Packing Co., LLC rose the most, from 75.4% at 2026-03-31 to 88.0% at 2026-06-30
> (+12.6 points), then Shinola/Detroit, LLC (60.5% to 72.0%, +11.5) and Freshii Inc. (50.8% to
> 62.0%, +11.2). The other seven rose by between 8.0 and 1.5 points.
>
> Basis: utilization and facilities, drawn over commitment summed per client; "last quarter" is
> the quarter ending 2026-06-30, so I compared 2026-03-31 with 2026-06-30. Only clients whose
> utilization rose are listed, top 10. I can show the SQL.

### Example 4 — A metric with a definition, over a forward window
Question: What is our weighted pipeline for the next 90 days, by product?

Weighted pipeline counts open stages only, as amount * probability / 100. "The next 90 days" is
measured from the snapshot date, 2026-06-30, through 2026-09-28.

```sql
SELECT product,
       COUNT(*) AS open_deals,
       ROUND(SUM(amount), 0) AS pipeline_usd,
       ROUND(SUM(amount * probability / 100), 0) AS weighted_pipeline_usd
FROM opportunities
WHERE stage IN ('Prospecting', 'Qualification', 'Proposal', 'Negotiation')
  AND close_date BETWEEN DATE '2026-06-30' AND DATE '2026-06-30' + INTERVAL 90 DAY
GROUP BY product
ORDER BY weighted_pipeline_usd DESC
LIMIT 20
```

Answer:
> Weighted pipeline expected to close in the next 90 days is $1.15M across three products
> and six open deals:
>
> | Product | Open deals | Pipeline | Weighted |
> | --- | --- | --- | --- |
> | Revolving Credit | 1 | $4.86M | $0.78M |
> | Lockbox | 3 | $0.48M | $0.19M |
> | Sweep Account | 2 | $0.42M | $0.18M |
>
> Basis: opportunities in Prospecting, Qualification, Proposal or Negotiation with an expected
> close date from 2026-06-30 to 2026-09-28; weighted = amount x probability / 100. Closed Won
> and Closed Lost deals are excluded. USD. I can show the SQL.

### Example 5 — Two many-row tables: aggregate each before joining
Question: Which clients have more than $5M of open pipeline, and what is our exposure to each?

Opportunities and facilities both have several rows per client. Joined directly they would
multiply each other, so each is reduced to one row per client first. A client with no facility
has no exposure row, so the join to it is a LEFT JOIN.

```sql
WITH pipeline AS (
  SELECT uen, SUM(amount) AS open_pipeline
  FROM opportunities
  WHERE stage IN ('Prospecting', 'Qualification', 'Proposal', 'Negotiation')
  GROUP BY uen
),
exposure AS (
  SELECT uen, SUM(our_hold) AS exposure
  FROM facilities
  GROUP BY uen
)
SELECT a.name AS client, ROUND(p.open_pipeline, 0) AS open_pipeline, ROUND(e.exposure, 0) AS exposure
FROM pipeline p
JOIN accounts a ON a.uen = p.uen
LEFT JOIN exposure e ON e.uen = p.uen
WHERE p.open_pipeline > 5000000
ORDER BY p.open_pipeline DESC
LIMIT 20
```

Answer:
> Six clients have more than $5M of open pipeline:
>
> | Client | Open pipeline | Exposure |
> | --- | --- | --- |
> | D.G. Yuengling & Son, Inc. | $44.9M | $46.8M |
> | Wawona Packing Co., LLC | $38.0M | $13.1M |
> | Igloo Products Corp. | $30.0M | $39.2M |
> | Malouf Companies, Inc. | $29.5M | none |
> | Vermont Teddy Bear Company | $16.1M | none |
> | The Vermont Country Store, Inc. | $5.0M | none |
>
> Basis: open pipeline is the unweighted amount of deals in the four open stages; exposure is
> our share of committed credit, SUM(facilities.our_hold). "None" means the client has no
> facility with us. USD, as of 2026-06-30. I can show the SQL.

### Example 6 — A name the RM typed
Question: What deals does Wawona have open?

Turn the typed name into the stored value before filtering on it; never guess a LIKE pattern.

`search_column_values(table="accounts", column="name", search="wawona")` returns `{"values": ["Wawona Packing Co., LLC"], "truncated": false}`.

One match, so filter on that exact value:

```sql
SELECT o.name AS deal, o.product, o.stage, ROUND(o.amount, 0) AS amount, o.probability, o.close_date
FROM opportunities o
JOIN accounts a ON a.uen = o.uen
WHERE a.name = 'Wawona Packing Co., LLC'
  AND o.stage IN ('Prospecting', 'Qualification', 'Proposal', 'Negotiation')
ORDER BY o.close_date
LIMIT 20
```

Answer:
> Wawona Packing Co., LLC has one open deal: the Revolving Credit Facility renewal for working
> capital, in Proposal, $38.0M at 57% (about $21.7M weighted), expected to close 2026-11-28.
>
> Basis: opportunities for Wawona Packing Co., LLC in an open stage. Its two Closed Won deals
> are not open and are left out. USD, as of 2026-06-30. I can show the SQL.

### Example 7 — Risk ratings are words: map them through xref
Question: Which clients were downgraded, and from what to what?

A rating is a word; xref gives it a number, 1 best. xref keeps several rows per word, so keep
only the number row with TRY_CAST(xref_value AS INT) IS NOT NULL, once, in a CTE that both the
current and the prior rating join to. A downgrade is a higher number than before.

```sql
WITH rating AS (
  SELECT xref_name AS label, TRY_CAST(xref_value AS INT) AS number
  FROM xref
  WHERE xref_type = 'risk_rating' AND TRY_CAST(xref_value AS INT) IS NOT NULL
)
SELECT a.name AS client,
       m.prior_risk_rating AS from_rating, prev.number AS from_number,
       m.risk_rating AS to_rating, cur.number AS to_number
FROM credit_metrics m
JOIN accounts a ON a.uen = m.uen
JOIN rating cur ON cur.label = m.risk_rating
JOIN rating prev ON prev.label = m.prior_risk_rating
WHERE cur.number > prev.number
ORDER BY cur.number - prev.number DESC, a.name
LIMIT 20
```

Answer:
> Six clients were downgraded. Freshii Inc. (Excellent, 1, to Good, 3) and Shinola/Detroit, LLC
> (Very Good, 2, to Watch, 4) fell furthest, two steps each. Rembrandt Foods, LLC, Sebastiani
> Vineyards & Winery and Wawona Packing Co., LLC each went from Good (3) to Watch (4), and
> Vermont Teddy Bear Company from Watch (4) to Unstable (5).
>
> Basis: credit_metrics risk_rating against prior_risk_rating, both mapped to the bank's 1-5 scale
> through xref (1 is best). Ratings as recorded at 2026-06-30. I can show the SQL.

### Example 8 — Money in several currencies is never added
Question: How much did clients send out by wire over the last 12 months?

Payments carry their own currency, so total per currency. "The last 12 months" runs to the
snapshot date; the window is open at the start so exactly twelve months are counted.

```sql
SELECT currency, COUNT(*) AS payments, ROUND(SUM(amount), 0) AS total_value
FROM transactions
WHERE rail = 'wire' AND direction = 'outbound'
  AND value_date > DATE '2026-06-30' - INTERVAL 12 MONTH
  AND value_date <= DATE '2026-06-30'
GROUP BY currency
ORDER BY total_value DESC
LIMIT 20
```

Answer:
> Outbound wires from 2025-07-01 to 2026-06-30, by currency:
>
> | Currency | Payments | Value |
> | --- | --- | --- |
> | USD | 1,252 | 622.6M |
> | CAD | 1,363 | 483.7M |
> | CNY | 240 | 437.6M |
> | JPY | 12 | 381.9M |
> | EUR | 214 | 90.9M |
>
> Basis: transactions with rail = 'wire' and direction = 'outbound' (from the client's side, so
> payables), value dates 2025-07-01 to 2026-06-30. Each total is in its own currency; I have not
> converted or added them. I can show the SQL.

### Example 9 — Against the industry: a calculated join
Question: Which clients have a debt service coverage ratio below their industry's peer average?

Benchmarks are keyed by the 5-digit NAICS industry; a client's code has 6 digits and loads as a
number, so divide by 10 with integer division. Higher DSCR is better.

```sql
SELECT a.name AS client, c.debt_service_coverage AS client_dscr, b.cohort_avg AS peer_dscr
FROM accounts a
JOIN credit_metrics c ON c.uen = a.uen
JOIN benchmarks b ON b.naics_industry = a.naics_code // 10 AND b.indicator = 'Debt Service Coverage'
WHERE c.debt_service_coverage < b.cohort_avg
ORDER BY c.debt_service_coverage - b.cohort_avg
LIMIT 20
```

Answer:
> 14 clients are below their industry's peer average DSCR. The widest gaps are McCownGordon
> Construction, LLC (1.19 against 1.87), Barge Design Solutions, Inc. (1.31 against 1.97) and
> Vermont Teddy Bear Company (1.08 against 1.74). I can list all 14.
>
> Basis: credit_metrics.debt_service_coverage against benchmarks.cohort_avg for the indicator
> 'Debt Service Coverage' in the client's NAICS industry. Clients with no NAICS code have no
> benchmark and are not counted. As of 2026-06-30. I can show the SQL.

### Example 10 — The server refuses a statement: repair it from the message
Question: Which are our five largest facilities by commitment, and who borrows them?

First attempt. The column name was guessed:

```sql
SELECT a.name AS client, f.facility_id, ROUND(f.committed_amount, 0) AS commitment,
       ROUND(f.our_hold, 0) AS our_hold
FROM facilities f
JOIN accounts a ON a.uen = f.uen
ORDER BY f.committed_amount DESC
LIMIT 5
```

`execute_sql` returns `{"error_kind": "execution_error", "message": "Binder Error: Table \"f\" does not have a column named \"committed_amount\"\n\nCandidate bindings: : \"commitment\"\n\nLINE 1: SELECT a.name AS client, f.facility_id, ROUND(f.committed_amount, 0) AS commitment, ROUND(f.our_hold,...\n                                                      ^"}`.

That is a normal result, not a failure of the tool. The message names the bad column and the
candidate. This is repair 1 of at most 2: change only what the message points at.

```sql
SELECT a.name AS client, f.facility_id, ROUND(f.commitment, 0) AS commitment,
       ROUND(f.our_hold, 0) AS our_hold
FROM facilities f
JOIN accounts a ON a.uen = f.uen
ORDER BY f.commitment DESC
LIMIT 5
```

Answer:
> The five largest facilities by commitment:
>
> | Client | Facility | Commitment | Our hold |
> | --- | --- | --- | --- |
> | Faribault Woolen Mill Co. | FAC000000120 | $62.0M | $32.6M |
> | All-One-God-Faith, Inc. (Dr. Bronner's) | FAC000000161 | $60.4M | $25.1M |
> | St-Hubert Group Inc. | FAC000000270 | $60.3M | $27.3M |
> | Simms Fishing Products LLC | FAC000000010 | $59.5M | $44.0M |
> | All-One-God-Faith, Inc. (Dr. Bronner's) | FAC000000162 | $58.6M | $51.7M |
>
> Basis: facilities joined to accounts, ordered by commitment (the whole facility; "our hold" is
> our share). USD, as of 2026-06-30. I can show the SQL.

### Example 11 — Nothing came back: check once, then say why
Question: What is King Arthur's cash conversion cycle?

`search_column_values(table="accounts", column="name", search="king arthur")` returns `{"values": ["King Arthur Baking Company, Inc."], "truncated": false}`.

The cycle is the client's row at its latest period_end:

```sql
SELECT w.period_end, w.cash_conversion_cycle_days
FROM working_capital w
JOIN accounts a ON a.uen = w.uen
WHERE a.name = 'King Arthur Baking Company, Inc.'
QUALIFY w.period_end = max(w.period_end) OVER (PARTITION BY w.uen)
```

It returns `row_count` 0. An empty result is not an answer. Re-examine once: is the filter too
narrow, or is the data not there? One query checks the client, and how many clients the table
covers at all:

```sql
SELECT (SELECT COUNT(*) FROM working_capital w JOIN accounts a ON a.uen = w.uen
        WHERE a.name = 'King Arthur Baking Company, Inc.') AS rows_for_client,
       (SELECT COUNT(DISTINCT uen) FROM working_capital) AS clients_with_rows,
       (SELECT COUNT(*) FROM accounts) AS clients
```

It returns one row: 0, 9, 46. The data is not there, so say so and name what was checked.

Answer:
> There is no cash conversion cycle to report for King Arthur Baking Company, Inc.: the
> working-capital table has no rows for it at any quarter-end. It covers only 9 of the 46 clients.
>
> Basis: working_capital for the client named exactly 'King Arthur Baking Company, Inc.', latest
> period_end per client, then all of its rows. I can name the nine clients that do have a cycle,
> or show the SQL.

### Example 12 — Two answers that run no SQL
Question A: How many open service tickets does Vermont have?

`search_column_values(table="accounts", column="name", search="vermont")` returns `{"values": ["The Vermont Country Store, Inc.", "Vermont Teddy Bear Company"], "truncated": false}`.

Two clients match. Ask one multiple-choice question and stop; run nothing until it is answered.

Answer:
> Two clients match "Vermont". Which do you mean?
> A. The Vermont Country Store, Inc.
> B. Vermont Teddy Bear Company
> C. Both, side by side

Question B: Set the probability on Wawona's proposal to 90%.

The data is read-only, so this is declined and the nearest thing that can be done is offered.
No tool is called.

Answer:
> I can't change data. I only read it, so the probability on Wawona's proposal stays as it is in
> the CRM. I can show you its current probability and weighted amount, or work out the weighted
> pipeline if it were 90%. Want either?
