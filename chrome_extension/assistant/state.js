"use strict";

const DISPLAY_LABELS = Object.freeze({
  active: "Ready",
  running: "Working",
  awaiting_user: "Waiting for your answer",
  awaiting_input: "Waiting for your answer",
  awaiting_tool_approval: "Waiting for approval",
  awaiting_review: "Ready for your review",
  revising: "Improving draft",
  verified: "Verified",
  approved: "Approved",
  completed: "Complete",
  succeeded: "Complete",
  failed: "Couldn’t complete",
  blocked: "Waiting",
  cancelled: "Cancelled",
  timed_out: "Timed out",
  approval_required: "Approval required",
  needs_revision: "Needs correction",
  stale: "Needs refresh",
  draft: "Draft",
});

const ARTIFACT_LABELS = Object.freeze({
  tailored_resume: "Tailored resume",
  resume: "Tailored resume",
  cover_letter: "Cover letter",
  application_answer: "Application answer",
  interview_report: "Interview report",
});

export function createCopilotState() {
  return { application: null, session: null, activities: [], cursor: 0, busy: false };
}

export function displayLabel(value, fallback = "Update") {
  if (!value) return fallback;
  return DISPLAY_LABELS[value] || String(value).replaceAll("_", " ")
    .replace(/\b\w/g, character => character.toUpperCase());
}

export function activityLabel(type) {
  return displayLabel(type, "Update");
}

export function artifactLabel(type) {
  return ARTIFACT_LABELS[type] || displayLabel(type, "Document");
}

export function statusSentence(status, subject = "This step") {
  const label = displayLabel(status, "Updated");
  if (["Complete", "Verified", "Approved"].includes(label)) return `${subject} is ${label.toLowerCase()}.`;
  return `${subject}: ${label}.`;
}
