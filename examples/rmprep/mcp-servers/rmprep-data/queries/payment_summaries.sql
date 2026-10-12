-- description: Reviewed payment summaries evidence; dated rows are bounded by the requested dates.
-- param uen: string
-- param start_date: date
-- param end_date: date
-- param row_offset: integer
-- max_rows: 50
-- classification: confidential
SELECT t."uen" AS "uen",
       t."rail" AS "rail",
       t."direction" AS "direction",
       t."period_start" AS "period_start",
       t."period_end" AS "period_end",
       t."transaction_count" AS "transaction_count",
       t."total_value" AS "total_value",
       t."currency" AS "currency",
       t."fee_revenue" AS "fee_revenue"
FROM "payment_summaries" t 
WHERE t.uen = :uen AND t.period_end BETWEEN :start_date AND :end_date
ORDER BY period_end DESC, rail, direction, currency
OFFSET :row_offset
