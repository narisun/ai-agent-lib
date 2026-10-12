-- description: Reviewed opportunities evidence; dated rows are bounded by the requested dates.
-- param uen: string
-- param start_date: date
-- param end_date: date
-- param row_offset: integer
-- max_rows: 50
-- classification: confidential
SELECT t."opportunity_id" AS "opportunity_id",
       t."uen" AS "uen",
       t."name" AS "name",
       t."product" AS "product",
       t."stage" AS "stage",
       t."amount" AS "amount",
       t."probability" AS "probability",
       t."close_date" AS "close_date",
       t."competitor" AS "competitor",
       t."owner" AS "owner",
       t."created_date" AS "created_date",
       t."next_step" AS "next_step"
FROM "opportunities" t 
WHERE t.uen = :uen AND t.created_date BETWEEN :start_date AND :end_date
ORDER BY created_date DESC, opportunity_id
OFFSET :row_offset
