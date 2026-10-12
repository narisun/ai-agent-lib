-- description: Reviewed borrowing base evidence; dated rows are bounded by the requested dates.
-- param uen: string
-- param start_date: date
-- param end_date: date
-- param row_offset: integer
-- max_rows: 50
-- classification: confidential
SELECT t."facility_id" AS "facility_id",
       t."uen" AS "uen",
       t."as_of" AS "as_of",
       t."eligible_ar" AS "eligible_ar",
       t."eligible_inventory" AS "eligible_inventory",
       t."advance_rate_ar_pct" AS "advance_rate_ar_pct",
       t."advance_rate_inventory_pct" AS "advance_rate_inventory_pct",
       t."availability" AS "availability",
       t."currency" AS "currency",
       f.commitment AS facility_commitment_usd,
       CASE WHEN t.currency = 'USD' THEN ROUND(100.0 * t.availability / NULLIF(f.commitment, 0), 2) END AS availability_pct_of_commitment
FROM "borrowing_base" t JOIN facilities f ON f.facility_id = t.facility_id AND f.uen = t.uen
WHERE t.uen = :uen AND t.as_of BETWEEN :start_date AND :end_date
ORDER BY as_of DESC, facility_id
OFFSET :row_offset
