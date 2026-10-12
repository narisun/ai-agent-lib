-- description: Reviewed commitments evidence; dated rows are bounded by the requested dates.
-- param uen: string
-- param start_date: date
-- param end_date: date
-- param row_offset: integer
-- max_rows: 50
-- classification: confidential
SELECT t."commitment_id" AS "commitment_id",
       t."uen" AS "uen",
       t."meeting_date" AS "meeting_date",
       t."owner" AS "owner",
       t."commitment" AS "commitment",
       t."due_date" AS "due_date",
       t."status" AS "status",
       t."client_or_bank" AS "client_or_bank",
       t."priority" AS "priority",
       t."last_updated" AS "last_updated"
FROM "commitments" t 
WHERE t.uen = :uen AND t.last_updated BETWEEN :start_date AND :end_date
ORDER BY due_date, commitment_id
OFFSET :row_offset
