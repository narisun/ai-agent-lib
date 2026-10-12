-- description: Reviewed deposit history evidence; dated rows are bounded by the requested dates.
-- param uen: string
-- param start_date: date
-- param end_date: date
-- param row_offset: integer
-- max_rows: 50
-- classification: confidential
SELECT t."uen" AS "uen",
       t."as_of" AS "as_of",
       t."account_type" AS "account_type",
       t."avg_balance" AS "avg_balance",
       t."idle_estimate" AS "idle_estimate",
       t."apy_pct" AS "apy_pct",
       t."currency" AS "currency",
       t."period_end_balance" AS "period_end_balance",
       t."earnings_credit_rate_pct" AS "earnings_credit_rate_pct",
       t."sweep_enabled" AS "sweep_enabled"
FROM "balances" t 
WHERE t.uen = :uen AND t.as_of BETWEEN :start_date AND :end_date
ORDER BY as_of DESC, account_type, currency
OFFSET :row_offset
