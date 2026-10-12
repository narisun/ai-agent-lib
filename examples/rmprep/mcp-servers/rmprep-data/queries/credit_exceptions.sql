-- description: Reviewed credit exceptions evidence; dated rows are bounded by the requested dates.
-- param uen: string
-- param start_date: date
-- param end_date: date
-- param row_offset: integer
-- max_rows: 50
-- classification: confidential
SELECT t."exception_id" AS "exception_id",
       t."uen" AS "uen",
       t."facility_id" AS "facility_id",
       t."exception_type" AS "exception_type",
       t."requirement" AS "requirement",
       t."due_date" AS "due_date",
       t."received_date" AS "received_date",
       t."status" AS "status",
       t."severity" AS "severity",
       t."owner" AS "owner",
       t."waiver_expiry" AS "waiver_expiry",
       t."cure_plan" AS "cure_plan",
       t."last_updated" AS "last_updated"
FROM "credit_exceptions" t 
WHERE t.uen = :uen AND t.last_updated BETWEEN :start_date AND :end_date
ORDER BY due_date, exception_id
OFFSET :row_offset
