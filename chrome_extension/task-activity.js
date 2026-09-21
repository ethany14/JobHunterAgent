"use strict";

import { cancelAgentTask, listApplicationAgentTasks, retryAgentTask } from "./api-client.js";

export function renderTaskActivity(container, tasks, onCancel, onRetry) {
  container.replaceChildren();
  if (!Array.isArray(tasks) || tasks.length === 0) {
    const empty = document.createElement("p");
    empty.textContent = "No agent tasks for this Application.";
    container.append(empty);
    return;
  }
  for (const task of tasks) {
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = `${task.parent_task_id ? "Child" : "Root"} · ${task.agent_role || "Agent"} · ${task.status || "unknown"}`;
    details.append(summary);
    const meta = document.createElement("p");
    const elapsed = task.started_at
      ? Math.max(0, Math.round((new Date(task.completed_at || Date.now()) - new Date(task.started_at)) / 1000))
      : null;
    meta.textContent = `Attempts ${task.attempt_count ?? 0}/${task.max_attempts ?? 0}` +
      (elapsed === null ? "" : ` · ${elapsed}s`) +
      (task.error_code ? ` · ${task.error_code}` : "");
    details.append(meta);
    if (Array.isArray(task.dependencies) && task.dependencies.length) {
      const dependencies = document.createElement("p");
      dependencies.textContent = `Dependencies: ${task.dependencies.map((item) =>
        `${item.dependency_type} ${item.depends_on_task_id}`).join(", ")}`;
      details.append(dependencies);
    }
    if (task.result_summary?.summary) {
      const result = document.createElement("p");
      result.textContent = task.result_summary.summary;
      details.append(result);
    }
    if (Array.isArray(task.output_artifacts) && task.output_artifacts.length) {
      const outputs = document.createElement("p");
      outputs.textContent = `Output artifacts: ${task.output_artifacts.map((item) =>
        `${item.role} (${item.artifact_id})`).join(", ")}`;
      details.append(outputs);
    }
    if (!["succeeded", "failed", "cancelled", "timed_out"].includes(task.status)) {
      const cancel = document.createElement("button");
      cancel.type = "button"; cancel.className = "text-button danger-text";
      cancel.textContent = "Cancel task";
      cancel.addEventListener("click", () => onCancel(task));
      details.append(cancel);
    }
    if (task.status === "failed" && task.attempt_count < task.max_attempts) {
      const retry = document.createElement("button");
      retry.type = "button"; retry.className = "text-button";
      retry.textContent = "Retry task";
      retry.addEventListener("click", () => onRetry(task));
      details.append(retry);
    }
    container.append(details);
  }
}

export function createTaskActivity({ container, onError }) {
  let applicationId = null;
  async function load(nextApplicationId = applicationId) {
    if (!nextApplicationId) return;
    applicationId = nextApplicationId;
    try {
      const tasks = await listApplicationAgentTasks(applicationId);
      renderTaskActivity(container, tasks, cancel, retry);
    } catch (error) {
      onError(error.message || "Could not load task activity.");
    }
  }
  async function cancel(task) {
    try { await cancelAgentTask(task.task_id, task.version); await load(); }
    catch (error) { onError(error.message || "Could not cancel task."); await load(); }
  }
  async function retry(task) {
    try { await retryAgentTask(task.task_id, task.version); await load(); }
    catch (error) { onError(error.message || "Could not retry task."); await load(); }
  }
  return { load };
}
