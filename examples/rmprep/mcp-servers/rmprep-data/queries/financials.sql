-- description: Reviewed financials evidence; dated rows are bounded by the requested dates.
-- param uen: string
-- param start_date: date
-- param end_date: date
-- param row_offset: integer
-- max_rows: 50
-- classification: confidential
SELECT t."uen" AS "uen",
       t."concept" AS "concept",
       t."label" AS "label",
       t."statement" AS "statement",
       t."value" AS "value",
       t."unit" AS "unit",
       t."fiscal_year" AS "fiscal_year",
       t."period_end" AS "period_end",
       t."is_annual" AS "is_annual"
FROM "financial_facts" t 
WHERE t.uen = :uen AND t.period_end BETWEEN :start_date AND :end_date
ORDER BY period_end DESC, concept, statement, is_annual
OFFSET :row_offset
