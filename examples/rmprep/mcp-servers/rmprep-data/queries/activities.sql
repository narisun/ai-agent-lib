-- description: Reviewed activities evidence; dated rows are bounded by the requested dates.
-- param uen: string
-- param start_date: date
-- param end_date: date
-- param row_offset: integer
-- max_rows: 50
-- classification: confidential
SELECT t."activity_id" AS "activity_id",
       t."uen" AS "uen",
       t."type" AS "type",
       t."subject" AS "subject",
       t."activity_date" AS "activity_date",
       t."owner" AS "owner",
       t."notes" AS "notes"
FROM "activities" t 
WHERE t.uen = :uen AND t.activity_date BETWEEN :start_date AND :end_date
ORDER BY activity_date DESC, activity_id
OFFSET :row_offset
