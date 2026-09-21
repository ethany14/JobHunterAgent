import test from "node:test";
import assert from "node:assert/strict";
import { createLearningController, formatEvolutionSummary } from "../chrome_extension/learning-controller.js";

class FakeNode {
  constructor() {
    this.textContent = "";
    this.value = "";
    this.children = [];
    this.dataset = {};
    this.listeners = {};
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  addEventListener(type, listener) { this.listeners[type] = listener; }
  set innerHTML(_) { throw new Error("unsafe HTML rendering"); }
}

test("Learning panel renders candidate text as inert content", async () => {
  const nodes = new Map();
  for (const selector of ["#learning-feedback", "#learning-list", "#learning-message",
    "#learning-submit", "#learning-refresh"]) nodes.set(selector, new FakeNode());
  const root = {
    querySelector: selector => nodes.get(selector),
    querySelectorAll: () => [],
  };
  globalThis.document = { createElement: () => new FakeNode() };
  globalThis.fetch = async () => ({ ok: true, json: async () => ({ candidates: [{
    candidate_id: "candidate-1", candidate_type: "preference_memory",
    proposed_key_or_name: "resume.summary", proposed_content: "<img src=x onerror=alert(1)>",
    scope: "user", occurrence_count: 1, confidence: 0.8,
    status: "ready_for_review", version: 1, applications_represented: [],
  }] }) });
  const controller = createLearningController(root);
  await controller.refresh();
  const card = nodes.get("#learning-list").children[0];
  assert.equal(card.children[1].textContent, "<img src=x onerror=alert(1)>");
});

test("Skill evaluation summary distinguishes gates and forward results", () => {
  const text = formatEvolutionSummary({ summary: {
    baseline: { task_success_rate: 0.5, mean_latency: 1, mean_tokens: 10 },
    candidate: { task_success_rate: 0.75, activation_accuracy: 1,
      false_activation_rate: 0, mean_latency: 2, mean_tokens: 12 },
    hard_gates: { no_permission_escalation: true, no_context_leakage: false },
    forward_test: { passed: false },
  } });
  assert.match(text, /no_context_leakage: FAIL/);
  assert.match(text, /Forward test: failed/);
  assert.equal(formatEvolutionSummary(null), "No completed evaluation.");
});

test("Staged Skill instructions are rendered as inert text", async () => {
  const nodes = new Map();
  for (const selector of ["#learning-feedback", "#learning-list", "#learning-message",
    "#learning-submit", "#learning-refresh"]) nodes.set(selector, new FakeNode());
  const root = { querySelector: selector => nodes.get(selector), querySelectorAll: () => [] };
  globalThis.document = { createElement: () => new FakeNode() };
  globalThis.fetch = async url => {
    const path = String(url);
    const payload = path.endsWith("/staged") ? {
      candidate_id: "skill-1", status: "approved_for_evaluation", version: 1,
      content_hash: "a".repeat(64), instructions: "<script>alert('bad')</script>",
      validation: { valid: true, warnings: [], content_changed: false },
    } : path.endsWith("/api/skills") ? { skills: [] } : { candidates: [{
      candidate_id: "skill-1", candidate_type: "skill", status: "confirmed",
      proposed_key_or_name: "safe-skill", proposed_content: "A procedure",
      scope: "global", occurrence_count: 3, confidence: .9,
      applications_represented: [], version: 2,
    }] };
    return { ok: true, json: async () => payload };
  };
  // The tab selector is supplied by the root's click listener in the real panel.
  // Here the synthetic root exposes one Skill tab to drive the same branch.
  const skillTab = new FakeNode(); skillTab.dataset.kind = "skill";
  root.querySelectorAll = () => [skillTab];
  const driven = createLearningController(root);
  skillTab.listeners.click();
  await new Promise(resolve => setTimeout(resolve, 0));
  const flatten = node => [node.textContent, ...node.children.flatMap(flatten)];
  assert.ok(flatten(nodes.get("#learning-list")).includes("<script>alert('bad')</script>"));
});
