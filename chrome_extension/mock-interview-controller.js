"use strict";

import {
  getActiveMockInterview, getMockInterview, getMockInterviewCandidates,
  getMockInterviewReport, mutateCareerEvidence,
  mutateMockInterview, startMockInterview,
} from "./api-client.js";

// All model, source and error strings are rendered with textContent.
export function createMockInterviewController({ elements, onMessage }) {
  let applicationId = null;
  let view = null;
  let busy = false;
  let retry = null;
  const line = (parent, value) => {
    const node = document.createElement("p");
    node.textContent = String(value ?? "");
    parent.append(node);
  };
  function keyFor(action, payload) {
    const signature = JSON.stringify([view?.interview?.mock_interview_id,
      view?.interview?.version, action, payload]);
    if (retry?.signature !== signature) retry = { signature, key: crypto.randomUUID() };
    return retry.key;
  }
  async function render(next) {
    view = next?.interview ? next : null;
    const state = view?.interview;
    const waiting = state?.status === "awaiting_answer";
    elements.progress.textContent = state
      ? `${state.status.replaceAll("_", " ")} · ${state.questions_completed}/${state.target_question_count} main questions`
      : "No active mock interview.";
    elements.competency.textContent = view?.question
      ? `Competency: ${view.question.competency.replaceAll("_", " ")}` : "";
    elements.question.textContent = view?.question?.question_text || "";
    for (const node of [elements.answer, elements.submit, elements.skip]) node.hidden = !waiting;
    for (const node of [elements.end, elements.cancel, elements.save]) {
      node.hidden = !state || ["completed", "cancelled"].includes(state.status);
    }
    elements.resume.hidden = !["failed", "planning", "evaluating"].includes(state?.status);
    elements.start.hidden = Boolean(state);
    elements.newAttempt.hidden = !state || !["completed", "cancelled"].includes(state.status);
    elements.feedback.replaceChildren();
    const feedback = view?.latest_feedback;
    if (feedback) {
      line(elements.feedback, `Overall ${feedback.overall_score}/5 · Relevance ${feedback.relevance_score}/5 · Specificity ${feedback.specificity_score}/5 · Evidence ${feedback.evidence_grounding_score}/5 · Structure ${feedback.structure_score}/5 · Communication ${feedback.communication_score}/5`);
      for (const [label, values] of [["Strengths", feedback.strengths],
        ["Improve", feedback.improvement_areas],
        ["Unclear claims", feedback.unsupported_or_unclear_claims],
        ["Missing elements", feedback.missing_answer_elements]]) {
        if (values?.length) line(elements.feedback, `${label}: ${values.join("; ")}`);
      }
      if (feedback.suggested_structure) line(elements.feedback,
        `Suggested structure: ${feedback.suggested_structure}`);
    }
    elements.report.replaceChildren();
    if (state?.status === "completed") {
      const result = await getMockInterviewReport(state.mock_interview_id);
      const report = result.report;
      if (report) {
        line(elements.report, `Covered: ${(report.covered_competencies || []).join(", ")}`);
        line(elements.report, `Practice priorities: ${(report.practice_priorities || []).join("; ") || "None recorded"}`);
        line(elements.report, `Average overall score: ${report.average_scores?.overall ?? "N/A"}/5`);
      }
      const candidates = await getMockInterviewCandidates(state.mock_interview_id);
      for (const id of candidates.evidence_candidate_ids || []) {
        const item = (candidates.items || []).find((entry) => entry.evidence_id === id);
        const card = document.createElement("div");
        card.className = "context-item";
        line(card, `Discovered during mock interview: ${item?.current?.claim_text || id}`);
        line(card, `Status: ${item?.status || "unavailable"}. Review the original answer before confirming.`);
        if (item?.status === "candidate") {
          for (const action of ["confirm", "reject"]) {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "secondary-button";
            button.textContent = action === "confirm" ? "Confirm evidence" : "Reject evidence";
            button.addEventListener("click", async () => {
              try {
                await mutateCareerEvidence(id, action, item.version);
                await render(await getMockInterview(state.mock_interview_id));
              } catch (error) { onMessage(error.message || "Evidence decision failed.", "error"); }
            });
            card.append(button);
          }
        }
        elements.report.append(card);
      }
    }
  }
  async function load(id) {
    applicationId = id;
    try { await render(await getActiveMockInterview(id)); }
    catch (error) { onMessage(error.message || "Could not restore mock interview.", "error"); }
  }
  async function start(newAttempt = false) {
    if (busy || !applicationId) return;
    busy = true;
    try {
      const count = Number(elements.count.value);
      if (!Number.isInteger(count) || count < 3 || count > 12) {
        onMessage("Choose 3 to 12 questions.", "error"); return;
      }
      await render(await startMockInterview(applicationId, {
        mode: elements.mode.value, difficulty: elements.difficulty.value,
        target_question_count: count, max_followups_per_question: 1,
        idempotency_key: crypto.randomUUID(), new_attempt: newAttempt,
      }));
    } catch (error) { onMessage(error.message || "Could not start mock interview.", "error"); }
    finally { busy = false; }
  }
  async function mutate(action, payload = {}) {
    if (busy || !view?.interview) return;
    const state = view.interview;
    busy = true;
    try {
      await render(await mutateMockInterview(state.mock_interview_id, action,
        state.version, keyFor(action, payload), payload));
      retry = null;
      if (action === "answers") elements.answer.value = "";
    } catch (error) {
      if (error.status === 409) {
        await render(await getMockInterview(state.mock_interview_id));
        retry = null;
        onMessage("Interview changed elsewhere. Refresh before submitting again.", "error");
      } else onMessage(error.message || "Interview action failed.", "error");
    } finally { busy = false; }
  }
  elements.start.addEventListener("click", () => start());
  elements.newAttempt.addEventListener("click", () => start(true));
  elements.submit.addEventListener("click", () => {
    const answer = elements.answer.value.trim();
    if (answer) mutate("answers", { answer });
    else onMessage("Enter an answer first.", "error");
  });
  elements.skip.addEventListener("click", () => mutate("skip"));
  elements.end.addEventListener("click", () => mutate("end"));
  elements.cancel.addEventListener("click", () => mutate("cancel"));
  elements.resume.addEventListener("click", () => mutate("resume"));
  elements.save.addEventListener("click", () => onMessage(
    "Saved. Open this Application later to continue.", "success"));
  return { load, render };
}
