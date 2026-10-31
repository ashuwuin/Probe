### 🔎 Probe evidence report: Fix discount calculation

**Overall:** ⚠️ `uncertain`  
**Checked against:** `examples/repos/shop` (demo repo)  
**Input:** synthetic pull request against the demo repo: it claims tests but changes only shop/billing.py

| # | Claim (quoted) | Verdict | Evidence |
|---|---|---|---|
| c1 | "adds unit tests for apply_discount" | ❌ `invalid` (medium) | The PR changes 1 file(s) but none is a test file, and the diff adds no test functions. |
| c2 | "Fixes the discount calculation in `apply_discount()`" | ⚠️ `uncertain` (medium) | PR touches reported target (The PR modifies `shop/billing.py`, which the linked issue names. This shows the PR touches the reported code, not that the fix is correct.), but fix correctness requires execution testing. |

<details><summary>Checks that ran</summary>

- `c1` · `pr_diff` → **contradicts** (no_test_files): The PR changes 1 file(s) but none is a test file, and the diff adds no test functions.
- `c2` · `pr_diff` → **supports** (issue_target_touched): The PR modifies `shop/billing.py`, which the linked issue names. This shows the PR touches the reported code, not that the fix is correct.

</details>

---
Expected overall verdict: `uncertain` · Status: **matches expected** · Verdicts from: probe.verdict.evaluate_claim_verdict

_Automated triage; a maintainer makes the final call._
