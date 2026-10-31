# apply_discount returns the wrong price

`apply_discount()` in `shop/billing.py` returns 0.0 for a 10% discount on 100 instead of 90.0.

Reproduction:

```python
from shop.billing import apply_discount

assert apply_discount(100, 10) == 90.0, apply_discount(100, 10)
```

Expected: `90.0`. Actual: `AssertionError: 0.0`.
