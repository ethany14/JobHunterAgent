import test from "node:test";
import assert from "node:assert/strict";
import { createMockInterviewController } from "../chrome_extension/mock-interview-controller.js";

class FakeNode {
  constructor() {
    this.textContent = "";
    this.value = "";
    this.hidden = false;
    this.children = [];
    this.listeners = {};
  }
  append(node) { this.children.push(node); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
}

globalThis.document = { createElement: () => new FakeNode() };

test("mock interview renders question and feedback as inert text", async () => {
  const names = ["mode", "difficulty", "count", "start", "newAttempt", "progress",
    "competency", "question", "answer", "submit", "skip", "save", "resume",
    "end", "cancel", "feedback", "report"];
  const elements = Object.fromEntries(names.map((name) => [name, new FakeNode()]));
  const controller = createMockInterviewController({ elements, onMessage: () => {} });
  const payload = "<img src=x onerror=alert(1)>";
  await controller.render({ interview: { mock_interview_id: "test", status: "awaiting_answer",
    questions_completed: 0, target_question_count: 5 },
    question: { competency: "role_fit", question_text: payload },
    latest_feedback: { overall_score: 3, relevance_score: 3, specificity_score: 3,
      evidence_grounding_score: 3, structure_score: 3, communication_score: 3,
      strengths: [payload], improvement_areas: [], unsupported_or_unclear_claims: [],
      missing_answer_elements: [] } });
  assert.equal(elements.question.textContent, payload);
  assert.equal(elements.feedback.children[1].textContent, `Strengths: ${payload}`);
  assert.equal(elements.answer.hidden, false);
  assert.equal(elements.resume.hidden, true);
});
