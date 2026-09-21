"""Optional paid-model Pack smoke test on synthetic data in an isolated SQLite file.

This does not run during pytest and never reads the user's application database.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from unittest.mock import patch
from uuid import uuid4

from dotenv import dotenv_values
from langchain_core.callbacks import BaseCallbackHandler

from agent_runtime.application_pack.repository import PackRepository
from agent_runtime.application_pack.workflow import (
    ApplicationPackWorkflow, PACK_PROMPT_VERSION, SharedPackModel,
)
from agent_runtime.evidence.repository import CareerEvidenceRepository
from agent_runtime.memory.repository import MemoryRepository
from agent_runtime.security import canonical_json
from agent_runtime.workspace.models import ApplicationArtifactRow
from agent_runtime.workspace.repository import JobWorkspaceRepository
from api.db import create_database, upgrade_database
from job_agent.model import ProviderCompatibleChatOpenAI, create_model as shared_create_model


class UsageCollector(BaseCallbackHandler):
    def __init__(self) -> None:
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.provider_cost_usd = 0.0
        self.has_provider_cost = False

    def on_llm_end(self, response, **kwargs) -> None:
        self.calls += 1
        for generations in response.generations:
            if not generations:
                continue
            message = getattr(generations[0], "message", None)
            usage = getattr(message, "usage_metadata", None) or {}
            self.input_tokens += int(usage.get("input_tokens", 0) or 0)
            self.output_tokens += int(usage.get("output_tokens", 0) or 0)
            metadata = getattr(message, "response_metadata", None) or {}
            if isinstance(metadata.get("cost_usd"), (int, float)):
                self.provider_cost_usd += float(metadata["cost_usd"])
                self.has_provider_cost = True


def run() -> dict:
    project = Path(__file__).resolve().parents[1]
    local = project / ".release-local"
    local.mkdir(exist_ok=True)
    database_file = local / f"application-pack-smoke-{uuid4().hex}.db"
    url = f"sqlite:///{database_file.as_posix()}"
    started = perf_counter()
    result = {"run_metadata": {"timestamp": datetime.now(UTC).isoformat(),
        "dataset_version": "application-pack-v1-live-smoke",
        "prompt_version": PACK_PROMPT_VERSION,
        "model_calls": None, "tokens": None, "estimated_cost_usd": None},
        "items": [], "manual_question": None, "error_code": None}
    database = None
    usage = UsageCollector()
    def instrumented_model(**kwargs):
        return shared_create_model(**kwargs, model_factory=lambda **options:
            ProviderCompatibleChatOpenAI(**options, callbacks=[usage]))
    class DiagnosticModel:
        def __init__(self):
            self.inner = SharedPackModel()
            self.calls = 0
            self.safe_error_codes = []

        def __getattr__(self, name):
            method = getattr(self.inner, name)
            def wrapped(*args):
                self.calls += 1
                try:
                    return method(*args)
                except Exception as exc:
                    self.safe_error_codes.append(type(exc).__name__)
                    raise
            return wrapped

    diagnostic_model = DiagnosticModel()
    try:
        upgrade_database(url)
        database = create_database(url)
        workspace = JobWorkspaceRepository(database.session_factory)
        saved = workspace.save_workspace(
            cleaned_job_description="Seeking a backend engineer with Python API and SQL experience.",
            title="Backend Engineer", company="Example Company")
        app_id = saved.application.application_id
        requirement = {"requirement_id": "REQ-1", "requirement_group_id": "GRP-1",
            "canonical_name": "python", "display_name": "Python", "original_text": "Python API",
            "source_text": "Python API", "atomic_text": "Python API",
            "category": "skill", "verification_mode": "resume_evidence", "level": "required"}
        job = {"title": "Backend Engineer", "summary": "Python API and SQL work.",
            "requirements": [requirement], "responsibilities": []}
        match = {"matches": [{"requirement_id": "REQ-1", "job_skill": "python",
            "requirement_level": "required", "match_status": "matched",
            "resume_evidence": ["Built Python APIs."], "confidence": 1}],
            "explanation": "", "recommendations": [], "missing_required_requirements": [],
            "missing_preferred_requirements": [], "overall_score": 100,
            "score_breakdown": {"overall_score": 100}, "confirmation_requirements": []}
        with database.session_factory.begin() as session:
            for kind, content in (("job_analysis", job), ("match_report", match)):
                session.add(ApplicationArtifactRow(artifact_id=str(uuid4()), application_id=app_id,
                    artifact_type=kind, version=1, status="verified",
                    content_json=canonical_json(content), evidence_ids_json="[]",
                    created_by="synthetic-live-smoke", source_run_id=None,
                    created_at=datetime.now(UTC)))
        evidence = CareerEvidenceRepository(database.session_factory)
        candidate = evidence.create_candidate(category="experience",
            claim_text="Built Python APIs.", source_type="user_attested",
            exact_quote="Built Python APIs.")
        confirmed = evidence.confirm(candidate.evidence_id, candidate.version)
        evidence.link_to_application(confirmed.evidence_id, app_id,
            expected_version=confirmed.version, requirement_id="REQ-1")
        packs = PackRepository(database.session_factory)
        workflow = ApplicationPackWorkflow(packs=packs, workspace=workspace,
            evidence=evidence, memories=MemoryRepository(database.session_factory),
            model=diagnostic_model)
        pack = workflow.create(app_id, expected_version=saved.application.version,
            idempotency_key="live-smoke-pack")
        result["run_metadata"]["model_configuration"] = packs.snapshot(pack.pack_id).model_configuration
        with patch("custom_agent.handlers.create_model", instrumented_model), \
             patch("agent_runtime.application_pack.workflow.create_model", instrumented_model):
            for artifact_type, question in (("tailored_resume", None), ("cover_letter", None),
                                            ("application_answer", "What experience do you have with Python?")):
                item_started = perf_counter()
                item = workflow.generate(pack.pack_id, artifact_type=artifact_type,
                    question=question, max_length=500 if question else None,
                    expected_version=packs.get(pack.pack_id).version,
                    idempotency_key=f"live-{artifact_type}")
                result["items"].append({"artifact_type": artifact_type,
                    "status": item.status.value, "revision_count": item.revision_count,
                    "verification": item.verification.model_dump(mode="json") if item.verification else None,
                    "content": packs.artifact(pack.pack_id, item.pack_item_id),
                    "latency_seconds": round(perf_counter() - item_started, 3)})
        manual = workflow.generate(pack.pack_id, artifact_type="application_answer",
            question="Will you require visa sponsorship?",
            expected_version=packs.get(pack.pack_id).version,
            idempotency_key="live-manual-question")
        result["manual_question"] = {"requires_manual_answer": manual.requires_manual_answer,
                                     "content": packs.artifact(pack.pack_id, manual.pack_item_id)}
    except Exception as exc:
        result["error_code"] = type(exc).__name__
    finally:
        result["elapsed_seconds"] = round(perf_counter() - started, 3)
        result["run_metadata"]["logical_model_calls"] = diagnostic_model.calls
        result["run_metadata"]["model_calls"] = usage.calls
        result["run_metadata"]["tokens"] = {"input": usage.input_tokens,
            "output": usage.output_tokens,
            "total": usage.input_tokens + usage.output_tokens}
        settings = {**dotenv_values(project / ".env"), **os.environ}
        try:
            input_rate = float(settings["LLM_INPUT_COST_PER_MILLION"])
            output_rate = float(settings["LLM_OUTPUT_COST_PER_MILLION"])
            configured_cost = (usage.input_tokens * input_rate +
                               usage.output_tokens * output_rate) / 1_000_000
        except (KeyError, TypeError, ValueError):
            configured_cost = None
        result["run_metadata"]["estimated_cost_usd"] = (
            usage.provider_cost_usd if usage.has_provider_cost else configured_cost)
        result["run_metadata"]["cost_basis"] = (
            "provider_reported" if usage.has_provider_cost else
            "configured_rates" if configured_cost is not None else "unavailable")
        result["model_error_codes"] = diagnostic_model.safe_error_codes
        if database is not None:
            database.close()
        for suffix in ("", "-wal", "-shm"):
            (Path(str(database_file) + suffix)).unlink(missing_ok=True)
    return result


if __name__ == "__main__":
    output = run()
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = Path(__file__).resolve().parent / "results" / f"application_pack_v0.1_live_smoke_{timestamp}.json"
    destination.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"statuses": [item["status"] for item in output["items"]],
        "manual_question": output["manual_question"], "error_code": output["error_code"],
        "elapsed_seconds": output["elapsed_seconds"], "artifact": destination.name}, ensure_ascii=False))
