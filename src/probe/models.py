from pydantic import BaseModel, Field
from typing import Literal

ClaimType = Literal[
    "symbol_exists",
    "bug_behavior",
    "security_vuln",
    "pr_fixes_issue",
    "pr_adds_tests",
    "version_affected"
]

class Claim(BaseModel):
    id: str = Field(description="Unique claim identifier, e.g. c1")
    type: ClaimType
    file: str | None = None
    symbol: str | None = None
    version: str | None = None
    expected: str | None = None
    actual: str | None = None
    repro: str | None = None
    issue_number: int | None = Field(default=None, description="For pr_fixes_issue: linked issue #N")
    quote: str = Field(description="Exact sentence from issue/PR text to prevent hallucinations")

class ClaimsResponse(BaseModel):
    claims: list[Claim]
    method: str = "llm"  # 'llm' or 'regex_fallback'
    model: str = "qwen3:8b"
    confidence: Literal["high", "medium", "low"] = "high"
    dropped_claims: list[dict] = Field(default_factory=list, description="Claims dropped due to quote mismatches")

# Individual tool observation (What static, sandbox, or diff checkers observe)
CheckOutcome = Literal["supports", "contradicts", "inconclusive"]

class CheckResult(BaseModel):
    claim_id: str
    checker: str  # 'static_ast', 'pr_diff', 'sandbox', 'version'
    outcome: CheckOutcome
    evidence: str
    line_number: int | None = None
    details: dict = Field(default_factory=dict)

# Final Verdict (What verdict.py produces for judges & maintainers)
VerdictStatus = Literal["confirmed", "cannot-reproduce", "invalid", "uncertain"]

class VerdictResult(BaseModel):
    claim_id: str
    status: VerdictStatus
    confidence: Literal["high", "medium", "low"]
    evidence: str
    quote: str
