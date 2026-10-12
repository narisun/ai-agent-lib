-- description: Reviewed treasury controls evidence; dated rows are bounded by the requested dates.
-- param uen: string
-- param start_date: date
-- param end_date: date
-- param row_offset: integer
-- max_rows: 50
-- classification: confidential
SELECT t."control_id" AS "control_id",
       t."uen" AS "uen",
       t."product" AS "product",
       t."control_type" AS "control_type",
       t."status" AS "status",
       t."severity" AS "severity",
       t."observation" AS "observation",
       t."detected_date" AS "detected_date",
       t."owner" AS "owner",
       t."target_date" AS "target_date",
       t."last_updated" AS "last_updated"
FROM "treasury_controls" t 
WHERE t.uen = :uen AND t.last_updated BETWEEN :start_date AND :end_date
ORDER BY target_date, control_id
OFFSET :row_offset
