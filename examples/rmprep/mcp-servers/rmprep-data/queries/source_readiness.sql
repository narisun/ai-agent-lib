-- description: Reviewed source readiness evidence; dated rows are bounded by the requested dates.
-- param uen: string
-- param start_date: date
-- param end_date: date
-- param row_offset: integer
-- max_rows: 50
-- classification: confidential
SELECT t."readiness_id" AS "readiness_id",
       t."uen" AS "uen",
       t."domain" AS "domain",
       t."as_of_date" AS "as_of_date",
       t."completeness_pct" AS "completeness_pct",
       t."reconciliation_status" AS "reconciliation_status",
       t."quality_status" AS "quality_status",
       t."issue" AS "issue",
       t."owner" AS "owner",
       t."last_updated" AS "last_updated"
FROM "source_readiness" t 
WHERE t.uen = :uen AND t.last_updated BETWEEN :start_date AND :end_date
ORDER BY domain, readiness_id
OFFSET :row_offset
