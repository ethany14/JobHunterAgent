"use strict";

const API_BASE_URL = "http://localhost:8000";
const REQUEST_TIMEOUT_MS = 60_000;

export class ApiError extends Error {
  constructor(message, { status = 0, detail = null } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

function validationMessage(detail) {
  if (detail && typeof detail === "object" && typeof detail.message === "string") {
    return detail.message;
  }
  if (!Array.isArray(detail)) {
    return typeof detail === "string" ? detail : null;
  }
  return detail
    .map((item) => {
      const location = Array.isArray(item?.loc) ? item.loc.slice(1).join(".") : "input";
      const message = typeof item?.msg === "string" ? item.msg : "is invalid";
      return `${location || "input"}: ${message}`;
    })
    .join("; ");
}

function safeErrorMessage(status, detail) {
  const validation = validationMessage(detail);
  if (status === 422) {
    return validation ? `Please check the submitted text: ${validation}` : "Please check the submitted text.";
  }
  if (status === 404) {
    return validation || "The requested resource no longer exists.";
  }
  if (status === 409) {
    return validation || "This run cannot be reviewed in its current state.";
  }
  return "The backend could not complete the request.";
}

async function request(path, options = {}) {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(`${API_BASE_URL}${path}`, {
      ...options,
      headers: {
        Accept: "application/json",
        ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...options.headers,
      },
      signal: controller.signal,
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok) {
      const detail = payload?.detail ?? null;
      throw new ApiError(safeErrorMessage(response.status, detail), {
        status: response.status,
        detail,
      });
    }
    return payload;
  } catch (error) {
    if (error?.name === "AbortError") {
      throw new ApiError("The backend request timed out. Please try again.");
    }
    if (error instanceof ApiError) {
      throw error;
    }
    throw new ApiError("Cannot reach the Job Agent backend at http://localhost:8000.");
  } finally {
    clearTimeout(timeoutId);
  }
}

export function health() {
  return request("/health");
}

export function createRun(resumeText, jobDescription) {
  return request("/runs", {
    method: "POST",
    body: JSON.stringify({
      resume_text: resumeText,
      job_description: jobDescription,
    }),
  });
}

export function getRun(runId) {
  return request(`/runs/${encodeURIComponent(runId)}`);
}

export function reviewRun(runId, approved, feedback = null) {
  return request(`/runs/${encodeURIComponent(runId)}/review`, {
    method: "POST",
    body: JSON.stringify({ approved, feedback }),
  });
}

export function createSession(activeRunId = null, applicationId = null) {
  return request("/sessions", {
    method: "POST",
    body: JSON.stringify({
      capability_profile: "job_assistant_readonly",
      active_run_id: activeRunId,
      application_id: applicationId,
    }),
  });
}

export function saveWorkspace(workspace) {
  return request("/api/workspaces", { method: "POST", body: JSON.stringify(workspace) });
}

export function listApplications({ status = "", search = "", limit = 25, cursor = "" } = {}) {
  const params = new URLSearchParams({ limit: String(limit) });
  if (status) params.set("status", status);
  if (search) params.set("search", search);
  if (cursor) params.set("cursor", cursor);
  return request(`/api/applications?${params.toString()}`);
}

export function getApplication(applicationId) {
  return request(`/api/applications/${encodeURIComponent(applicationId)}`);
}

export function updateApplication(applicationId, values) {
  return request(`/api/applications/${encodeURIComponent(applicationId)}`, {
    method: "PATCH", body: JSON.stringify(values),
  });
}

export function transitionApplication(applicationId, targetStatus, expectedVersion, appliedAt = null) {
  return request(`/api/applications/${encodeURIComponent(applicationId)}/status`, {
    method: "PATCH",
    body: JSON.stringify({ target_status: targetStatus, expected_version: expectedVersion, applied_at: appliedAt }),
  });
}

export function analyzeApplication(applicationId, snapshotId, resumeText, expectedVersion) {
  return request(`/api/applications/${encodeURIComponent(applicationId)}/analyze`, {
    method: "POST",
    body: JSON.stringify({ snapshot_id: snapshotId, resume_text: resumeText, expected_version: expectedVersion }),
  });
}

export function listApplicationArtifacts(applicationId) {
  return request(`/api/applications/${encodeURIComponent(applicationId)}/artifacts`);
}

export function listApplicationEvents(applicationId) {
  return request(`/api/applications/${encodeURIComponent(applicationId)}/events`);
}

export function listSessions(limit = 25) {
  return request(`/sessions?limit=${encodeURIComponent(limit)}`);
}

export function getSession(sessionId) {
  return request(`/sessions/${encodeURIComponent(sessionId)}`);
}

export function getSessionMessages(sessionId) {
  return request(`/sessions/${encodeURIComponent(sessionId)}/messages`);
}

export function sendSessionMessage(sessionId, messageId, content, expectedVersion) {
  return request(`/sessions/${encodeURIComponent(sessionId)}/messages`, {
    method: "POST",
    body: JSON.stringify({
      message_id: messageId,
      content,
      expected_version: expectedVersion,
    }),
  });
}

export function approveToolCall(sessionId, toolCallId, expectedVersion) {
  return request(
    `/sessions/${encodeURIComponent(sessionId)}/tool-calls/${encodeURIComponent(toolCallId)}/approve`,
    {
      method: "POST",
      body: JSON.stringify({ expected_version: expectedVersion }),
    },
  );
}

export function rejectToolCall(sessionId, toolCallId, expectedVersion) {
  return request(
    `/sessions/${encodeURIComponent(sessionId)}/tool-calls/${encodeURIComponent(toolCallId)}/reject`,
    {
      method: "POST",
      body: JSON.stringify({ expected_version: expectedVersion }),
    },
  );
}

export function cancelSession(sessionId, expectedVersion, reason) {
  return request(`/sessions/${encodeURIComponent(sessionId)}/cancel`, {
    method: "POST",
    body: JSON.stringify({ expected_version: expectedVersion, reason }),
  });
}

export function recoverSession(sessionId, expectedVersion) {
  return request(`/sessions/${encodeURIComponent(sessionId)}/recover`, {
    method: "POST",
    body: JSON.stringify({ expected_version: expectedVersion }),
  });
}

export function listMemories() {
  return request("/memories");
}

export function createMemory(memory) {
  return request("/memories", {
    method: "POST",
    body: JSON.stringify(memory),
  });
}

export function confirmMemory(memoryId, expectedVersion) {
  return request(`/memories/${encodeURIComponent(memoryId)}/confirm`, {
    method: "POST",
    body: JSON.stringify({ expected_version: expectedVersion }),
  });
}

export function rejectMemory(memoryId, expectedVersion) {
  return request(`/memories/${encodeURIComponent(memoryId)}/reject`, {
    method: "POST",
    body: JSON.stringify({ expected_version: expectedVersion }),
  });
}

export function supersedeMemory(memoryId, expectedVersion, replacementMemoryId, replacementVersion) {
  return request(`/memories/${encodeURIComponent(memoryId)}/supersede`, {
    method: "POST",
    body: JSON.stringify({
      expected_version: expectedVersion,
      replacement_memory_id: replacementMemoryId,
      replacement_expected_version: replacementVersion,
    }),
  });
}

export function deleteMemory(memoryId, expectedVersion) {
  return request(
    `/memories/${encodeURIComponent(memoryId)}?expected_version=${encodeURIComponent(expectedVersion)}`,
    { method: "DELETE" },
  );
}

export function listSkills() {
  return request("/skills");
}

export function listSkillVersions(skillName) {
  return request(`/skills/${encodeURIComponent(skillName)}/versions`);
}

export function getSkillVersion(versionId) {
  return request(`/skill-versions/${encodeURIComponent(versionId)}`);
}

function mutateSkill(versionId, action, expectedVersion) {
  return request(`/skill-versions/${encodeURIComponent(versionId)}/${action}`, {
    method: "POST",
    body: JSON.stringify({ expected_version: expectedVersion }),
  });
}

export function approveSkill(versionId, expectedVersion) {
  return mutateSkill(versionId, "approve", expectedVersion);
}

export function activateSkill(versionId, expectedVersion) {
  return mutateSkill(versionId, "activate", expectedVersion);
}

export function rejectSkill(versionId, expectedVersion) {
  return mutateSkill(versionId, "reject", expectedVersion);
}

export function retireSkill(versionId, expectedVersion) {
  return mutateSkill(versionId, "retire", expectedVersion);
}

export function getSessionContext(sessionId) {
  return request(`/sessions/${encodeURIComponent(sessionId)}/context`);
}
