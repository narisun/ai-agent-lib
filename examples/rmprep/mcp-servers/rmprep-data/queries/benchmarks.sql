-- description: Reviewed benchmarks evidence; dated rows are bounded by the requested dates.
-- param uen: string
-- param start_date: date
-- param end_date: date
-- param row_offset: integer
-- max_rows: 50
-- classification: confidential
SELECT t."naics_industry" AS "naics_industry",
       t."sector" AS "sector",
       t."indicator" AS "indicator",
       t."external_macro" AS "external_macro",
       t."cohort_avg" AS "cohort_avg",
       t."unit" AS "unit",
       t."higher_is_better" AS "higher_is_better"
FROM "benchmarks" t JOIN accounts a ON a.uen = :uen
WHERE t.naics_industry = CAST(SUBSTRING(CAST(a.naics_code AS VARCHAR), 1, 5) AS BIGINT) AND :start_date <= :end_date
ORDER BY "naics_industry", "indicator"
OFFSET :row_offset
