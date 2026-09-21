"""Deterministic synthetic routing baseline; no model calls or source rewriting."""
import json
from time import perf_counter
from datetime import UTC, datetime
from pathlib import Path

from agent_runtime.feedback.policy import classify, signal_strength
from agent_runtime.feedback.types import FeedbackEvent, FeedbackSourceType, FeedbackProcessStatus
from agent_runtime.feedback.repository import FeedbackRepository
from agent_runtime.feedback.service import FeedbackService
from api.db import create_database


def run():
    root = Path(__file__).resolve().parent
    cases = json.loads((root / "feedback_learning_v0.1.json").read_text(encoding="utf-8"))
    results = []
    for item in cases:
        started = perf_counter()
        source = FeedbackSourceType(item["source_type"])
        event = FeedbackEvent(owner_id="synthetic-local", source_type=source,
            source_action_id=item["id"], original_content=item["text"],
            before_content=item.get("before"), application_id=item.get("application_id"),
            signal_strength=signal_strength(source, item["text"]))
        result = classify(event)
        results.append({"case_id": item["id"], "expected_type": item["expected_type"],
            "actual_type": result.candidate_type.value,
            "proposed_content": result.proposed_content,
            "correct": result.candidate_type.value == item["expected_type"],
            "requires_user_confirmation": result.requires_user_confirmation,
            "latency_seconds": round(perf_counter() - started, 6)})
    non_skill = [r for r in results if r["expected_type"] != "skill"]
    non_memory = [r for r in results if r["expected_type"] != "preference_memory"]
    evidence = [r for r in results if r["expected_type"] == "career_evidence"]
    policy = [r for r in results if r["expected_type"] == "product_policy"]
    database = create_database("sqlite:///:memory:", create_schema_for_tests=True)
    service = FeedbackService(FeedbackRepository(database.session_factory))
    common = dict(owner_id="synthetic-local", source_type=FeedbackSourceType.EXPLICIT_INSTRUCTION,
        source_action_id="duplicate", original_content="Keep my resume summary to no more than two sentences.")
    _, first = service.record(**common)
    _, replay = service.record(**common)
    duplicate_aggregation_rate = 1.0 if first.occurrence_count == replay.occurrence_count == 1 else 0.0
    _, changed = service.record(owner_id="synthetic-local",
        source_type=FeedbackSourceType.EXPLICIT_INSTRUCTION, source_action_id="change",
        original_content="I now prefer four sentences in my resume summary for all applications.")
    conflict_detection_recall = 1.0 if first.candidate_id in [item.candidate_id for item in
        service.repository.conflicts(changed.candidate_id, owner_id="synthetic-local")] else 0.0
    pending = service.repository.ingest(FeedbackEvent(owner_id="synthetic-local",
        source_type=FeedbackSourceType.EXPLICIT_INSTRUCTION,
        source_action_id="recovery", original_content="Use concise bullet points.",
        signal_strength=signal_strength(FeedbackSourceType.EXPLICIT_INSTRUCTION)))
    staged, attempt_id = service.repository.begin_attempt(pending.feedback_event_id,
        owner_id="synthetic-local", max_attempts=2)
    staged = service.repository.stage(pending.feedback_event_id, owner_id="synthetic-local",
        attempt_id=attempt_id, expected_version=staged.processing_version,
        status=FeedbackProcessStatus.NORMALIZED)
    service.processor.process(pending.feedback_event_id, owner_id="synthetic-local")
    processing_recovery_success = 1.0 if service.repository.get(
        pending.feedback_event_id, owner_id="synthetic-local").processed_status == FeedbackProcessStatus.COMPLETED else 0.0
    database.close()
    payload = {"timestamp": datetime.now(UTC).isoformat(), "dataset_version": "feedback-v0.1",
        "model_calls": 0, "tokens": 0, "estimated_cost_usd": 0,
        "classification_accuracy": sum(r["correct"] for r in results) / len(results),
        "false_skill_candidate_rate": sum(r["actual_type"] == "skill" for r in non_skill) / len(non_skill),
        "false_memory_candidate_rate": sum(r["actual_type"] == "preference_memory" for r in non_memory) / len(non_memory),
        "evidence_routing_accuracy": sum(r["correct"] for r in evidence) / len(evidence),
        "policy_routing_accuracy": sum(r["correct"] for r in policy) / len(policy),
        "confirmation_requirement_compliance": sum(r["requires_user_confirmation"] for r in results) / len(results),
        "pii_leakage_cases": sum(r["actual_type"] == "skill" and
            "alex@example.com" in r["proposed_content"] for r in results),
        "mean_latency_seconds": sum(r["latency_seconds"] for r in results) / len(results),
        "duplicate_aggregation_rate": duplicate_aggregation_rate,
        "conflict_detection_recall": conflict_detection_recall,
        "processing_recovery_success": processing_recovery_success,
        "results": results}
    target = root / "results" / "feedback_learning_v0.1.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"cases": len(results), "classification_accuracy": payload["classification_accuracy"]}))


if __name__ == "__main__":
    run()
