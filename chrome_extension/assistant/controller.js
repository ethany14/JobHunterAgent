"use strict";

import {
  approveToolCall, getApplication, getAssistantTimeline, listApplications,
  rejectToolCall, submitAssistantAction,
} from "../api-client.js";
import { createCopilotState, displayLabel } from "./state.js";
import { renderTimeline } from "./timeline.js";
import { clearNode, textElement } from "../shared/safe-dom.js";

export function createCareerCopilotController({ elements, getSession, onOpenWorkspace,
  onUseCurrentPage, onSaveCurrentPage, onOpenJobs, onOpenArtifact,
  onActionResult, onMessage }) {
  const state = createCopilotState();

  function normalizeActivity(activity) {
    if (activity?.activity_id) return activity;
    const type = activity?.type === "user" ? "user_message"
      : activity?.type === "assistant" ? "assistant_message"
      : activity?.type === "progress" ? "workflow_progress"
      : activity?.type;
    const preview = Array.isArray(activity?.preview)
      ? activity.preview.map(item => [item?.heading, item?.text].filter(Boolean).join(": ")).join("\n")
      : activity?.summary;
    return {
      ...activity,
      activity_id: activity?.id || crypto.randomUUID(),
      type,
      payload: activity?.payload || {
        content: activity?.text || "",
        label: activity?.label,
        preview,
        artifact_type: activity?.kind,
        version: activity?.version,
      },
      reference_id: activity?.reference_id || activity?.artifactId || activity?.id || crypto.randomUUID(),
    };
  }

  function setActivities(activities) {
    state.activities = Array.isArray(activities) ? activities.map(normalizeActivity) : [];
    renderTimeline(elements.timeline, state.activities, {
      onAction: cardAction,
      onExpand: (activity, detail) => onOpenArtifact?.(activity, detail),
    });
  }

  function appendActivity(activity) {
    const normalized = normalizeActivity(activity);
    setActivities([...state.activities, normalized]);
    return normalized.activity_id;
  }

  function upsertActivity(activity) {
    const normalized = normalizeActivity(activity);
    const index = state.activities.findIndex(
      item => item.activity_id === normalized.activity_id,
    );
    if (index < 0) return appendActivity(normalized);
    const updated = [...state.activities];
    updated[index] = { ...updated[index], ...normalized };
    setActivities(updated);
    return normalized.activity_id;
  }

  function dispatchAction(item) {
    elements.root.dispatchEvent(new CustomEvent("career-copilot:action", {
      bubbles: true,
      detail: {
        action: item.action,
        payload: item.payload || null,
        prompt: item.prompt || null,
        jobId: state.application?.application_id || null,
      },
    }));
  }

  function setQuickActions(actions) {
    clearNode(elements.quickActions);
    for (const item of (Array.isArray(actions) ? actions : []).slice(0, 3)) {
      const actionButton = document.createElement("button");
      actionButton.type = "button";
      actionButton.textContent = item.label;
      actionButton.addEventListener("click", () => dispatchAction(item));
      elements.quickActions.append(actionButton);
    }
  }

  function setBusy(value, label = "") {
    state.busy = Boolean(value);
    elements.root.toggleAttribute("aria-busy", state.busy);
    elements.root.dataset.busyLabel = state.busy ? label : "";
    for (const item of elements.quickActions.querySelectorAll("button")) {
      item.disabled = state.busy;
    }
  }

  async function refreshWorkspaces() {
    const response = await listApplications({ limit: 100 });
    clearNode(elements.workspaceSelect);
    const empty = document.createElement("option"); empty.value = ""; empty.textContent = "Select a saved job";
    elements.workspaceSelect.append(empty);
    for (const item of response.applications) {
      const option = document.createElement("option"); option.value = item.application_id;
      option.textContent = `${item.company || "Unknown company"} · ${item.title || "Untitled role"}`;
      elements.workspaceSelect.append(option);
    }
    if (state.application) elements.workspaceSelect.value = state.application.application_id;
  }

  async function selectWorkspace(applicationId) {
    if (!applicationId) { setWorkspace(null); return; }
    const detail = await getApplication(applicationId);
    setWorkspace(detail);
    await onOpenWorkspace(applicationId);
  }

  function setWorkspace(application) {
    state.application = application;
    elements.role.textContent = application?.job?.title || "No job selected";
    elements.company.textContent = application?.job?.company || "Choose a saved job or use the current page.";
    elements.status.textContent = application ? displayLabel(application.status) : "No workspace";
    elements.source.textContent = application?.job?.source_site ? `Source: ${application.job.source_site}` : "";
    elements.workspaceSelect.value = application?.application_id || "";
  }

  async function refreshTimeline() {
    const session = getSession();
    if (!session) { renderTimeline(elements.timeline, []); return; }
    state.session = session;
    const response = await getAssistantTimeline(session.session_id);
    state.activities = response.activities;
    state.cursor = response.next_sequence;
    setActivities(state.activities);
  }

  async function registeredAction(actionType, payload) {
    const session = getSession();
    if (!session) throw new Error("Start a Session before requesting an action.");
    return submitAssistantAction(session.session_id, actionType, session.version, payload);
  }

  async function cardAction(activity, verb, value = "") {
    const session = getSession();
    if (!session) { onMessage("Start a Session before requesting an action.", "error"); return; }
    state.busy = true;
    try {
      let result;
      if (activity.type === "approval") {
        result = verb === "approve"
          ? await approveToolCall(session.session_id, activity.reference_id, session.version)
          : await rejectToolCall(session.session_id, activity.reference_id, session.version);
      } else if (activity.type === "question") {
        if (verb === "skip" || verb === "no_experience") {
          result = await registeredAction(
            activity.reference_type === "mock_question"
              ? "submit_mock_interview_answer" : "submit_interview_answer",
            {
              ...(activity.reference_type === "mock_question"
                ? { mock_interview_id: activity.payload?.interview_id }
                : { interview_id: activity.payload?.interview_id }),
              interview_version: activity.payload?.interview_version,
              response_kind: verb,
            },
          );
        } else {
          const answer = String(value || "").trim();
          if (!answer) throw new Error("Enter an answer before submitting.");
          result = activity.reference_type === "mock_question"
          ? await registeredAction("submit_mock_interview_answer", {
              mock_interview_id: activity.payload?.interview_id,
              interview_version: activity.payload?.interview_version,
              answer,
            })
          : await registeredAction("submit_interview_answer", {
              interview_id: activity.payload?.interview_id,
              interview_version: activity.payload?.interview_version,
              answer,
            });
        }
      } else if (activity.type === "evidence_candidate") {
        result = await registeredAction("review_evidence_candidate", {
          interview_id: activity.payload?.interview_id,
          interview_version: activity.payload?.interview_version,
          evidence_id: activity.reference_id,
          decision: verb,
          edited_claim: String(value || "").trim() || null,
        });
      } else if (activity.type === "artifact") {
        if (verb === "export") {
          await onOpenArtifact?.(activity);
          onMessage("The document is ready to copy or download.", "success");
          return;
        }
        const actionType = verb === "export" ? "export_artifact" : "review_artifact";
        result = await registeredAction(actionType, {
          artifact_id: activity.reference_id,
          application_version: state.application?.version,
          decision: verb === "approve" ? "approve" : "regenerate",
        });
      } else if (["error", "recovery_notice"].includes(activity.type)) {
        result = await registeredAction(verb === "retry" ? "retry_workflow" : "cancel_workflow", {
          task_id: activity.reference_id,
        });
      }
      if (onActionResult) await onActionResult(result);
      await refreshTimeline();
      onMessage("The latest workflow state was loaded.", "success");
    } catch (error) {
      if (error.status === 409) {
        await refreshTimeline();
        onMessage("The workflow changed. The latest state was loaded; your action was not resent.", "error");
      } else {
        onMessage(error.message || "The action could not be completed.", "error");
      }
    } finally {
      state.busy = false;
    }
  }

  async function action(actionType, payload = {}) {
    const session = getSession();
    if (!session) { onMessage("Start a Session before requesting an action.", "error"); return; }
    state.busy = true;
    try {
      const result = await submitAssistantAction(
        session.session_id, actionType, session.version, payload,
      );
      if (onActionResult) await onActionResult(result);
      onMessage(
        result.status === "needs_clarification"
          ? result.clarification
          : displayLabel(result.status, "Request accepted"),
        "success",
      );
      await refreshTimeline();
    } catch (error) {
      if (error.status === 409) onMessage("The Session changed. Refresh it before retrying; the action was not resent.", "error");
      else onMessage(error.message || "The action could not be completed.", "error");
    } finally { state.busy = false; }
  }

  elements.workspaceSelect.addEventListener("change", () => selectWorkspace(elements.workspaceSelect.value));
  elements.currentPage.addEventListener("click", onUseCurrentPage);
  elements.saveJob.addEventListener("click", onSaveCurrentPage);
  elements.openJobs.addEventListener("click", onOpenJobs);
  for (const item of elements.quickActions.querySelectorAll("[data-action]")) {
    item.addEventListener("click", () => dispatchAction({ action: item.dataset.action }));
  }
  return {
    state,
    refreshWorkspaces,
    selectWorkspace,
    setWorkspace,
    refreshTimeline,
    action,
    setActivities,
    appendActivity,
    upsertActivity,
    setQuickActions,
    setBusy,
  };
}
