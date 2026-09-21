"use strict";

import {
  cancelMultiAgentRun, createMultiAgentRun, getMultiAgentRun,
  getMultiAgentTasks, listApplicationAgentTasks, resumeMultiAgentRun,
} from "./api-client.js";

const TERMINAL = new Set(["awaiting_review", "failed", "cancelled", "timed_out"]);

export function renderMultiAgentProgress(container, tasks) {
  container.replaceChildren();
  for (const task of tasks) {
    const line = document.createElement("p");
    const elapsed = task.started_at
      ? Math.max(0, Math.round((new Date(task.completed_at || Date.now()) - new Date(task.started_at)) / 1000))
      : null;
    line.textContent = `${task.agent_role || task.task_type}: ${task.status}` +
      ` · attempts ${task.attempt_count ?? 0}` +
      (elapsed === null ? "" : ` · ${elapsed}s`) +
      (task.error_code ? ` · ${task.error_code}` : "") +
      (Array.isArray(task.output_artifacts) && task.output_artifacts.length
        ? ` · artifacts ${task.output_artifacts.map((item) => item.artifact_id).join(", ")}` : "");
    container.append(line);
  }
}

export function createMultiAgentController({ elements, getApplication, onMessage,
  onTasksChanged = () => {}, onInterviewNeeded = () => {} }) {
  let applicationId = null;
  let rootId = null;
  let tasks = [];
  let busy = false;
  let pendingKey = null;
  let polling = null;
  let pollsRemaining = 0;

  function stopPolling() {
    if (polling !== null) clearTimeout(polling);
    polling = null;
  }

  function schedulePoll(status) {
    stopPolling();
    if (TERMINAL.has(status) || pollsRemaining <= 0) return;
    pollsRemaining -= 1;
    polling = setTimeout(() => refresh(), 3000);
  }

  function modeChanged() {
    const selected = elements.mode.value === "multi_agent_v1";
    elements.options.hidden = !selected;
    if (!selected) elements.status.textContent = "Standard workflow selected.";
    else if (!rootId) elements.status.textContent = "No Multi-Agent execution for this Application.";
  }

  async function refresh() {
    if (!rootId || busy) return;
    try {
      const [summary, nextTasks] = await Promise.all([
        getMultiAgentRun(rootId), getMultiAgentTasks(rootId),
      ]);
      tasks = nextTasks;
      elements.status.textContent = `${summary.status.replaceAll("_", " ")}` +
        (summary.pack_id ? ` · Pack ${summary.pack_id}` : "") +
        (summary.pack_status ? ` · ${summary.pack_status.replaceAll("_", " ")}` : "") +
        (summary.usage?.estimated_cost ? ` · estimated $${summary.usage.estimated_cost}` : "");
      renderMultiAgentProgress(elements.progress, tasks);
      const root = tasks.find((item) => item.task_id === rootId);
      elements.cancel.hidden = !root || TERMINAL.has(summary.status);
      elements.skipInterview.hidden = !tasks.some((item) =>
        item.task_type === "evidence_interview" && item.status === "awaiting_input");
      if (summary.status === "awaiting_input") onInterviewNeeded(applicationId);
      onTasksChanged(applicationId);
      schedulePoll(summary.status);
    } catch (error) {
      stopPolling();
      onMessage(error.message || "Could not refresh Multi-Agent progress.", "error");
    }
  }

  async function load(nextApplicationId) {
    stopPolling();
    applicationId = nextApplicationId;
    rootId = null;
    tasks = [];
    elements.progress.replaceChildren();
    modeChanged();
    if (!applicationId) return;
    try {
      const recent = await listApplicationAgentTasks(applicationId);
      const root = recent.find((item) => !item.parent_task_id &&
        item.workflow_mode === "multi_agent_v1");
      if (!root) return;
      rootId = root.task_id;
      pollsRemaining = 40;
      await refresh();
    } catch (error) {
      onMessage(error.message || "Could not restore Multi-Agent progress.", "error");
    }
  }

  async function start() {
    if (busy || !applicationId || elements.mode.value !== "multi_agent_v1") return;
    const app = getApplication();
    if (!app || app.application_id !== applicationId) return;
    const requested = [];
    if (elements.resume.checked) requested.push("tailored_resume");
    if (elements.cover.checked) requested.push("cover_letter");
    const question = elements.question.value.trim();
    if (question) requested.push("application_answer");
    if (!requested.length) {
      onMessage("Select at least one artifact or enter an application question.", "error");
      return;
    }
    busy = true;
    elements.start.disabled = true;
    pendingKey ||= crypto.randomUUID();
    try {
      const result = await createMultiAgentRun(applicationId, {
        expected_application_version: app.version,
        include_interview: elements.interview.checked,
        requested_artifacts: requested,
        application_questions: question ? [question] : [],
        budget_profile: "standard",
        idempotency_key: pendingKey,
      });
      rootId = result.root_task_id;
      pendingKey = null;
      pollsRemaining = 40;
      onMessage("Multi-Agent execution started.", "success");
    } catch (error) {
      if ([409, 422].includes(error.status)) pendingKey = null;
      onMessage(error.message || "Could not start workflow.", "error");
    } finally {
      busy = false;
      elements.start.disabled = false;
      if (rootId) await refresh();
    }
  }

  async function cancel() {
    const root = tasks.find((item) => item.task_id === rootId);
    if (!root || busy) return;
    busy = true;
    try { await cancelMultiAgentRun(rootId, root.version); }
    catch (error) { onMessage(error.message || "Could not cancel execution.", "error"); }
    finally { busy = false; await refresh(); }
  }

  async function resumeInterview() {
    const paused = tasks.find((item) => item.task_type === "evidence_interview" &&
      item.status === "awaiting_input");
    if (!paused || busy) return;
    busy = true;
    try { await resumeMultiAgentRun(rootId, paused.task_id, paused.version); }
    catch (error) { onMessage(error.message || "Could not resume interview task.", "error"); }
    finally { busy = false; pollsRemaining = 40; await refresh(); }
  }

  async function skipInterview() {
    const paused = tasks.find((item) => item.task_type === "evidence_interview" &&
      item.status === "awaiting_input");
    if (!paused || busy) return;
    busy = true;
    try { await resumeMultiAgentRun(rootId, paused.task_id, paused.version, true); }
    catch (error) { onMessage(error.message || "Cancel the interview first.", "error"); }
    finally { busy = false; pollsRemaining = 40; await refresh(); }
  }

  elements.mode.addEventListener("change", modeChanged);
  elements.start.addEventListener("click", start);
  elements.cancel.addEventListener("click", cancel);
  elements.skipInterview.addEventListener("click", skipInterview);
  modeChanged();
  return { load, refresh, resumeInterview };
}
