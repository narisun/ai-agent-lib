-- description: Reviewed forward curve evidence; dated rows are bounded by the requested dates.
-- param search: string
-- param start_date: date
-- param end_date: date
-- param row_offset: integer
-- max_rows: 50
-- classification: internal
SELECT t."curve" AS "curve",
       t."tenor" AS "tenor",
       t."rate" AS "rate",
       t."as_of" AS "as_of",
       t."source" AS "source"
FROM "forward_curve" t 
WHERE STRPOS(LOWER(CAST(t.curve AS VARCHAR)), LOWER(:search)) > 0 AND t.as_of BETWEEN :start_date AND :end_date
ORDER BY "curve", "tenor", "as_of"
OFFSET :row_offset
