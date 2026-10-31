"""Sample module used by Probe's static-checker tests. It is parsed, never imported."""

TAX_RATE = 0.18
DEFAULT_CURRENCY: str = "INR"
MIN_AMOUNT, MAX_AMOUNT = 0, 1_000_000


def calculate_tax(amount: float) -> float:
    """Return tax for ``amount``. Rejects negative input."""
    if amount < 0:
        raise ValueError("amount must be non-negative")
    return round(amount * TAX_RATE, 2)


async def fetch_exchange_rate(currency: str) -> float:
    return 1.0


class Invoice:
    currency = DEFAULT_CURRENCY

    def __init__(self, amount: float) -> None:
        self.amount = amount

    def total(self) -> float:
        return self.amount + calculate_tax(self.amount)


try:
    from decimal import Decimal as Money
except ImportError:  # pragma: no cover
    Money = float
