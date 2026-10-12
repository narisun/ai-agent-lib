-- description: Reviewed covenants evidence; dated rows are bounded by the requested dates.
-- param uen: string
-- param start_date: date
-- param end_date: date
-- param row_offset: integer
-- max_rows: 50
-- classification: confidential
SELECT t."facility_id" AS "facility_id",
       t."name" AS "name",
       t."threshold" AS "threshold",
       t."current_value" AS "current_value",
       t."in_compliance" AS "in_compliance",
       t."next_test_date" AS "next_test_date",
       f.uen AS uen
FROM "covenants" t JOIN facilities f ON f.facility_id = t.facility_id
WHERE f.uen = :uen AND :start_date <= :end_date
ORDER BY "facility_id", "name"
OFFSET :row_offset
