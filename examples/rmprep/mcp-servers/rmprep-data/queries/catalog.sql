-- description: Reviewed catalog evidence; dated rows are bounded by the requested dates.
-- param search: string
-- param start_date: date
-- param end_date: date
-- param row_offset: integer
-- max_rows: 50
-- classification: internal
SELECT t."product_id" AS "product_id",
       t."name" AS "name",
       t."family" AS "family",
       t."description" AS "description",
       t."client_benefit" AS "client_benefit",
       t."min_annual_revenue" AS "min_annual_revenue",
       t."min_dscr" AS "min_dscr",
       t."max_risk_rating" AS "max_risk_rating",
       t."eligibility_criteria" AS "eligibility_criteria",
       t."revenue_to_bank" AS "revenue_to_bank",
       t."typical_pricing" AS "typical_pricing"
FROM "catalog" t 
WHERE STRPOS(LOWER(CAST(t.name AS VARCHAR)), LOWER(:search)) > 0 AND :start_date <= :end_date
ORDER BY "product_id"
OFFSET :row_offset
