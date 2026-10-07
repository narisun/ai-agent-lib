"""The agent's tools. Plain functions; the library governs them."""

from __future__ import annotations

from types import MappingProxyType

__all__ = ["lookup_balance"]

# Sample data. A later version reads this through a governed data source.
_BALANCES = MappingProxyType(
    {
        "4411": ("USD", "1,250.00"),
        "4412": ("USD", "87,300.10"),
        "5520": ("EUR", "12,004.55"),
    }
)


def lookup_balance(account: str) -> str:
    """Return the current balance of an account, given its account number."""
    found = _BALANCES.get(account.strip())
    if found is None:
        return f"No account with number {account} was found."
    currency, amount = found
    return f"The balance of account {account} is {amount} {currency}."
