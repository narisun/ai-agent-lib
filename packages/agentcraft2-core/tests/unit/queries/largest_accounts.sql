-- description: The largest accounts, biggest first.
-- param opened_after: date = 2000-01-01
-- max_rows: 50
SELECT account_id, region, balance
FROM accounts
WHERE opened >= :opened_after
ORDER BY balance DESC
LIMIT 2
