### 🔎 Probe evidence report: apply_discount returns the wrong price

**Overall:** ⚠️ `uncertain`  
**Checked against:** `examples/repos/shop` (demo repo)  
**Input:** synthetic issue against the demo repo examples/repos/shop (the bug is real: apply_discount(100, 10) returns 0.0)

| # | Claim (quoted) | Verdict | Evidence |
|---|---|---|---|
| c1 | "`apply_discount()` in `shop/billing.py` returns 0.0 for a 10% discount on 100 instead of 90.0." | ⚠️ `uncertain` (low) | Not enough evidence to decide. `apply_discount` is defined in `shop/billing.py` at line 6 (function). |

<details><summary>Checks that ran</summary>

- `c1` · `static_ast` → **supports** (symbol_found): `apply_discount` is defined in `shop/billing.py` at line 6 (function).

</details>

---
Expected overall verdict: `confirmed` · Status: **pending sandbox** · Verdicts from: baseline rules (probe.verdict not available yet)

_Automated triage; a maintainer makes the final call._
