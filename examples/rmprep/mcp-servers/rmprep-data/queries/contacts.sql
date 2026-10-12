-- description: Reviewed contacts evidence; dated rows are bounded by the requested dates.
-- param uen: string
-- param start_date: date
-- param end_date: date
-- param row_offset: integer
-- max_rows: 50
-- classification: confidential
SELECT t."uen" AS "uen",
       t."first_name" AS "first_name",
       t."last_name" AS "last_name",
       t."title" AS "title",
       t."email" AS "email",
       t."phone" AS "phone",
       t."is_primary" AS "is_primary",
       t."role" AS "role",
       t."department" AS "department"
FROM "contacts" t 
WHERE t.uen = :uen AND :start_date <= :end_date
ORDER BY "uen", "first_name", "last_name", "title"
OFFSET :row_offset
