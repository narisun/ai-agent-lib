-- description: Reviewed company evidence; dated rows are bounded by the requested dates.
-- param uen: string
-- param start_date: date
-- param end_date: date
-- param row_offset: integer
-- max_rows: 50
-- classification: confidential
SELECT t."uen" AS "uen",
       t."company_name" AS "company_name",
       t."cik" AS "cik"
FROM "company" t 
WHERE t.uen = :uen AND :start_date <= :end_date
ORDER BY "uen"
OFFSET :row_offset
