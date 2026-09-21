"""Transactional, optimistic mock-interview persistence."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from agent_runtime.mock_interview.errors import (
    MockInterviewConflict, MockInterviewNotFound, MockInterviewValidation,
)
from agent_runtime.mock_interview.models import (
    MockAnswerEvaluationRow, MockInterviewAnswerRow, MockInterviewCandidateRow,
    MockInterviewEventRow, MockInterviewPlanItemRow, MockInterviewPlanRow,
    MockInterviewQuestionRow, MockInterviewReportRow, MockInterviewRow,
)
from agent_runtime.mock_interview.types import (
    MockAnswerEvaluation, MockInterviewAnswer, MockInterviewPlan,
    MockInterviewQuestion, MockInterviewSession, MockStatus, PlanItem,
)
from agent_runtime.security import canonical_json


def _utc(value):
    return value.replace(tzinfo=UTC) if value and value.tzinfo is None else value


class MockInterviewRepository:
    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory

    @staticmethod
    def _view(row: MockInterviewRow) -> MockInterviewSession:
        return MockInterviewSession(mock_interview_id=row.mock_interview_id,
            application_id=row.application_id, root_task_id=row.root_task_id,
            agent_session_id=row.agent_session_id, mode=row.mode,
            status=row.status, difficulty=row.difficulty,
            target_question_count=row.target_question_count,
            questions_completed=row.questions_completed,
            max_followups_per_question=row.max_followups_per_question,
            current_question_id=row.current_question_id, version=row.version,
            started_at=_utc(row.started_at), updated_at=_utc(row.updated_at),
            completed_at=_utc(row.completed_at), error_code=row.error_code)

    @staticmethod
    def _require(db: Session, interview_id: str) -> MockInterviewRow:
        row = db.get(MockInterviewRow, interview_id)
        if row is None:
            raise MockInterviewNotFound("Mock interview was not found.")
        return row

    @staticmethod
    def _event(db: Session, row: MockInterviewRow, kind: str,
               key: str | None = None, payload: dict | None = None) -> None:
        row.event_sequence += 1
        db.add(MockInterviewEventRow(event_id=str(uuid4()),
            mock_interview_id=row.mock_interview_id, sequence=row.event_sequence,
            event_type=kind, idempotency_key=key,
            payload_json=canonical_json(payload or {}), occurred_at=datetime.now(UTC)))

    @staticmethod
    def _advance(db: Session, row: MockInterviewRow, expected_version: int,
                 **values) -> None:
        now = datetime.now(UTC)
        changed = db.execute(update(MockInterviewRow).where(
            MockInterviewRow.mock_interview_id == row.mock_interview_id,
            MockInterviewRow.version == expected_version).values(
            version=expected_version + 1, updated_at=now, **values))
        if changed.rowcount != 1:
            raise MockInterviewConflict("Interview version changed.")
        db.refresh(row)

    def create(self, session: MockInterviewSession, plan: MockInterviewPlan,
               *, idempotency_key: str) -> MockInterviewSession:
        if plan.mock_interview_id != session.mock_interview_id:
            raise MockInterviewValidation("Plan and interview differ.")
        try:
            return self._create_transaction(session, plan, idempotency_key)
        except IntegrityError as exc:
            message = str(exc.orig).lower()
            if ("mock_interviews.application_id" in message
                    and "mock_interviews.mode" in message):
                raise MockInterviewConflict("An active interview already exists for this mode.") from exc
            raise

    def _create_transaction(self, session: MockInterviewSession,
                            plan: MockInterviewPlan, key: str) -> MockInterviewSession:
        with self._factory.begin() as db:
            active = db.scalar(select(MockInterviewRow).where(
                MockInterviewRow.application_id == session.application_id,
                MockInterviewRow.mode == session.mode.value,
                MockInterviewRow.status.in_(["planning", "awaiting_answer", "evaluating", "failed"])))
            if active is not None:
                raise MockInterviewConflict("An active interview already exists for this mode.")
            db.add(MockInterviewRow(mock_interview_id=session.mock_interview_id,
                application_id=session.application_id, root_task_id=session.root_task_id,
                agent_session_id=session.agent_session_id, mode=session.mode.value,
                status=session.status.value, difficulty=session.difficulty.value,
                target_question_count=session.target_question_count,
                questions_completed=0,
                max_followups_per_question=session.max_followups_per_question,
                current_question_id=None, version=1, event_sequence=0,
                answer_sequence=0, started_at=session.started_at,
                updated_at=session.updated_at))
            db.flush()
            digest = hashlib.sha256(plan.model_dump_json().encode()).hexdigest()
            db.add(MockInterviewPlanRow(plan_id=plan.plan_id,
                mock_interview_id=session.mock_interview_id,
                manifest_json=plan.model_dump_json(), content_hash=digest,
                created_at=plan.created_at))
            db.flush()
            for item in plan.items:
                db.add(MockInterviewPlanItemRow(plan_item_id=item.plan_item_id,
                    plan_id=plan.plan_id, sequence=item.sequence,
                    content_json=item.model_dump_json(), status="pending"))
            db.flush()
            self._event(db, self._require(db, session.mock_interview_id),
                "CREATED", key,
                {"plan_id": plan.plan_id, "mode": session.mode.value})
            return self._view(self._require(db, session.mock_interview_id))

    def get(self, interview_id: str) -> MockInterviewSession:
        with self._factory() as db:
            return self._view(self._require(db, interview_id))

    def active(self, application_id: str, mode: str | None = None) -> MockInterviewSession | None:
        with self._factory() as db:
            query = select(MockInterviewRow).where(
                MockInterviewRow.application_id == application_id,
                MockInterviewRow.status.in_(["planning", "awaiting_answer", "evaluating", "failed"]))
            if mode:
                query = query.where(MockInterviewRow.mode == mode)
            row = db.scalar(query.order_by(MockInterviewRow.started_at.desc()))
            return self._view(row) if row else None

    def latest(self, application_id: str) -> MockInterviewSession | None:
        with self._factory() as db:
            row = db.scalar(select(MockInterviewRow).where(
                MockInterviewRow.application_id == application_id).order_by(
                MockInterviewRow.started_at.desc()))
            return self._view(row) if row else None

    def by_start_key(self, application_id: str, key: str) -> MockInterviewSession | None:
        with self._factory() as db:
            row = db.scalar(select(MockInterviewRow).join(MockInterviewEventRow,
                MockInterviewEventRow.mock_interview_id == MockInterviewRow.mock_interview_id).where(
                MockInterviewRow.application_id == application_id,
                MockInterviewEventRow.event_type == "CREATED",
                MockInterviewEventRow.idempotency_key == key))
            return self._view(row) if row else None

    def plan(self, interview_id: str) -> MockInterviewPlan:
        with self._factory() as db:
            self._require(db, interview_id)
            row = db.scalar(select(MockInterviewPlanRow).where(
                MockInterviewPlanRow.mock_interview_id == interview_id))
            return MockInterviewPlan.model_validate_json(row.manifest_json)

    def plan_items(self, interview_id: str) -> list[PlanItem]:
        with self._factory() as db:
            self._require(db, interview_id)
            rows = db.scalars(select(MockInterviewPlanItemRow).join(MockInterviewPlanRow).where(
                MockInterviewPlanRow.mock_interview_id == interview_id).order_by(
                MockInterviewPlanItemRow.sequence)).all()
            return [PlanItem.model_validate({**json.loads(row.content_json), "status": row.status})
                    for row in rows]

    def question(self, question_id: str) -> MockInterviewQuestion:
        with self._factory() as db:
            row = db.get(MockInterviewQuestionRow, question_id)
            if row is None:
                raise MockInterviewNotFound("Question was not found.")
            return MockInterviewQuestion.model_validate_json(row.content_json)

    def questions(self, interview_id: str) -> list[MockInterviewQuestion]:
        with self._factory() as db:
            self._require(db, interview_id)
            rows = db.scalars(select(MockInterviewQuestionRow).where(
                MockInterviewQuestionRow.mock_interview_id == interview_id).order_by(
                MockInterviewQuestionRow.created_at)).all()
            return [MockInterviewQuestion.model_validate_json(row.content_json) for row in rows]

    def answer_for_question(self, question_id: str) -> MockInterviewAnswer | None:
        with self._factory() as db:
            row = db.scalar(select(MockInterviewAnswerRow).where(
                MockInterviewAnswerRow.question_id == question_id))
            return self._answer(row) if row else None

    def answer_by_key(self, interview_id: str, key: str) -> MockInterviewAnswer | None:
        with self._factory() as db:
            row = db.scalar(select(MockInterviewAnswerRow).where(
                MockInterviewAnswerRow.mock_interview_id == interview_id,
                MockInterviewAnswerRow.idempotency_key == key))
            return self._answer(row) if row else None

    @staticmethod
    def _answer(row: MockInterviewAnswerRow) -> MockInterviewAnswer:
        return MockInterviewAnswer(answer_id=row.answer_id, question_id=row.question_id,
            sequence=row.sequence, original_text=row.original_text,
            idempotency_key=row.idempotency_key, submitted_at=_utc(row.submitted_at))

    def turns(self, interview_id: str) -> list[dict]:
        with self._factory() as db:
            self._require(db, interview_id)
            questions = db.scalars(select(MockInterviewQuestionRow).where(
                MockInterviewQuestionRow.mock_interview_id == interview_id).order_by(
                MockInterviewQuestionRow.created_at)).all()
            output = []
            for row in questions:
                answer = db.scalar(select(MockInterviewAnswerRow).where(
                    MockInterviewAnswerRow.question_id == row.question_id))
                evaluation = (db.scalar(select(MockAnswerEvaluationRow).where(
                    MockAnswerEvaluationRow.answer_id == answer.answer_id)) if answer else None)
                output.append({"question": json.loads(row.content_json),
                    "answer": self._answer(answer).model_dump(mode="json") if answer else None,
                    "evaluation": json.loads(evaluation.content_json) if evaluation else None})
            return output

    def save_question(self, interview_id: str, question: MockInterviewQuestion,
                      expected_version: int) -> MockInterviewSession:
        with self._factory.begin() as db:
            row = self._require(db, interview_id)
            if row.version != expected_version or row.status not in {"planning", "failed"}:
                raise MockInterviewConflict("Question generation is not current.")
            item = db.get(MockInterviewPlanItemRow, question.plan_item_id)
            if item is None:
                raise MockInterviewValidation("Question is outside the plan.")
            db.add(MockInterviewQuestionRow(question_id=question.question_id,
                mock_interview_id=interview_id, plan_item_id=question.plan_item_id,
                parent_question_id=question.parent_question_id,
                content_json=question.model_dump_json(), created_at=question.created_at))
            item.status = "current"
            self._advance(db, row, expected_version,
                status=MockStatus.AWAITING_ANSWER.value,
                current_question_id=question.question_id, error_code=None)
            self._event(db, row, "QUESTION_ASKED", payload={"question_id": question.question_id,
                "plan_item_id": question.plan_item_id})
            return self._view(row)

    def save_answer(self, interview_id: str, question_id: str, text: str,
                    expected_version: int, key: str) -> tuple[MockInterviewAnswer, bool]:
        with self._factory.begin() as db:
            row = self._require(db, interview_id)
            previous = db.scalar(select(MockInterviewAnswerRow).where(
                MockInterviewAnswerRow.mock_interview_id == interview_id,
                MockInterviewAnswerRow.idempotency_key == key))
            if previous:
                if previous.question_id != question_id or previous.original_text != text:
                    raise MockInterviewConflict("Idempotency key was reused with different content.")
                return self._answer(previous), True
            if row.version != expected_version or row.status != "awaiting_answer" or row.current_question_id != question_id:
                raise MockInterviewConflict("Question or version changed.")
            row.answer_sequence += 1
            answer = MockInterviewAnswer(answer_id=str(uuid4()), question_id=question_id,
                sequence=row.answer_sequence, original_text=text, idempotency_key=key)
            db.add(MockInterviewAnswerRow(answer_id=answer.answer_id,
                mock_interview_id=interview_id, question_id=question_id,
                sequence=answer.sequence, original_text=text,
                idempotency_key=key, submitted_at=answer.submitted_at))
            self._advance(db, row, expected_version, status="evaluating")
            self._event(db, row, "ANSWER_SAVED", key,
                {"answer_id": answer.answer_id, "question_id": question_id})
            return answer, False

    def evaluation(self, answer_id: str) -> MockAnswerEvaluation | None:
        with self._factory() as db:
            row = db.scalar(select(MockAnswerEvaluationRow).where(
                MockAnswerEvaluationRow.answer_id == answer_id))
            return MockAnswerEvaluation.model_validate_json(row.content_json) if row else None

    def save_evaluation(self, interview_id: str, evaluation: MockAnswerEvaluation,
                        *, expected_version: int, followup: bool) -> MockInterviewSession:
        with self._factory.begin() as db:
            row = self._require(db, interview_id)
            if row.version != expected_version or row.status not in {"evaluating", "failed"}:
                raise MockInterviewConflict("Evaluation is not current.")
            answer = db.get(MockInterviewAnswerRow, evaluation.answer_id)
            if answer is None or answer.mock_interview_id != interview_id or answer.question_id != row.current_question_id:
                raise MockInterviewValidation("Evaluation belongs to another answer.")
            if db.scalar(select(MockAnswerEvaluationRow).where(
                    MockAnswerEvaluationRow.answer_id == answer.answer_id)):
                raise MockInterviewConflict("Answer was already evaluated.")
            question = db.get(MockInterviewQuestionRow, answer.question_id)
            item = db.get(MockInterviewPlanItemRow, question.plan_item_id)
            db.add(MockAnswerEvaluationRow(evaluation_id=evaluation.evaluation_id,
                answer_id=answer.answer_id, content_json=evaluation.model_dump_json(),
                created_at=evaluation.created_at))
            if not followup:
                item.status = "completed"
            self._advance(db, row, expected_version, status="planning",
                current_question_id=(question.question_id if followup else None),
                questions_completed=row.questions_completed + (0 if followup else 1),
                error_code=None)
            self._event(db, row, "EVALUATION_SAVED", payload={
                "answer_id": answer.answer_id, "followup": followup})
            return self._view(row)

    def skip(self, interview_id: str, expected_version: int, key: str) -> MockInterviewSession:
        with self._factory.begin() as db:
            row = self._require(db, interview_id)
            event = db.scalar(select(MockInterviewEventRow).where(
                MockInterviewEventRow.mock_interview_id == interview_id,
                MockInterviewEventRow.idempotency_key == key))
            if event:
                if event.event_type != "QUESTION_SKIPPED":
                    raise MockInterviewConflict("Idempotency key was reused.")
                return self._view(row)
            if row.version != expected_version or row.status != "awaiting_answer" or not row.current_question_id:
                raise MockInterviewConflict("Question or version changed.")
            question = db.get(MockInterviewQuestionRow, row.current_question_id)
            item = db.get(MockInterviewPlanItemRow, question.plan_item_id)
            item.status = "skipped"
            self._advance(db, row, expected_version, status="planning",
                current_question_id=None, questions_completed=row.questions_completed + 1)
            self._event(db, row, "QUESTION_SKIPPED", key,
                {"question_id": question.question_id})
            return self._view(row)

    def change_status(self, interview_id: str, expected_version: int,
                      status: MockStatus, kind: str, key: str | None = None,
                      error_code: str | None = None) -> MockInterviewSession:
        with self._factory.begin() as db:
            row = self._require(db, interview_id)
            if key:
                event = db.scalar(select(MockInterviewEventRow).where(
                    MockInterviewEventRow.mock_interview_id == interview_id,
                    MockInterviewEventRow.idempotency_key == key))
                if event:
                    if event.event_type != kind:
                        raise MockInterviewConflict("Idempotency key was reused.")
                    return self._view(row)
            if row.version != expected_version:
                raise MockInterviewConflict("Interview version changed.")
            self._advance(db, row, expected_version, status=status.value,
                error_code=error_code,
                **({"completed_at": datetime.now(UTC)} if status in {
                    MockStatus.COMPLETED, MockStatus.CANCELLED} else {}))
            self._event(db, row, kind, key)
            return self._view(row)

    def save_report(self, interview_id: str, report: dict,
                    expected_version: int, key: str) -> dict:
        with self._factory.begin() as db:
            row = self._require(db, interview_id)
            existing = db.scalar(select(MockInterviewReportRow).where(
                MockInterviewReportRow.mock_interview_id == interview_id))
            if existing:
                return json.loads(existing.content_json)
            if row.version != expected_version or row.status not in {"planning", "awaiting_answer", "failed"}:
                raise MockInterviewConflict("Interview cannot be finalized now.")
            content = canonical_json(report)
            db.add(MockInterviewReportRow(report_id=str(uuid4()),
                mock_interview_id=interview_id, application_id=row.application_id,
                artifact_type="interview_report", content_json=content,
                content_hash=hashlib.sha256(content.encode()).hexdigest(),
                created_at=datetime.now(UTC)))
            self._advance(db, row, expected_version, status="completed",
                completed_at=datetime.now(UTC), current_question_id=None)
            self._event(db, row, "COMPLETED", key, {"report_created": True})
            return report

    def report(self, interview_id: str) -> dict | None:
        with self._factory() as db:
            self._require(db, interview_id)
            row = db.scalar(select(MockInterviewReportRow).where(
                MockInterviewReportRow.mock_interview_id == interview_id))
            return json.loads(row.content_json) if row else None

    def add_candidate_link(self, interview_id: str, answer_id: str, evidence_id: str) -> None:
        with self._factory.begin() as db:
            if db.get(MockInterviewCandidateRow, (interview_id, answer_id, evidence_id)):
                return
            db.add(MockInterviewCandidateRow(mock_interview_id=interview_id,
                answer_id=answer_id, evidence_id=evidence_id))

    def candidate_ids(self, interview_id: str) -> list[str]:
        with self._factory() as db:
            self._require(db, interview_id)
            return list(db.scalars(select(MockInterviewCandidateRow.evidence_id).where(
                MockInterviewCandidateRow.mock_interview_id == interview_id)).all())

    def candidate_for_answer(self, answer_id: str) -> str | None:
        with self._factory() as db:
            return db.scalar(select(MockInterviewCandidateRow.evidence_id).where(
                MockInterviewCandidateRow.answer_id == answer_id))
