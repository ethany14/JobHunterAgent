import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { renderTimeline } from "../chrome_extension/assistant/timeline.js";
import {
  renderApprovalCard, renderArtifactCard, renderConfirmationCard,
} from "../chrome_extension/assistant/cards/index.js";
import { createCopilotState, displayLabel } from "../chrome_extension/assistant/state.js";
import { artifactFileName, renderArtifactDocument } from "../chrome_extension/assistant/artifact-rendering.js";

class FakeNode {
  constructor(tag = "div") {
    this.tag = tag; this.textContent = ""; this.children = []; this.dataset = {};
    this.listeners = {}; this.hidden = false; this.value = ""; this.open = false;
  }
  append(...nodes) { this.children.push(...nodes); }
  appendChild(node) { this.children.push(node); }
  removeChild(node) { this.children.splice(this.children.indexOf(node), 1); }
  get firstChild() { return this.children[0] || null; }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  setAttribute(name, value) { this[name] = value; }
  set innerHTML(_) { throw new Error("unsafe HTML rendering"); }
}

function installDocument() {
  globalThis.document = { createElement: tag => new FakeNode(tag) };
}

function values(node) {
  return [node.textContent, ...node.children.flatMap(values)];
}

function activity(type, id, payload = {}, status = "completed") {
  return { activity_id: id, type, status, reference_type: type,
    reference_id: `${id}-reference`, payload };
}

test("conversation renders assistant text plainly and user text as a bubble", () => {
  installDocument();
  const root = new FakeNode();
  renderTimeline(root, [
    activity("assistant_message", "a1", { content: "Plain assistant response" }),
    activity("user_message", "u1", { content: "<img src=x onerror=alert(1)>" }),
  ]);
  assert.equal(root.children[0].className, "conversation-message assistant");
  assert.equal(root.children[1].className, "conversation-message user");
  assert.ok(values(root).includes("<img src=x onerror=alert(1)>"));
});

test("workflow progress is one collapsed human-readable disclosure", () => {
  installDocument();
  const root = new FakeNode();
  renderTimeline(root, [activity("workflow_progress", "w1",
    { label: "resume evidence" }, "succeeded")]);
  assert.equal(root.children.length, 1);
  assert.equal(root.children[0].tag, "details");
  assert.equal(root.children[0].open, false);
  assert.ok(values(root).includes("Application analysis complete"));
  assert.ok(values(root).some(value => value.includes("Resume Evidence: Complete")));
  assert.equal(values(root).some(value => value.includes("succeeded")), false);
});

test("repeated draft actions collapse and use a meaningful label", () => {
  installDocument();
  const root = new FakeNode();
  const first = activity("workflow_progress", "w1",
    { action_type: "generate_application_pack" }, "draft");
  const second = activity("workflow_progress", "w2",
    { action_type: "generate_application_pack" }, "draft");
  first.reference_type = "assistant_action";
  second.reference_type = "assistant_action";
  renderTimeline(root, [first, second]);
  assert.equal(root.children.length, 1);
  assert.equal(values(root).filter(value => value.includes("Cover Letter: Draft")).length, 1);
  assert.equal(values(root).some(value => value.includes("Application Step")), false);
});

test("timeline shows only the newest user-facing artifact", () => {
  installDocument();
  const root = new FakeNode();
  renderTimeline(root, [
    activity("artifact", "technical", { artifact_type: "match_report", version: 1 }, "draft"),
    activity("artifact", "old", { artifact_type: "tailored_resume", version: 1 }, "draft"),
    activity("artifact", "new", { artifact_type: "tailored_resume", version: 2 }, "draft"),
  ]);
  assert.equal(root.children.length, 1);
  assert.ok(values(root).includes("Version 2 • Draft"));
});

test("cover letter rendering excludes evidence and requirement identifiers", () => {
  const rendered = renderArtifactDocument("cover_letter", {
    schema_version: 2,
    greeting: "Dear Hiring Team,",
    paragraphs: [{
      paragraph_type: "evidence",
      text: "I built reliable APIs.",
      evidence_ids: ["secret-evidence-id"],
      evidence_version_ids: ["secret-version-id"],
      target_requirement_ids: ["REQ-secret"],
    }],
    closing: "Sincerely,",
    signer_name: "Candidate Name",
  });
  assert.equal(rendered,
    "Dear Hiring Team,\n\nI built reliable APIs.\n\nSincerely,\nCandidate Name");
  assert.equal(rendered.includes("secret"), false);
  assert.equal(artifactFileName("cover_letter"), "cover-letter.txt");
});

