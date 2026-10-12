-- description: Reviewed fx evidence; dated rows are bounded by the requested dates.
-- param search: string
-- param start_date: date
-- param end_date: date
-- param row_offset: integer
-- max_rows: 50
-- classification: internal
SELECT t."pair" AS "pair",
       t."rate" AS "rate",
       t."as_of" AS "as_of",
       t."source" AS "source",
       t."ytd_change_pct" AS "ytd_change_pct",
       t."fwd_3m_pct" AS "fwd_3m_pct",
       t."fwd_12m_pct" AS "fwd_12m_pct",
       t."implied_vol_3m_pct" AS "implied_vol_3m_pct"
FROM "fx" t 
WHERE STRPOS(LOWER(CAST(t.pair AS VARCHAR)), LOWER(:search)) > 0 AND t.as_of BETWEEN :start_date AND :end_date
ORDER BY "pair", "as_of"
OFFSET :row_offset
