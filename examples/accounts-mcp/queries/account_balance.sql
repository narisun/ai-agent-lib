-- description: The holder and balance of one account.
-- param account_id: integer
-- max_rows: 1
-- classification: restricted
SELECT account_id, holder, balance
FROM accounts
WHERE account_id = :account_id
