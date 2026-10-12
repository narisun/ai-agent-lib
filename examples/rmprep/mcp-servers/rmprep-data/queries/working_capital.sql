-- description: Reviewed working capital evidence; dated rows are bounded by the requested dates.
-- param uen: string
-- param start_date: date
-- param end_date: date
-- param row_offset: integer
-- max_rows: 50
-- classification: confidential
SELECT t."uen" AS "uen",
       t."period_end" AS "period_end",
       t."dso_days" AS "dso_days",
       t."dio_days" AS "dio_days",
       t."dpo_days" AS "dpo_days",
       t."cash_conversion_cycle_days" AS "cash_conversion_cycle_days",
       t."ar_over_90_pct" AS "ar_over_90_pct",
       t."inventory_aging_pct" AS "inventory_aging_pct",
       t."net_working_capital" AS "net_working_capital",
       t."currency" AS "currency",
       t."data_quality" AS "data_quality"
FROM "working_capital" t 
WHERE t.uen = :uen AND t.period_end BETWEEN :start_date AND :end_date
ORDER BY period_end DESC
OFFSET :row_offset
