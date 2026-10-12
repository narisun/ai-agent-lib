-- description: Reviewed find clients evidence; dated rows are bounded by the requested dates.
-- param search: string
-- max_rows: 50
-- classification: confidential
SELECT uen, name, industry, country, currency,
       CASE WHEN LOWER(uen) = LOWER(:search) OR LOWER(name) = LOWER(:search) THEN 0 ELSE 1 END AS match_rank
FROM accounts
WHERE :search <> '' AND (LOWER(uen) = LOWER(:search) OR STRPOS(LOWER(name), LOWER(:search)) > 0)
ORDER BY match_rank, name, uen