test("application answers and interview reports render useful text without internal IDs", () => {
  const answer = renderArtifactDocument("application_answer", {
    question: "Why this role?",
    answer_blocks: [{ text: "My API work is relevant to this role.",
      evidence_ids: ["secret-evidence-id"] }],
  });
  assert.equal(answer, "My API work is relevant to this role.");
  assert.equal(answer.includes("secret"), false);
  const report = renderArtifactDocument("interview_report", {
    average_scores: { communication: 4 },
    recurring_gaps: ["Add a clearer result."],
    practice_priorities: ["Use the STAR structure."],
    question_answer_summaries: [{ question: "Tell me about a project.",
      answer: "I described an API project.", overall_score: 4,
      question_id: "secret-question-id" }],
  });
  assert.ok(report.includes("Communication 4/5") || report.includes("communication 4/5"));
  assert.ok(report.includes("Use the STAR structure."));
  assert.equal(report.includes("secret-question-id"), false);
});

test("questions are ordinary assistant messages with inline response actions", () => {
  installDocument();
  const root = new FakeNode();
  const question = activity("question", "q1", { content: "What did you personally do?" });
  question.reference_type = "interview_turn";
  renderTimeline(root, [question], { onAction() {} });
  assert.equal(root.children[0].className, "conversation-message assistant");
  assert.ok(values(root).includes("Send"));
  assert.ok(values(root).includes("Skip"));
  assert.ok(values(root).includes("I don’t have this experience"));
});

test("only the latest question in one interview remains actionable", () => {
  installDocument();
  const root = new FakeNode();
  const first = activity("question", "q1", {
    content: "First question", interview_id: "mock-1",
  });
  const second = activity("question", "q2", {
    content: "Second question", interview_id: "mock-1",
  });
  first.reference_type = "mock_question";
  second.reference_type = "mock_question";
  renderTimeline(root, [first, second], { onAction() {} });
  assert.equal(values(root).filter(value => value === "Send").length, 1);
  assert.ok(values(root).includes("First question"));
  assert.ok(values(root).includes("Second question"));
});

test("only artifact approval and confirmation use cards", () => {
  installDocument();
  const artifact = renderArtifactCard(activity("artifact", "r1",
    { artifact_type: "tailored_resume", version: 2, preview: "Short preview" }, "verified"));
  const approval = renderApprovalCard(activity("approval", "p1", { tool_name: "save_file" }));
  const confirmation = renderConfirmationCard(activity("evidence_candidate", "c1",
    { content: "Presented findings to leadership." }));
  assert.equal(artifact.className, "artifact-card");
  assert.equal(approval.className, "approval-card");
  assert.equal(confirmation.className, "confirmation-card");
  assert.ok(values(artifact).includes("Version 2 • Verified"));
});

test("ordinary successful tools collapse into one line", () => {
  installDocument();
  const root = new FakeNode();
  renderTimeline(root, [
    activity("tool_call", "t1", { tool_name: "load_evidence" }),
    activity("tool_call", "t2", { tool_name: "read_job" }),
  ]);
  assert.equal(root.children.length, 1);
  assert.ok(values(root).includes("Used 2 tools"));
});

test("duplicate polling reuses stable DOM nodes and does not duplicate items", () => {
  installDocument();
  const root = new FakeNode();
  const activities = [activity("assistant_message", "stable", { content: "Same result" })];
  renderTimeline(root, activities);
  const first = root.children[0];
  renderTimeline(root, activities);
  assert.equal(root.children.length, 1);
  assert.equal(root.children[0], first);
});

test("central labels hide raw public statuses", () => {
  assert.equal(displayLabel("awaiting_review"), "Ready for your review");
  assert.equal(displayLabel("revising"), "Improving draft");
  assert.equal(displayLabel("failed"), "Couldn’t complete");
});

test("Copilot state is Workspace-scoped", () => {
  const first = createCopilotState(); const second = createCopilotState();
  first.application = { application_id: "one" };
  assert.equal(second.application, null);
});

test("new rendering modules never use innerHTML", () => {
  for (const file of ["assistant/timeline.js", "assistant/cards/index.js",
    "assistant/controller.js", "assistant/state.js", "assistant/artifact-rendering.js",
    "shared/safe-dom.js"]) {
    const source = fs.readFileSync(new URL(`../chrome_extension/${file}`, import.meta.url), "utf8");
    assert.equal(source.includes("innerHTML"), false, file);
  }
});
