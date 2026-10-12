-- description: Reviewed counterparties evidence; dated rows are bounded by the requested dates.
-- param uen: string
-- param start_date: date
-- param end_date: date
-- param row_offset: integer
-- max_rows: 50
-- classification: confidential
SELECT t."uen" AS "uen",
       t."counterparty" AS "counterparty",
       t."role" AS "role",
       t."direction" AS "direction",
       t."rail" AS "rail",
       t."country" AS "country",
       t."currency" AS "currency",
       t."annual_value" AS "annual_value",
       t."counterparty_bank" AS "counterparty_bank",
       t."note" AS "note"
FROM "counterparties" t 
WHERE t.uen = :uen AND :start_date <= :end_date
ORDER BY annual_value DESC, counterparty, direction, rail, currency
OFFSET :row_offset
