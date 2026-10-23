-- description: Balances of the accounts in one region.
-- param region: string
-- param min_balance: number = 0
-- max_rows: 3
-- classification: restricted
SELECT account_id, NVL(holder, '?') AS holder, region, balance
FROM accounts
WHERE region = :region AND balance >= :min_balance
ORDER BY account_id
