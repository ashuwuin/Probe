"""Compare LLM-extracted claims with the hand-written gold claims (no network).

Usage (from the repository root, after running the benchmark in both modes):

    PYTHONPATH=src python eval/benchmark.py                    # gold   -> eval/results.json
    PYTHONPATH=src python eval/benchmark.py --mode extracted \\
        --out eval/RESULTS_extracted.md --json eval/results_extracted.json
    PYTHONPATH=src python eval/compare_extraction.py           # -> eval/EXTRACTION.md

Matching rule (per case): each gold claim is paired with at most one extracted
claim of the SAME type that has the same symbol, OR the same normalised file, OR
whose quote shares at least half of the gold quote's words. Recall = gold claims
found; precision = extracted claims that matched a gold claim. The regex fallback
(probe.extract.regex_extract) is run on the very same input texts as a baseline.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
MIN_QUOTE_OVERLAP = 0.5
_WORD_RE = re.compile(r"[a-z0-9_]+")


# --------------------------------------------------------------------------- matching

def norm_symbol(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().strip("`").removesuffix("()").strip()
    return value.split(".")[-1].lower() or None


def norm_file(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().strip("`").replace("\\", "/")
    value = re.sub(r":\d+(:\d+)?$", "", value)
    while value.startswith("./"):
        value = value[2:]
    return value.lstrip("/").lower() or None


def _files_match(a: str | None, b: str | None) -> bool:
    a, b = norm_file(a), norm_file(b)
    if not a or not b:
        return False
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


def quote_overlap(gold_quote: str, other_quote: str) -> float:
    """Share of the gold quote's distinct words that also occur in the other quote."""
    gold = set(_WORD_RE.findall(gold_quote.lower()))
    other = set(_WORD_RE.findall(other_quote.lower()))
    return len(gold & other) / len(gold) if gold else 0.0


def claims_match(gold: dict, other: dict) -> bool:
    if gold.get("type") != other.get("type"):
        return False
    gs, os_ = norm_symbol(gold.get("symbol")), norm_symbol(other.get("symbol"))
    if gs and gs == os_:
        return True
    if _files_match(gold.get("file"), other.get("file")):
        return True
    return quote_overlap(gold.get("quote", ""), other.get("quote", "")) >= MIN_QUOTE_OVERLAP


def match_claims(gold: list[dict], other: list[dict]) -> list[tuple[dict, dict]]:
    """Greedy one-to-one pairing in gold order; each extracted claim is used at most once."""
    used: set[int] = set()
    pairs = []
    for g in gold:
        for i, o in enumerate(other):
            if i not in used and claims_match(g, o):
                used.add(i)
                pairs.append((g, o))
                break
    return pairs


# --------------------------------------------------------------------------- scoring

@dataclass
class Score:
    gold: int = 0
    extracted: int = 0
    matched: int = 0
    by_type_gold: Counter = field(default_factory=Counter)
    by_type_found: Counter = field(default_factory=Counter)
    by_type_extracted: Counter = field(default_factory=Counter)
    by_type_extracted_matched: Counter = field(default_factory=Counter)
    field_hits: Counter = field(default_factory=Counter)
    field_totals: Counter = field(default_factory=Counter)

    def add(self, gold: list[dict], other: list[dict]) -> list[tuple[dict, dict]]:
        pairs = match_claims(gold, other)
        self.gold += len(gold)
        self.extracted += len(other)
        self.matched += len(pairs)
        self.by_type_gold.update(c["type"] for c in gold)
        self.by_type_extracted.update(c["type"] for c in other)
        for g, o in pairs:
            self.by_type_found[g["type"]] += 1
            self.by_type_extracted_matched[o["type"]] += 1
            if g.get("symbol"):
                self.field_totals["symbol"] += 1
                self.field_hits["symbol"] += norm_symbol(g["symbol"]) == norm_symbol(o.get("symbol"))
            if g.get("file"):
                self.field_totals["file"] += 1
                self.field_hits["file"] += _files_match(g["file"], o.get("file"))
            if g.get("repro"):
                self.field_totals["repro"] += 1
                self.field_hits["repro"] += bool(o.get("repro"))
        return pairs

    @property
    def recall(self) -> float | None:
        return self.matched / self.gold if self.gold else None

    @property
    def precision(self) -> float | None:
        return self.matched / self.extracted if self.extracted else None


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def _ratio(hit: int, total: int) -> str:
    return f"{hit}/{total} ({hit / total:.0%})" if total else "n/a"


