-- description: Reviewed credit evidence; dated rows are bounded by the requested dates.
-- param uen: string
-- param start_date: date
-- param end_date: date
-- param row_offset: integer
-- max_rows: 50
-- classification: confidential
SELECT t."uen" AS "uen",
       t."risk_rating" AS "risk_rating",
       t."prior_risk_rating" AS "prior_risk_rating",
       t."risk_rating_as_of" AS "risk_rating_as_of",
       t."annual_review_date" AS "annual_review_date",
       t."trigger_exceptions" AS "trigger_exceptions",
       t."ebitda" AS "ebitda",
       t."profit_margin_pct" AS "profit_margin_pct",
       t."debt_to_net_worth" AS "debt_to_net_worth",
       t."fixed_charge_coverage" AS "fixed_charge_coverage",
       t."debt_service_coverage" AS "debt_service_coverage",
       t."current_ratio" AS "current_ratio",
       t."pd_pct" AS "pd_pct",
       t."lgd_pct" AS "lgd_pct",
       t."rating_outlook" AS "rating_outlook",
       t."watch_status" AS "watch_status"
FROM "credit_metrics" t 
WHERE t.uen = :uen AND t.risk_rating_as_of BETWEEN :start_date AND :end_date
ORDER BY "uen"
OFFSET :row_offset
