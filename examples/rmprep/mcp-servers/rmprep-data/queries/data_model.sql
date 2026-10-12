-- description: Reviewed data model evidence; dated rows are bounded by the requested dates.
-- param search: string
-- param start_date: date
-- param end_date: date
-- param row_offset: integer
-- max_rows: 50
-- classification: internal
SELECT t."domain" AS "domain",
       t."table" AS "table",
       t."column" AS "column",
       t."datatype" AS "datatype",
       t."nullable" AS "nullable",
       t."pk" AS "pk",
       t."uk" AS "uk",
       t."fk" AS "fk",
       t."description" AS "description"
FROM "data_model" t 
WHERE STRPOS(LOWER(CAST(t."table" AS VARCHAR)), LOWER(:search)) > 0 AND t."table" <> 'rm_scenarios' AND :start_date <= :end_date
ORDER BY "table", "column"
OFFSET :row_offset
