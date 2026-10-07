-- description: Accounts in one region, with holder and balance.
-- param region: string
-- param min_balance: number = 0
-- max_rows: 100
-- classification: restricted
SELECT account_id, holder, region, balance
FROM accounts
WHERE region = :region AND balance >= :min_balance
ORDER BY account_id
