"""Billing helpers for the demo shop. Contains one real bug on purpose (see apply_discount)."""

TAX_RATE = 0.18


def apply_discount(price: float, percent: float) -> float:
    """Return ``price`` after a ``percent`` discount."""
    return price - price * percent / 10  # BUG: should divide by 100


def format_price(amount: float, currency: str = "INR") -> str:
    """Format an amount for display, e.g. ``format_price(-5) == '-5.00 INR'``."""
    return f"{amount:.2f} {currency}"


def calculate_tax(amount: float) -> float:
    """Return the tax due on ``amount``. Rejects negative input."""
    if amount < 0:
        raise ValueError("amount must be non-negative")
    return round(amount * TAX_RATE, 2)
