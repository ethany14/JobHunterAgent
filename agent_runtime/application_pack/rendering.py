"""Deterministic rendering for already validated application artifacts."""
from __future__ import annotations

from agent_runtime.application_pack.types import ApplicationAnswer, CoverLetter


def render_cover_letter(value: CoverLetter | dict) -> str:
    letter = CoverLetter.model_validate(value)
    blocks = [letter.greeting.strip()]
    blocks.extend(paragraph.text.strip() for paragraph in letter.paragraphs)
    signature = letter.closing.strip()
    if letter.signer_name:
        signature = f"{signature}\n{letter.signer_name.strip()}"
    blocks.append(signature)
    return "\n\n".join(block for block in blocks if block)


def render_application_answer(value: ApplicationAnswer | dict) -> str:
    answer = ApplicationAnswer.model_validate(value)
    return "\n\n".join(block.text.strip() for block in answer.answer_blocks if block.text.strip())


def render_interview_report(value: dict) -> str:
    lines = ["Mock interview report"]
    scores = value.get("average_scores") or {}
    available = [(str(name).replace("_", " ").title(), score)
                 for name, score in scores.items() if score is not None]
    if available:
        lines.append("Scores: " + ", ".join(f"{name} {score}/5" for name, score in available))
    gaps = [str(item).strip() for item in (value.get("recurring_gaps") or []) if str(item).strip()]
    if gaps:
        lines.append("Areas to improve:\n" + "\n".join(f"- {item}" for item in gaps))
    priorities = [str(item).strip() for item in (value.get("practice_priorities") or [])
                  if str(item).strip()]
    if priorities:
        lines.append("Practice priorities:\n" + "\n".join(f"- {item}" for item in priorities))
    summaries = value.get("question_answer_summaries") or []
    for index, item in enumerate(summaries, start=1):
        question = str(item.get("question") or "").strip()
        answer = str(item.get("answer") or "").strip()
        score = item.get("overall_score")
        if question:
            block = [f"Question {index}: {question}"]
            if answer: block.append(f"Answer: {answer}")
            if score is not None: block.append(f"Score: {score}/5")
            lines.append("\n".join(block))
    return "\n\n".join(lines)
