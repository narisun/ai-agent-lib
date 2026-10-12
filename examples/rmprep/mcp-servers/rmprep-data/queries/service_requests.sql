-- description: Reviewed service requests evidence; dated rows are bounded by the requested dates.
-- param uen: string
-- param start_date: date
-- param end_date: date
-- param row_offset: integer
-- max_rows: 50
-- classification: confidential
SELECT t."uen" AS "uen",
       t."request" AS "request",
       t."product" AS "product",
       t."close_date" AS "close_date",
       t."status" AS "status",
       t."category" AS "category",
       t."priority" AS "priority",
       t."opened_date" AS "opened_date"
FROM "service_requests" t 
WHERE t.uen = :uen AND t.opened_date BETWEEN :start_date AND :end_date
ORDER BY opened_date DESC, request, product
OFFSET :row_offset
