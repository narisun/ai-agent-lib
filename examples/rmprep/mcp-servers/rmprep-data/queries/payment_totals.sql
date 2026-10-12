-- description: Reviewed payment totals evidence; dated rows are bounded by the requested dates.
-- param uen: string
-- param start_date: date
-- param end_date: date
-- param row_offset: integer
-- max_rows: 50
-- classification: confidential
SELECT t.uen AS uen,
       COALESCE(r.xref_name, t.rail) AS rail,
       COALESCE(d.xref_name, t.direction) AS direction,
       t.currency AS currency, MIN(t.value_date) AS first_value_date,
       MAX(t.value_date) AS last_value_date, COUNT(*) AS transaction_count,
       SUM(CAST(t.amount AS DECIMAL(24, 2))) AS total_value,
       SUM(CAST(t.fee AS DECIMAL(24, 2))) AS gross_fees
FROM transactions t
LEFT JOIN (SELECT LOWER(xref_value) AS value_key, MIN(xref_name) AS xref_name
           FROM xref WHERE xref_type = 'payment_rail' GROUP BY LOWER(xref_value)) r
ON LOWER(t.rail) = r.value_key
LEFT JOIN (SELECT LOWER(xref_value) AS value_key, MIN(xref_name) AS xref_name
           FROM xref WHERE xref_type = 'payment_direction' GROUP BY LOWER(xref_value)) d
ON LOWER(t.direction) = d.value_key
WHERE t.uen = :uen AND t.value_date BETWEEN :start_date AND :end_date
GROUP BY t.uen, COALESCE(r.xref_name, t.rail), COALESCE(d.xref_name, t.direction), t.currency
ORDER BY currency, rail, direction
OFFSET :row_offset
