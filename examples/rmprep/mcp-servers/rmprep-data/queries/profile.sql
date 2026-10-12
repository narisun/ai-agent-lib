-- description: Reviewed profile evidence; dated rows are bounded by the requested dates.
-- param uen: string
-- param start_date: date
-- param end_date: date
-- param row_offset: integer
-- max_rows: 50
-- classification: confidential
SELECT t."uen" AS "uen",
       t."connected_uen" AS "connected_uen",
       t."name" AS "name",
       t."industry" AS "industry",
       LPAD(CAST(t.sic_code AS VARCHAR), 4, '0') AS sic_code,
       CAST(t.naics_code AS VARCHAR) AS naics_code,
       t."annual_revenue" AS "annual_revenue",
       t."employees" AS "employees",
       t."city" AS "city",
       t."state" AS "state",
       t."country" AS "country",
       t."currency" AS "currency",
       t."relationship_manager" AS "relationship_manager",
       t."segment" AS "segment",
       t."customer_since" AS "customer_since",
       t."founded_year" AS "founded_year",
       t."business_description" AS "business_description",
       t."locations" AS "locations",
       t."nps" AS "nps",
       t."logo_url" AS "logo_url"
FROM "accounts" t 
WHERE t.uen = :uen AND :start_date <= :end_date
ORDER BY "uen"
OFFSET :row_offset
