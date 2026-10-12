-- description: Reviewed utilization evidence; dated rows are bounded by the requested dates.
-- param uen: string
-- param start_date: date
-- param end_date: date
-- param row_offset: integer
-- max_rows: 50
-- classification: confidential
SELECT t."facility_id" AS "facility_id",
       t."as_of" AS "as_of",
       t."drawn" AS "drawn",
       t."available" AS "available",
       t."utilization_pct" AS "utilization_pct",
       f.uen AS uen,
       'USD' AS amount_currency
FROM "utilization" t JOIN facilities f ON f.facility_id = t.facility_id
WHERE f.uen = :uen AND t.as_of BETWEEN :start_date AND :end_date
ORDER BY as_of DESC, facility_id
OFFSET :row_offset
