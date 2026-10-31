### 🔎 Probe evidence report: Security: parse_url_v2() leaks proxy credentials

**Overall:** ❌ `invalid`  
**Checked against:** [psf/requests@3c7baaa](https://github.com/psf/requests/tree/3c7baaa05ab4aa49cc819ce3a819d260de19c157)  
**Input:** synthetic report naming a function that does not exist, checked against the real psf/requests codebase

| # | Claim (quoted) | Verdict | Evidence |
|---|---|---|---|
| c1 | "`parse_url_v2()` in `src/requests/utils.py` does not strip the userinfo part of proxy URLs before logging them" | ❌ `invalid` (high) | `parse_url_v2` is not defined or mentioned in any of the 37 Python file(s) in the repository. It is not defined in the named file `src/requests/utils.py`. |

<details><summary>Checks that ran</summary>

- `c1` · `static_ast` → **contradicts** (symbol_not_found): `parse_url_v2` is not defined or mentioned in any of the 37 Python file(s) in the repository. It is not defined in the named file `src/requests/utils.py`.

</details>

---
Expected overall verdict: `invalid` · Status: **matches expected** · Verdicts from: baseline rules (probe.verdict not available yet)

_Automated triage; a maintainer makes the final call._
