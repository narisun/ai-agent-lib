-- description: Reviewed relationship evidence; dated rows are bounded by the requested dates.
-- param uen: string
-- param start_date: date
-- param end_date: date
-- param row_offset: integer
-- max_rows: 50
-- classification: confidential
SELECT t."uen" AS "uen",
       t."nii" AS "nii",
       t."nir" AS "nir",
       t."total_deposits" AS "total_deposits",
       t."roe" AS "roe",
       t."coverage_team" AS "coverage_team",
       t."strategic_priorities" AS "strategic_priorities",
       t."ask" AS "ask",
       t."commercial" AS "commercial",
       t."tps" AS "tps",
       t."capital_markets" AS "capital_markets",
       t."wealth" AS "wealth",
       t."retail" AS "retail",
       t."products_held" AS "products_held",
       t."total_exposure" AS "total_exposure",
       t."primary_operating_account" AS "primary_operating_account",
       t."last_review_date" AS "last_review_date",
       t."next_review_date" AS "next_review_date",
       t."wallet_share_pct" AS "wallet_share_pct",
       t."raroc_pct" AS "raroc_pct"
FROM "relationship" t 
WHERE t.uen = :uen AND :start_date <= :end_date
ORDER BY "uen"
OFFSET :row_offset