def _regex_claims(text: str, is_pull_request: bool | None) -> list[dict]:
    from probe.extract import regex_extract

    return [c.model_dump() for c in regex_extract(text, is_pull_request=is_pull_request)]


def compare(gold_rows: list[dict], extracted_rows: list[dict]) -> dict:
    gold_by_id = {r["id"]: r for r in gold_rows}
    model, regex = Score(), Score()
    model_only = regex_only = 0
    verdict_same = verdict_total = 0
    claim_verdict_same = claim_verdict_total = 0
    drops: Counter = Counter()
    methods: Counter = Counter()
    seconds: list[float] = []
    text_sources: Counter = Counter()
    per_case = []
    meta_any = {}

    for row in extracted_rows:
        g = gold_by_id.get(row["id"])
        meta = row.get("extraction")
        if g is None or meta is None:
            continue
        meta_any = meta
        gold_claims = g["claims"]
        model_claims = row["claims"]
        model_pairs = model.add(gold_claims, model_claims)

        regex_claims = _regex_claims(meta["text"], meta.get("is_pull_request"))
        regex_pairs = regex.add(gold_claims, regex_claims)
        model_found = {id(gc) for gc, _ in model_pairs}
        regex_found = {id(gc) for gc, _ in regex_pairs}
        model_only += len(model_found - regex_found)
        regex_only += len(regex_found - model_found)

        verdict_total += 1
        verdict_same += row["prediction"] == g["prediction"]
        gold_v = {v["claim_id"]: v["status"] for v in g["verdicts"]}
        model_v = {v["claim_id"]: v["status"] for v in row["verdicts"]}
        for gc, mc in model_pairs:
            claim_verdict_total += 1
            claim_verdict_same += gold_v.get(gc["id"]) == model_v.get(mc["id"])

        drops.update(d.get("reason", "unknown") for d in meta.get("dropped_claims", []))
        methods[meta["method"]] += 1
        seconds.append(meta["seconds"])
        text_sources[meta.get("text_source", "unknown")] += 1
        per_case.append({
            "id": row["id"],
            "text_source": meta.get("text_source", "unknown"),
            "method": meta["method"],
            "seconds": meta["seconds"],
            "gold": len(gold_claims),
            "model": len(model_claims),
            "model_found": len(model_pairs),
            "regex": len(regex_claims),
            "regex_found": len(regex_pairs),
            "dropped": len(meta.get("dropped_claims", [])),
            "gold_prediction": g["prediction"],
            "model_prediction": row["prediction"],
        })

    return {
        "model": model, "regex": regex, "model_only": model_only, "regex_only": regex_only,
        "verdict_same": verdict_same, "verdict_total": verdict_total,
        "claim_verdict_same": claim_verdict_same, "claim_verdict_total": claim_verdict_total,
        "drops": drops, "methods": methods, "seconds": seconds, "text_sources": text_sources,
        "per_case": per_case, "meta": meta_any,
    }


# --------------------------------------------------------------------------- report

