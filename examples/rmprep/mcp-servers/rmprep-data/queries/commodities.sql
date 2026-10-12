-- description: Reviewed commodities evidence; dated rows are bounded by the requested dates.
-- param search: string
-- param start_date: date
-- param end_date: date
-- param row_offset: integer
-- max_rows: 50
-- classification: internal
SELECT t."name" AS "name",
       t."value" AS "value",
       t."unit" AS "unit",
       t."as_of" AS "as_of",
       t."source" AS "source",
       t."qoq_change_pct" AS "qoq_change_pct",
       t."note" AS "note"
FROM "commodities" t 
WHERE STRPOS(LOWER(CAST(t.name AS VARCHAR)), LOWER(:search)) > 0 AND t.as_of BETWEEN :start_date AND :end_date
ORDER BY "name", "as_of"
OFFSET :row_offset
