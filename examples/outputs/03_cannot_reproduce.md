### 🔎 Probe evidence report: format_price crashes on negative amounts

**Overall:** ⚠️ `uncertain`  
**Checked against:** `examples/repos/shop` (demo repo)  
**Input:** synthetic issue against the demo repo (format_price(-5) actually returns '-5.00 INR')

| # | Claim (quoted) | Verdict | Evidence |
|---|---|---|---|
| c1 | "Calling `format_price()` with a negative refund amount crashes the checkout page." | ⚠️ `uncertain` (low) | Not enough evidence to decide. `format_price` is defined in `shop/billing.py` at line 11 (function). |

<details><summary>Checks that ran</summary>

- `c1` · `static_ast` → **supports** (symbol_found): `format_price` is defined in `shop/billing.py` at line 11 (function).

</details>

---
Expected overall verdict: `cannot-reproduce` · Status: **pending sandbox** · Verdicts from: baseline rules (probe.verdict not available yet)

_Automated triage; a maintainer makes the final call._
