"""Deterministic Pack safety baseline; no model call or paid token estimate."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

from agent_runtime.application_pack.policy import classify_question, evidence_set_hash, requires_manual_answer
from agent_runtime.application_pack.types import CoverLetter, EvidenceSnapshotItem, GroundedBlock
from agent_runtime.application_pack.verifier import ArtifactVerifier

ROOT = Path(__file__).resolve().parent


def run() -> dict:
    cases = json.loads((ROOT / "application_pack_cases.json").read_text(encoding="utf-8"))
    results = []
    for case in cases:
        started = perf_counter()
        if case["kind"] == "question":
            actual = requires_manual_answer(classify_question(case["question"]))
            expected = case["expected_manual"]
        elif case["kind"] == "staleness":
            def digest(claim):
                evidence = EvidenceSnapshotItem(evidence_id="EV-1", evidence_version_id="VER-1",
                    content_hash=claim, source_type="resume", selection_reason="application_link",
                    associated_requirement_ids=[], claim_text=claim, status_at_selection="confirmed")
                return evidence_set_hash([evidence], job_snapshot_id="job-1", preferences=[], prompt_version="application-pack-v1")
            actual = digest(case["old_evidence"]) != digest(case["new_evidence"])
            expected = case["expected_stale"]
        else:
            evidence = EvidenceSnapshotItem(evidence_id="EV-1", evidence_version_id="VER-1",
                content_hash="sha256-test", source_type="resume", selection_reason="application_link",
                associated_requirement_ids=[], claim_text=case["evidence"], status_at_selection="confirmed")
            valid = case["citation"] == "valid"
            block = GroundedBlock(block_id="claim-1", text=case["claim"], block_type="factual",
                evidence_ids=["EV-1" if valid else "JD-UNKNOWN"],
                evidence_version_ids=["VER-1"] if valid else [])
            artifact = CoverLetter(blocks=[block]).model_dump(mode="json")
            verdict = ArtifactVerifier().verify(artifact, "cover_letter", [evidence],
                max_length=case.get("max_length"))
            actual, expected = verdict.passed, case["expected_pass"]
        results.append({"case_id": case["id"], "kind": case["kind"],
            "label": case.get("label"), "expected": expected, "actual": actual,
            "passed": actual == expected,
            "latency_seconds": round(perf_counter() - started, 6)})
    unsupported = [row for row in results if row["label"] == "unsupported"]
    supported = [row for row in results if row["label"] == "supported"]
    invalid_citations = [row for row in results if row["case_id"] in {
        "injected_jd_or_question", "injected_evidence_instruction"}]
    return {"run_metadata": {"timestamp": datetime.now(UTC).isoformat(),
        "dataset_version": "application-pack-v1", "evaluator": "deterministic-artifact-verifier",
        "model_calls": 0}, "results": results, "metrics": {
        "cases": len(results), "expected_behavior_achieved": sum(row["passed"] for row in results),
        "unsupported_claim_detection_recall": sum(not row["actual"] for row in unsupported) / len(unsupported),
        "supported_claim_false_positive_rate": sum(not row["actual"] for row in supported) / len(supported),
        "invalid_citation_acceptance_rate": sum(row["actual"] for row in invalid_citations) / len(invalid_citations),
        "jd_as_evidence_violations": sum(row["actual"] for row in invalid_citations),
        "candidate_evidence_leakage": None,
        "length_compliance": next(row["passed"] for row in results if row["case_id"] == "character_limit"),
        "restricted_question_refusal_accuracy": sum(row["passed"] for row in results if row["kind"] == "question") / sum(row["kind"] == "question" for row in results),
        "revision_success_rate": None, "preference_adherence": None,
        "mean_generation_latency_seconds": None, "mean_model_tokens": None,
        "mean_estimated_cost_usd": None, "restart_recovery_success": None,
    }}


if __name__ == "__main__":
    result = run()
    target = ROOT / "results" / "application_pack_v0.1_deterministic.json"
    target.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{result['metrics']['expected_behavior_achieved']}/{result['metrics']['cases']} expected behaviors; {target}")
