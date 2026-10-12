-- description: Reviewed facilities evidence; dated rows are bounded by the requested dates.
-- param uen: string
-- param start_date: date
-- param end_date: date
-- param row_offset: integer
-- max_rows: 50
-- classification: confidential
SELECT t."facility_id" AS "facility_id",
       t."uen" AS "uen",
       t."lender" AS "lender",
       t."facility_type" AS "facility_type",
       t."commitment" AS "commitment",
       t."outstanding" AS "outstanding",
       t."interest_rate_basis" AS "interest_rate_basis",
       t."origination_date" AS "origination_date",
       t."maturity_date" AS "maturity_date",
       t."purpose" AS "purpose",
       t."collateral_type" AS "collateral_type",
       t."collateral_value" AS "collateral_value",
       t."ltv_pct" AS "ltv_pct",
       t."our_hold" AS "our_hold",
       t."facility_risk_rating" AS "facility_risk_rating",
       t."spread_bps" AS "spread_bps",
       t."all_in_rate_pct" AS "all_in_rate_pct",
       t."unused_fee_bps" AS "unused_fee_bps",
       DATEDIFF(day, :end_date, t.maturity_date) AS days_to_maturity,
       'USD' AS amount_currency
FROM "facilities" t 
WHERE t.uen = :uen AND t.origination_date BETWEEN :start_date AND :end_date
ORDER BY maturity_date, facility_id
OFFSET :row_offset
