-- description: Reviewed profitability evidence; dated rows are bounded by the requested dates.
-- param uen: string
-- param start_date: date
-- param end_date: date
-- param row_offset: integer
-- max_rows: 50
-- classification: confidential
SELECT t."uen" AS "uen",
       t."period" AS "period",
       t."line_of_business" AS "line_of_business",
       t."product" AS "product",
       t."revenue_nii" AS "revenue_nii",
       t."revenue_fee" AS "revenue_fee",
       t."expense_direct" AS "expense_direct",
       t."risk_cost" AS "risk_cost",
       t."allocated_capital" AS "allocated_capital"
FROM "product_profitability" t 
WHERE t.uen = :uen AND :start_date <= :end_date
ORDER BY period DESC, line_of_business, product
OFFSET :row_offset
