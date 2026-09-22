"use strict";

import { button, textElement } from "../../shared/safe-dom.js";
import { activityLabel, artifactLabel, displayLabel } from "../state.js";

function identify(node, activity, prefix = "activity") {
  node.id = `${prefix}-${activity.activity_id || activity.reference_id}`;
  node.dataset.activityId = activity.activity_id || "";
  node.dataset.referenceId = activity.reference_id || "";
  return node;
}

function safeSummary(activity) {
  const payload = activity.payload || {};
  return payload.content || payload.preview || payload.label || payload.canonical_key
    || payload.artifact_type || payload.tool_name
    || (payload.action_type ? activityLabel(payload.action_type) : "Update");
}

export function renderConversationMessage(activity, role = null) {
  const effectiveRole = role || (activity.type === "user_message" ? "user" : "assistant");
  const message = identify(document.createElement("article"), activity, "message");
  message.className = `conversation-message ${effectiveRole}`;
  message.append(textElement("p", "sr-only", effectiveRole === "user" ? "You" : "Career Copilot"));
  message.append(textElement("p", "message-content", safeSummary(activity)));
  return message;
}

export function renderQuestion(activity, { onAction } = {}) {
  const message = renderConversationMessage(activity, "assistant");
  message.id = `question-${activity.reference_id}`;
  if (!onAction) return message;

  const response = document.createElement("div");
  response.className = "inline-response";
  const answer = document.createElement("textarea");
  answer.rows = 3;
  answer.maxLength = 20_000;
  answer.placeholder = "Type your answer";
  answer.setAttribute?.("aria-label", "Interview answer");
  const actions = document.createElement("div");
  actions.className = "inline-actions";
  actions.append(button("Send", () => onAction(activity, "answer", answer.value), "primary-button"));
  actions.append(button("Skip", () => onAction(activity, "skip"), "text-button"));
  if (activity.reference_type === "interview_turn") {
    actions.append(button("I don’t have this experience",
      () => onAction(activity, "no_experience"), "text-button"));
  }
  response.append(answer, actions);
  message.append(response);
  return message;
}

export function renderArtifactCard(activity, { onAction, onExpand } = {}) {
  const card = identify(document.createElement("article"), activity, "artifact");
  card.className = "artifact-card";
  card.dataset.artifactId = activity.reference_id;
  card.dataset.version = String(activity.payload?.version || "");
  card.append(textElement("h3", "artifact-name", artifactLabel(activity.payload?.artifact_type)));
  const status = activity.payload?.verification_status || activity.status;
  card.append(textElement("p", "artifact-meta",
    [`Version ${activity.payload?.version || 1}`, displayLabel(status)].join(" • ")));
  card.append(textElement("p", "artifact-preview",
    activity.payload?.preview || "Open the document to review its full content."));

  const details = document.createElement("details");
  details.className = "artifact-detail";
  details.append(textElement("summary", "sr-only", "Document detail"));
  if (onExpand) details.addEventListener("toggle", () => {
    if (details.open && !details.dataset.loaded) {
      details.dataset.loaded = "true";
      onExpand(activity, details);
    }
  });
  card.append(details);

  if (onAction && activity.reference_type === "application_artifact") {
    const actions = document.createElement("div");
    actions.className = "card-actions";
    actions.append(button("Review", () => {
      details.open = true;
      if (onExpand && !details.dataset.loaded) {
        details.dataset.loaded = "true";
        onExpand(activity, details);
      }
    }, "primary-button"));
    actions.append(button("Export", () => onAction(activity, "export"), "secondary-button"));
    card.append(actions);
  }
  return card;
}

export function renderApprovalCard(activity, { onAction } = {}) {
  const card = identify(document.createElement("article"), activity, "approval");
  card.className = "approval-card";
  card.dataset.approvalId = activity.reference_id;
  card.append(textElement("h3", "approval-title", "Approval required"));
  card.append(textElement("p", "approval-summary",
    activity.payload?.label || `Allow ${activity.payload?.tool_name || "this action"}?`));
  const metadata = [activity.payload?.destination, activity.payload?.file_name]
    .filter(Boolean).join(" • ");
  if (metadata) card.append(textElement("p", "artifact-meta", metadata));
  if (onAction) {
    const actions = document.createElement("div");
    actions.className = "card-actions";
    actions.append(button("Cancel", () => onAction(activity, "reject"), "secondary-button"));
    actions.append(button("Approve", () => onAction(activity, "approve"), "primary-button"));
    card.append(actions);
  }
  return card;
}

export function renderConfirmationCard(activity, { onAction } = {}) {
  const card = identify(document.createElement("article"), activity, "confirmation");
  card.className = "confirmation-card";
  card.append(textElement("h3", "confirmation-title",
    activity.type === "evidence_candidate" ? "Save as reusable career evidence?"
      : "Save this for future conversations?"));
  card.append(textElement("p", "confirmation-content", safeSummary(activity)));
  if (onAction && activity.reference_type === "career_evidence") {
    const edit = document.createElement("textarea");
    edit.rows = 3;
    edit.value = activity.payload?.content || "";
    edit.hidden = true;
    const actions = document.createElement("div");
    actions.className = "card-actions";
    actions.append(button("Edit", () => { edit.hidden = !edit.hidden; }, "secondary-button"));
    actions.append(button("Reject", () => onAction(activity, "reject"), "text-button"));
    actions.append(button("Confirm", () => onAction(activity, "confirm", edit.hidden ? "" : edit.value), "primary-button"));
    card.append(edit, actions);
  }
  return card;
}

export function renderActivityCard(activity, options = {}) {
  if (["user_message", "assistant_message", "interview_feedback", "action_proposal",
       "recovery_notice", "error"].includes(activity.type)) {
    return renderConversationMessage(activity);
  }
  if (activity.type === "question") return renderQuestion(activity, options);
  if (activity.type === "artifact") return renderArtifactCard(activity, options);
  if (activity.type === "approval") return renderApprovalCard(activity, options);
  if (["evidence_candidate", "learning_candidate"].includes(activity.type)) {
    return renderConfirmationCard(activity, options);
  }
  return renderConversationMessage(activity, "assistant");
}

export const ArtifactCard = renderArtifactCard;
export const ApprovalCard = renderApprovalCard;
export const ConfirmationCard = renderConfirmationCard;