def render(result: dict) -> str:
    m, r = result["model"], result["regex"]
    meta = result["meta"]
    secs = result["seconds"]
    cases = result["verdict_total"]
    llm_cases = result["methods"].get("llm", 0)
    lines = [
        "# LLM claim extraction vs. gold claims",
        "",
        f"Model: `{meta.get('model', '?')}` (Ollama ID `{meta.get('model_digest') or 'unknown'}`), "
        f"temperature {meta.get('temperature', '?')}, thinking off. Cases compared: {cases}. "
        "Generated by `eval/compare_extraction.py` from `results.json` (gold) and `results_extracted.json`.",
        "",
        "Matching rule: a gold claim is found when an extracted claim has the same type and the same symbol, "
        f"the same file, or a quote sharing at least {MIN_QUOTE_OVERLAP:.0%} of the gold quote's words. "
        "Each extracted claim can match only one gold claim.",
        "",
        "## Headline",
        "",
        "| | qwen3:8b (LLM) | Regex fallback (same texts) |",
        "|---|---|---|",
        f"| Recall (gold claims found) | {_ratio(m.matched, m.gold)} | {_ratio(r.matched, r.gold)} |",
        f"| Precision (extracted claims that match a gold claim) | {_ratio(m.matched, m.extracted)} | {_ratio(r.matched, r.extracted)} |",
        f"| Claims extracted | {m.extracted} | {r.extracted} |",
        "",
        f"- Gold claims the **model found but the regex fallback missed: {result['model_only']}**; "
        f"found by regex but missed by the model: {result['regex_only']}.",
        f"- Issue-level verdict, extracted vs. gold claims: same on {_ratio(result['verdict_same'], cases)} cases. "
        f"Per matched claim: same verdict on {_ratio(result['claim_verdict_same'], result['claim_verdict_total'])}.",
        f"- Extraction ran on the model for {llm_cases}/{cases} cases "
        f"(regex fallback on {result['methods'].get('regex_fallback', 0)}). "
        + (f"Time per case: median {statistics.median(secs):.1f} s, max {max(secs):.1f} s." if secs else ""),
        "",
        "## Recall and precision per claim type",
        "",
        "| Type | Gold | Model found | Model extracted | Model precision | Regex found |",
        "|---|---|---|---|---|---|",
    ]
    for t in sorted(set(m.by_type_gold) | set(m.by_type_extracted)):
        lines.append(
            f"| `{t}` | {m.by_type_gold[t]} | {m.by_type_found[t]} | {m.by_type_extracted[t]} | "
            f"{_ratio(m.by_type_extracted_matched[t], m.by_type_extracted[t])} | {r.by_type_found[t]} |"
        )
    lines += [
        "",
        "## Field accuracy on matched claims",
        "",
        "| Field (when the gold claim has it) | Model | Regex |",
        "|---|---|---|",
    ]
    for f_ in ("symbol", "file", "repro"):
        lines.append(f"| `{f_}` | {_ratio(m.field_hits[f_], m.field_totals[f_])} | {_ratio(r.field_hits[f_], r.field_totals[f_])} |")
    lines += ["", "## Guardrails", "", "Claims the model proposed but the guardrails discarded, by reason:", ""]
    if result["drops"]:
        lines += [f"- `{reason}`: {count}" for reason, count in result["drops"].most_common()]
    else:
        lines.append("- none")
    lines += [
        "",
        "## Per case",
        "",
        "| Case | Input text | Method | Time | Gold | Model found / extracted | Regex found / extracted | Dropped | Verdict (gold → extracted) |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for c in result["per_case"]:
        lines.append(
            f"| {c['id']} | {c['text_source']} | {c['method']} | {c['seconds']:.1f} s | {c['gold']} | "
            f"{c['model_found']} / {c['model']} | {c['regex_found']} / {c['regex']} | {c['dropped']} | "
            f"`{c['gold_prediction']}` → `{c['model_prediction']}` |"
        )
    fallback = [c["id"] for c in result["per_case"] if c["text_source"] in ("gold_quotes_fallback", "case_note_fallback")]
    lines += [
        "",
        "## How to read this",
        "",
        "- Gold claims were written by hand from the same texts; they are a reference, not ground truth.",
        "- Small sample: treat percentages as indicative.",
    ]
    if fallback:
        lines.append(
            f"- {len(fallback)} case(s) had no text of their own ({', '.join(fallback)}): the extractor was given a fallback "
            "text built from the case note or gold quotes, which makes matching easier for those cases."
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gold", type=Path, default=HERE / "results.json")
    parser.add_argument("--extracted", type=Path, default=HERE / "results_extracted.json")
    parser.add_argument("--out", type=Path, default=HERE / "EXTRACTION.md")
    args = parser.parse_args(argv)
    gold_rows = json.loads(args.gold.read_text())
    extracted_rows = json.loads(args.extracted.read_text())
    if not any("extraction" in row for row in extracted_rows):
        print("results_extracted.json has no extraction metadata; rerun the benchmark with --mode extracted")
        return 1
    result = compare(gold_rows, extracted_rows)
    args.out.write_text(render(result))
    m, r = result["model"], result["regex"]
    print(f"model recall={_pct(m.recall)} precision={_pct(m.precision)} | regex recall={_pct(r.recall)} "
          f"precision={_pct(r.precision)} | model-only={result['model_only']} regex-only={result['regex_only']}"
          f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
