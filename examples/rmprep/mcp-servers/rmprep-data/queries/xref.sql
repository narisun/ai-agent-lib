-- description: Reviewed xref evidence; dated rows are bounded by the requested dates.
-- param search: string
-- param start_date: date
-- param end_date: date
-- param row_offset: integer
-- max_rows: 50
-- classification: internal
SELECT t."xref_id" AS "xref_id",
       t."xref_type" AS "xref_type",
       t."xref_name" AS "xref_name",
       t."xref_value" AS "xref_value",
       t."xref_description" AS "xref_description"
FROM "xref" t 
WHERE STRPOS(LOWER(CAST(t.xref_type AS VARCHAR)), LOWER(:search)) > 0 AND :start_date <= :end_date
ORDER BY "xref_id"
OFFSET :row_offset
