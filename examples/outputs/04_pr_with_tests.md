### 🔎 Probe evidence report: Raise InvalidUrl if host starts with '.' character.

**Overall:** ✅ `confirmed`  
**Checked against:** [psf/requests@658c10e](https://github.com/psf/requests/tree/658c10e84a461e5298199f20f17b510b90ac478a)  
**Input:** real merged pull request psf/requests#5414 (description and diff copied from GitHub)

| # | Claim (quoted) | Verdict | Evidence |
|---|---|---|---|
| c1 | "I've added `InvalidUrl` tests for `http://.example.com` and `http://*example.com`." | ✅ `confirmed` (high) | The diff adds lines to 1 test file(s): tests/test_requests.py. |

<details><summary>Checks that ran</summary>

- `c1` · `pr_diff` → **supports** (tests_added): The diff adds lines to 1 test file(s): tests/test_requests.py.

</details>

---
Expected overall verdict: `confirmed` · Status: **matches expected** · Verdicts from: baseline rules (probe.verdict not available yet)

_Automated triage; a maintainer makes the final call._
