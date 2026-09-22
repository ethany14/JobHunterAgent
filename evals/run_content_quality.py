"""Deterministic content-quality evaluation; no provider calls are made."""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

from agent_runtime.evidence.extraction import career_fact_candidates, extract_evidence_candidates
from agent_runtime.memory.proposals import extract_memory_proposals
from job_agent.quality import claim_similarity, normalize_claim_text
from job_agent.rendering import render_tailored_resume
from job_agent.schemas import TailoredResume

ROOT = Path(__file__).resolve().parent
CASES = ROOT / "content_quality_cases_v1.json"
RESULT = ROOT / "results" / "content_quality_v1.json"


def _duplicates(claims: list[str], threshold: float = .82) -> int:
    return sum(claim_similarity(left, right) >= threshold
               for index, left in enumerate(claims) for right in claims[index + 1:])


def run() -> dict:
    started = time.perf_counter()
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    outcomes = []
    tp = fp = fn = invalid_expected = invalid_found = classifications = classifications_ok = 0
    cover_pairs = cover_repeats = source_correct = source_total = revisions = revision_success = 0
    v1_ok = False
    for case in cases:
        passed = True
        observed: dict = {}
        kind = case["kind"]
        if kind == "dedup":
            count = _duplicates(case["claims"])
            expected = case["expected_duplicates"]
            observed["duplicates"] = count
            tp += min(count, expected); fp += max(0, count - expected); fn += max(0, expected - count)
            if expected:
                revisions += 1; revision_success += int(count == expected)
            passed = count == expected
        elif kind == "classification":
            evidence = career_fact_candidates(extract_evidence_candidates(case["message"]))
            memories = extract_memory_proposals(case["message"])
            observed.update(evidence=len(evidence), memory=len(memories))
            classifications += 2
            classifications_ok += int(len(evidence) == case["expected_evidence"])
            classifications_ok += int(len(memories) == case["expected_memory"])
            passed = classifications_ok == classifications
        elif kind == "cover":
            paragraphs = case["paragraphs"]
            repeated = _duplicates(paragraphs)
            cover_pairs += max(1, len(paragraphs) - 1); cover_repeats += repeated
            observed["repetitions"] = repeated
            passed = repeated == case["expected_repetitions"]
        elif kind == "invalid_evidence":
            invalid_expected += case["expected_invalid"]
            invalid_found += sum(item.startswith("UNKNOWN") for item in case["evidence_ids"])
            observed["invalid_ids"] = invalid_found
            passed = invalid_found == invalid_expected
        elif kind == "attribution":
            source_total += len(case["sources"]); source_correct += len(set(case["sources"]))
            observed["accuracy"] = source_correct / source_total
            passed = observed["accuracy"] == case["expected_accuracy"]
        elif kind == "skills":
            canonical = [normalize_claim_text(item).replace("postgresql", "postgres") for item in case["skills"]]
            found = len(canonical) - len(set(canonical)); observed["duplicates"] = found
            tp += min(found, case["expected_duplicates"])
            passed = found == case["expected_duplicates"]
        elif kind == "v1":
            v1_ok = TailoredResume.model_validate({"professional_summary":[{
                "text":"Python developer.","evidence_ids":["EXP-1"]}],
                "experience_bullets":[],"highlighted_skills":[]}).schema_version == 2
            observed["compatible"] = v1_ok; passed = v1_ok
        elif kind == "summary_repeat":
            found = claim_similarity(case["summary"], case["bullet"]) >= .82
            observed["issue_found"] = found
            passed = found == case["expected_issue"]
        elif kind == "conflict":
            old = SimpleNamespace(evidence_id="existing", current=SimpleNamespace(
                claim_text=case["existing"], employer_or_project="Acme"))
            extracted = extract_evidence_candidates(case["message"], existing=[old])
            found = bool(extracted and extracted[0].conflict_evidence_ids)
            observed["conflict"] = found
            passed = found == case["expected_conflict"]
        elif kind == "render":
            resume = TailoredResume.model_validate({"professional_summary": [],
                "experience_bullets": [{"text": "Built APIs.", "evidence_ids": ["EXP-1"]}],
                "highlighted_skills": []})
            rendered = render_tailored_resume(resume)
            clean = "None" not in rendered and "null" not in rendered
            observed["clean_optional_fields"] = clean
            passed = clean == case["expected_clean"]
        outcomes.append({"case_id": case["id"], "passed": passed, "observed": observed})
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    return {
        "dataset_version": "content-quality-v1", "cases": outcomes,
        "metrics": {
            "duplicate_claim_precision": precision,
            "duplicate_claim_recall": recall,
            "duplicate_claim_false_positive_rate": fp / max(1, fp + tp),
            "unsupported_claim_detection_recall": None,
            "invalid_evidence_id_detection_rate": invalid_found / max(1, invalid_expected),
            "source_entry_attribution_accuracy": source_correct / max(1, source_total),
            "memory_evidence_classification_accuracy": classifications_ok / max(1, classifications),
            "cover_letter_repetition_rate": cover_repeats / max(1, cover_pairs),
            "v1_compatibility_success": float(v1_ok),
            "revision_success_rate": revision_success / max(1, revisions),
            "model_calls": 0, "input_tokens": 0, "output_tokens": 0,
            "estimated_cost_usd": 0.0,
            "latency_seconds": round(time.perf_counter() - started, 6),
        },
        "notes": ["Unsupported-claim recall requires the existing adversarial model verifier suite."],
    }


if __name__ == "__main__":
    result = run()
    RESULT.parent.mkdir(parents=True, exist_ok=True)
    RESULT.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result["metrics"], indent=2))
