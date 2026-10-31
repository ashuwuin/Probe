### 🔎 Probe evidence report: apply_discount returns the wrong price

**Overall:** ✅ `confirmed`  
**Checked against:** `examples/repos/shop` (demo repo)  
**Input:** synthetic issue against the demo repo examples/repos/shop (the bug is real: apply_discount(100, 10) returns 0.0)

| # | Claim (quoted) | Verdict | Evidence |
|---|---|---|---|
| c1 | "`apply_discount()` in `shop/billing.py` returns 0.0 for a 10% discount on 100 instead of 90.0." | ✅ `confirmed` (high) | Reproduced failure (AssertionError): Traceback (most recent call last): File "<stdin>", line 3, in <module> AssertionError: 0.0 |

<details><summary>Checks that ran</summary>

- `c1` · `static_ast` → **supports** (symbol_found): `apply_discount` is defined in `shop/billing.py` at line 6 (function).
- `c1` · `sandbox` → **supports** (None): Reproduced failure (AssertionError): Traceback (most recent call last): File "<stdin>", line 3, in <module> AssertionError: 0.0

</details>

---
Expected overall verdict: `confirmed` · Status: **matches expected** · Verdicts from: probe.verdict.evaluate_claim_verdict

_Automated triage; a maintainer makes the final call._
