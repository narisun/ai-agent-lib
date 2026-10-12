-- description: Reviewed transactions evidence; dated rows are bounded by the requested dates.
-- param uen: string
-- param start_date: date
-- param end_date: date
-- param row_offset: integer
-- max_rows: 50
-- classification: confidential
SELECT t."transaction_id" AS "transaction_id",
       t."uen" AS "uen",
       t."rail" AS "rail",
       t."direction" AS "direction",
       t."value_date" AS "value_date",
       t."amount" AS "amount",
       t."currency" AS "currency",
       t."fee" AS "fee",
       t."counterparty" AS "counterparty",
       t."memo" AS "memo"
FROM "transactions" t 
WHERE t.uen = :uen AND t.value_date BETWEEN :start_date AND :end_date
ORDER BY value_date DESC, transaction_id
OFFSET :row_offset
