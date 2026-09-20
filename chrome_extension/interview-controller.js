"use strict";

import {
  decideInterviewCandidate, getActiveInterview, getInterview,
  mutateInterview, startInterview,
} from "./api-client.js";

export function createInterviewController({ elements, onMessage }) {
  let applicationId = null;
  let view = null;
  let busy = false;
  let retry = null;
  function mutationKey(kind, version, payload) {
    const signature = JSON.stringify([view?.interview?.interview_session_id, kind, version, payload]);
    if (retry?.signature !== signature) retry = { signature, key: crypto.randomUUID() };
    return retry.key;
  }
  const text = (node, value) => { node.textContent = value || ""; };
  const button = (label, action) => {
    const node = document.createElement("button");
    node.type = "button";
    node.className = "secondary-button";
    node.textContent = label;
    node.addEventListener("click", action);
    return node;
  };

  function render(next) {
    view = next?.interview ? next : null;
    const interview = view?.interview;
    const waiting = interview?.status === "awaiting_answer";
    const candidate = interview?.status === "awaiting_evidence_confirmation" ? view.candidate : null;
    text(elements.progress, interview
      ? `${interview.status.replaceAll("_", " ")} · ${interview.questions_asked}/${interview.max_questions} questions · ${view.completed_requirements} completed · ${view.remaining_requirements} remaining`
      : "No active evidence interview.");
    text(elements.requirement, view?.current_assessment
      ? `Requirement: ${view.current_assessment.original_requirement_text}` : "");
    text(elements.question, waiting ? view.question : "");
    elements.answer.hidden = !waiting;
    elements.submit.hidden = !waiting;
    elements.noExperience.hidden = !waiting;
    elements.skip.hidden = !waiting && !candidate;
    elements.cancel.hidden = !interview || ["completed", "cancelled"].includes(interview.status);
    elements.recover.hidden = interview?.status !== "failed";
    elements.candidate.replaceChildren();
    if (candidate) {
      const card = document.createElement("div");
      card.className = "context-item";
      for (const value of ["Pending candidate", candidate.claim_text,
        `Original answer: ${candidate.original_answer}`,
        `Requirement: ${view.current_assessment?.original_requirement_text || ""}`,
        `Category: ${candidate.category} · Provenance: Interview`,
        `Technologies: ${(candidate.technologies || []).join(", ")}`,
        `Metrics: ${(candidate.metrics || []).map((m) => m.original_text).join(", ")}`,
        candidate.warning]) {
        const line = document.createElement("p");
        line.textContent = value;
        card.append(line);
      }
      const edit = document.createElement("textarea");
      edit.rows = 3;
      edit.maxLength = 5000;
      edit.value = candidate.claim_text;
      card.append(edit,
        button("Confirm exactly", () => candidateAction("confirm")),
        button("Edit and confirm", () => candidateAction("confirm", edit.value.trim())),
        button("Reject", () => candidateAction("reject")),
        button("Mark no experience", () => action("confirm-gap")));
      elements.candidate.append(card);
    }
    elements.statusList.replaceChildren();
    for (const item of view?.assessments || []) {
      const line = document.createElement("p");
      line.textContent = `${item.canonical_requirement} · ${item.evidence_status.replaceAll("_", " ")}`;
      elements.statusList.append(line);
    }
  }

  async function load(id) {
    applicationId = id;
    try { render(await getActiveInterview(id)); }
    catch (error) { onMessage(error.message || "Could not restore interview.", "error"); }
  }

  async function start() {
    if (busy || !applicationId) return;
    busy = true;
    try { render(await startInterview(applicationId)); }
    catch (error) { onMessage(error.message || "Could not start interview.", "error"); }
    finally { busy = false; }
  }

  async function action(kind, extra = {}) {
    if (busy || !view?.interview) return;
    busy = true;
    try {
      const key = mutationKey(kind, view.interview.version, extra);
      render(await mutateInterview(view.interview.interview_session_id,
        kind, view.interview.version, { ...extra, idempotency_key: key }));
      retry = null;
      elements.answer.value = "";
    } catch (error) {
      if (error.status === 409) {
        render(await getInterview(view.interview.interview_session_id));
        retry = null;
        onMessage("Interview changed elsewhere. Refresh it before submitting again.", "error");
      } else onMessage(error.message || "Interview action failed.", "error");
    } finally { busy = false; }
  }

  async function candidateAction(kind, editedClaim = null) {
    if (busy || !view?.candidate) return;
    busy = true;
    try {
      const extra = editedClaim === null ? {} : { edited_claim: editedClaim };
      const key = mutationKey(`candidate:${view.candidate.evidence_id}:${kind}`,
        view.interview.version, extra);
      render(await decideInterviewCandidate(view.interview.interview_session_id,
        view.candidate.evidence_id, kind, view.interview.version,
        { ...extra, idempotency_key: key }));
      retry = null;
    } catch (error) {
      if (error.status === 409) {
        render(await getInterview(view.interview.interview_session_id));
        retry = null;
      }
      onMessage(error.message || "Candidate action failed.", "error");
    } finally { busy = false; }
  }

  elements.start.addEventListener("click", start);
  elements.submit.addEventListener("click", () => {
    const answer = elements.answer.value.trim();
    if (answer) action("answers", { answer });
    else onMessage("Enter an answer first.", "error");
  });
  elements.noExperience.addEventListener("click", () => action("answers", {
    answer: "I do not have this experience.",
  }));
  elements.skip.addEventListener("click", () => action("skip"));
  elements.recover.addEventListener("click", () => action("resume"));
  elements.cancel.addEventListener("click", () => action("cancel"));
  elements.saveLater.addEventListener("click", () => onMessage(
    "Interview saved. Open this Application later to continue.", "success"));
  return { load, render };
}
