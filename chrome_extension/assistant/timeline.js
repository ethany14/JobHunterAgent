"use strict";

import { clearNode, textElement } from "../shared/safe-dom.js";
import { displayLabel } from "./state.js";
import { renderActivityCard, renderConversationMessage } from "./cards/index.js";

function identity(activity) {
  return activity.activity_id || `${activity.reference_type}-${activity.reference_id}`;
}

const PUBLIC_ARTIFACT_TYPES = new Set([
  "tailored_resume", "cover_letter", "application_answer", "interview_report",
]);

const ACTION_LABELS = Object.freeze({
  analyze_current_job: "Job analysis",
  generate_application_pack: "Cover letter",
  save_current_job: "Saved job",
  start_mock_interview: "Practice interview",
  start_evidence_interview: "Evidence interview",
});

function visibleActivities(activities) {
  const latestArtifact = new Map();
  const latestRepeatedAction = new Map();
  const latestQuestion = new Map();
  for (const activity of activities) {
    if (activity.type === "artifact") {
      const type = activity.payload?.artifact_type;
      if (!PUBLIC_ARTIFACT_TYPES.has(type)) continue;
      const current = latestArtifact.get(type);
      if (!current || Number(activity.payload?.version || 0) >= Number(current.payload?.version || 0)) {
        latestArtifact.set(type, activity);
      }
    }
    if (activity.type === "workflow_progress" && activity.reference_type === "assistant_action") {
      const action = activity.payload?.action_type;
      if (action) latestRepeatedAction.set(action, activity);
    }
    if (activity.type === "question") {
      const interview = activity.payload?.interview_id || "unscoped";
      latestQuestion.set(`${activity.reference_type}:${interview}`, activity);
    }
  }
  return activities.filter(activity => {
    if (activity.type === "artifact") {
      return latestArtifact.get(activity.payload?.artifact_type) === activity;
    }
    if (activity.type === "workflow_progress" && activity.reference_type === "assistant_action") {
      const action = activity.payload?.action_type;
      return !action || latestRepeatedAction.get(action) === activity;
    }
    return true;
  }).map(activity => {
    if (activity.type !== "question") return activity;
    const interview = activity.payload?.interview_id || "unscoped";
    if (latestQuestion.get(`${activity.reference_type}:${interview}`) === activity) {
      return activity;
    }
    return { ...activity, type: "assistant_message", status: "completed" };
  });
}

function renderWorkflowDisclosure(group) {
  const activityIds = group.map(identity);
  const complete = group.every(item => ["completed", "succeeded", "approved"].includes(item.status));
  const failed = group.some(item => item.status === "failed");
  const details = document.createElement("details");
  details.id = `workflow-${activityIds.join("-")}`;
  details.className = "activity-disclosure workflow-disclosure";
  details.append(textElement("summary", "activity-line",
    failed ? "Application preparation couldn’t complete"
      : complete ? "Application analysis complete" : "Preparing your application"));
  const list = document.createElement("ul");
  for (const item of group) {
    const action = item.payload?.action_type;
    const label = item.payload?.label || item.payload?.task_type
      || ACTION_LABELS[action] || "Application preparation";
    list.append(textElement("li", "", `${displayLabel(label)}: ${displayLabel(item.status)}`));
  }
  details.append(list);

  const developer = document.createElement("details");
  developer.className = "developer-detail legacy-feature";
  developer.append(textElement("summary", "", "Technical details"));
  for (const item of group) {
    developer.append(textElement("p", "muted",
      `${item.payload?.task_type || "task"} • attempt ${item.payload?.attempt ?? 0}`));
  }
  details.append(developer);
  return details;
}

function renderToolDisclosure(group) {
  const details = document.createElement("details");
  details.id = `tools-${group.map(identity).join("-")}`;
  details.className = "activity-disclosure tool-disclosure";
  details.append(textElement("summary", "activity-line",
    `Used ${group.length} ${group.length === 1 ? "tool" : "tools"}`));
  const list = document.createElement("ul");
  for (const item of group) {
    list.append(textElement("li", "", displayLabel(item.payload?.tool_name, "Tool")));
  }
  details.append(list);
  const developer = document.createElement("div");
  developer.className = "developer-detail legacy-feature";
  for (const item of group) {
    const provider = item.payload?.provider === "MCP" ? "MCP" : "Built-in";
    developer.append(textElement("p", "muted",
      `${provider} • ${displayLabel(item.status)}${item.payload?.duration_ms != null ? ` • ${item.payload.duration_ms} ms` : ""}`));
  }
  details.append(developer);
  return details;
}

function renderVerification(activity) {
  const passed = activity.payload?.passed === true
    || ["passed", "verified", "approved"].includes(activity.payload?.verification_status)
    || ["verified", "approved"].includes(activity.status);
  const count = Number(activity.payload?.issue_count || 0);
  const text = passed ? "Factual verification passed."
    : count ? `I found ${count} ${count === 1 ? "claim" : "claims"} that need correction.`
      : "Some claims need correction before this draft is ready.";
  return renderConversationMessage({ ...activity, payload: { content: text } }, "assistant");
}

function groupedEntries(activities) {
  const entries = [];
  let index = 0;
  while (index < activities.length) {
    const activity = activities[index];
    if (["workflow_progress", "tool_call"].includes(activity.type)) {
      const type = activity.type;
      const group = [];
      while (index < activities.length && activities[index].type === type) {
        group.push(activities[index]);
        index += 1;
      }
      entries.push({ key: `${type}:${group.map(identity).join(":")}`, group, type });
      continue;
    }
    entries.push({ key: identity(activity), activity, type: activity.type });
    index += 1;
  }
  return entries;
}

export function renderTimeline(container, activities, options = {}) {
  const visible = visibleActivities(activities);
  if (!visible.length) {
    clearNode(container);
    container.__copilotNodes = new Map();
    container.append(textElement("p", "conversation-empty",
      "Ask Career Copilot to help with this job."));
    return;
  }

  const previous = container.__copilotNodes || new Map();
  const next = new Map();
  const ordered = [];
  for (const entry of groupedEntries(visible)) {
    const signature = JSON.stringify(entry);
    let cached = previous.get(entry.key);
    if (!cached || cached.signature !== signature) {
      let node;
      if (entry.type === "workflow_progress") node = renderWorkflowDisclosure(entry.group);
      else if (entry.type === "tool_call") node = renderToolDisclosure(entry.group);
      else if (entry.type === "verification") node = renderVerification(entry.activity);
      else node = renderActivityCard(entry.activity, options);
      cached = { node, signature };
    }
    next.set(entry.key, cached);
    ordered.push(cached.node);
  }
  clearNode(container);
  container.append(...ordered);
  container.__copilotNodes = next;
}
