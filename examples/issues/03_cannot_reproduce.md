# format_price crashes on negative amounts

Calling `format_price()` with a negative refund amount crashes the checkout page.

```python
from shop.billing import format_price

print(format_price(-5))
```

Actual: `ValueError: amount must be non-negative`.
