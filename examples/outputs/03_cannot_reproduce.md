### 🔎 Probe evidence report: format_price crashes on negative amounts

**Overall:** 🔁 `cannot-reproduce`  
**Checked against:** `examples/repos/shop` (demo repo)  
**Input:** synthetic issue against the demo repo (format_price(-5) actually returns '-5.00 INR')

| # | Claim (quoted) | Verdict | Evidence |
|---|---|---|---|
| c1 | "Calling `format_price()` with a negative refund amount crashes the checkout page." | 🔁 `cannot-reproduce` (medium) | Ran cleanly with exit code 0 (claimed crash or assertion failure did not occur) |

<details><summary>Checks that ran</summary>

- `c1` · `static_ast` → **supports** (symbol_found): `format_price` is defined in `shop/billing.py` at line 11 (function).
- `c1` · `sandbox` → **contradicts** (None): Ran cleanly with exit code 0 (claimed crash or assertion failure did not occur)

</details>

---
Expected overall verdict: `cannot-reproduce` · Status: **matches expected** · Verdicts from: probe.verdict.evaluate_claim_verdict

_Automated triage; a maintainer makes the final call._
