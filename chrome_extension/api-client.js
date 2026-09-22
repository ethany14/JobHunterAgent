"use strict";

const API_BASE_URL = "http://localhost:8000";
const REQUEST_TIMEOUT_MS = 300_000;

export class ApiError extends Error {
  constructor(message, { status = 0 } = {}) { super(message); this.name = "ApiError"; this.status = status; }
}

function safeDetail(payload, status) {
  const detail = payload?.detail;
  if (typeof detail === "string") return detail;
  if (typeof detail?.message === "string") return detail.message;
  if (status === 409) return "Upload a default PDF resume in the full web workspace first.";
  if (status === 422) return "Check the job description and try again.";
  return "The backend could not complete the request.";
}

async function request(path, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), options.timeoutMs || REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(`${API_BASE_URL}${path}`, { ...options, headers: { Accept: "application/json", ...(options.body ? { "Content-Type": "application/json" } : {}), ...(options.headers || {}) }, signal: controller.signal });
    const payload = await response.json().catch(() => null);
    if (!response.ok) throw new ApiError(safeDetail(payload, response.status), { status: response.status });
    return payload;
  } catch (error) {
    if (error?.name === "AbortError") throw new ApiError("The analysis timed out. The run may still be processing; check the full workspace.");
    if (error instanceof ApiError) throw error;
    throw new ApiError("Cannot reach JobHunterAgent at http://localhost:8000.");
  } finally { clearTimeout(timeout); }
}

export const health = () => request("/health", { timeoutMs: 8_000 });
export const saveWorkspace = (workspace) => request("/api/workspaces", {
  method: "POST",
  body: JSON.stringify({ ...workspace, reopen_existing: true }),
});
export const analyzeApplication = (applicationId, expectedVersion) => request(
  `/api/applications/${encodeURIComponent(applicationId)}/analyze-with-resume`,
  { method: "POST", body: JSON.stringify({ expected_version: expectedVersion, resume_id: null }) },
);

// Compatibility exports for dormant, unmounted controllers. The focused Side
// Panel does not import these modules, but keeping their API adapters valid
// preserves isolated rendering tests and future Copilot-launched flows.
export const createFeedback = (sourceType, content, sourceActionId = crypto.randomUUID(), applicationId = null) =>
  request("/api/feedback", { method: "POST", body: JSON.stringify({ source_type: sourceType, content, source_action_id: sourceActionId, application_id: applicationId }) });
export const listLearningCandidates = (type = null) => request(`/api/learning-candidates${type ? `?candidate_type=${encodeURIComponent(type)}` : ""}`);
export const getLearningCandidateEvents = (id) => request(`/api/learning-candidates/${encodeURIComponent(id)}/events`);
export const getLearningCandidateConflicts = (id) => request(`/api/learning-candidates/${encodeURIComponent(id)}/conflicts`);
export const mutateLearningCandidate = (id, action, version, content = null) => request(`/api/learning-candidates/${encodeURIComponent(id)}/${action}`, { method: "POST", body: JSON.stringify({ expected_version: version, idempotency_key: crypto.randomUUID(), content }) });
export const materializeSkillCandidate = (id, version, name, semanticVersion = "1.0.0") => request(`/api/skill-candidates/${encodeURIComponent(id)}/materialize`, { method: "POST", body: JSON.stringify({ expected_version: version, idempotency_key: crypto.randomUUID(), skill_name: name, semantic_version: semanticVersion }) });
export const getStagedSkillCandidate = (id) => request(`/api/skill-candidates/${encodeURIComponent(id)}/staged`);
export const restageSkillCandidate = (id, version) => request(`/api/skill-candidates/${encodeURIComponent(id)}/restage`, { method: "POST", body: JSON.stringify({ expected_version: version, idempotency_key: crypto.randomUUID() }) });
export const evaluateSkillCandidate = (id, version, repetitions = 3) => request(`/api/skill-candidates/${encodeURIComponent(id)}/evaluations`, { method: "POST", timeoutMs: 600_000, body: JSON.stringify({ expected_version: version, idempotency_key: crypto.randomUUID(), repetitions }) });
export const getSkillEvaluation = (id, results = false) => request(`/api/skill-evaluations/${encodeURIComponent(id)}${results ? "/results" : ""}`);
export const publishSkillCandidate = (id, version, acknowledge = false) => request(`/api/skill-candidates/${encodeURIComponent(id)}/publish`, { method: "POST", body: JSON.stringify({ expected_version: version, idempotency_key: crypto.randomUUID(), acknowledge_soft_regressions: acknowledge }) });
export const rejectStagedSkillCandidate = (id, version) => request(`/api/skill-candidates/${encodeURIComponent(id)}/reject`, { method: "POST", body: JSON.stringify({ expected_version: version, idempotency_key: crypto.randomUUID() }) });
export const listGovernedSkills = () => request("/api/skills");
export const getGovernedSkillMetrics = (name) => request(`/api/skills/${encodeURIComponent(name)}/metrics`);
export const activateGovernedSkill = (name, versionId, version, mode) => request(`/api/skills/${encodeURIComponent(name)}/activate`, { method: "POST", body: JSON.stringify({ version_id: versionId, mode, expected_version: version, idempotency_key: crypto.randomUUID() }) });
export const rollbackGovernedSkill = (name, failedId, targetId, version, reason) => request(`/api/skills/${encodeURIComponent(name)}/rollback`, { method: "POST", body: JSON.stringify({ failed_version_id: failedId, target_version_id: targetId, expected_version: version, reason, idempotency_key: crypto.randomUUID() }) });
export const getActiveMockInterview = (applicationId) => request(`/api/applications/${encodeURIComponent(applicationId)}/mock-interviews/active`);
export const getMockInterview = (id) => request(`/api/mock-interviews/${encodeURIComponent(id)}`);
export const getMockInterviewReport = (id) => request(`/api/mock-interviews/${encodeURIComponent(id)}/report`);
export const getMockInterviewCandidates = (id) => request(`/api/mock-interviews/${encodeURIComponent(id)}/evidence-candidates`);
export const startMockInterview = (applicationId, options) => request(`/api/applications/${encodeURIComponent(applicationId)}/mock-interviews`, { method: "POST", body: JSON.stringify(options) });
export const mutateMockInterview = (id, action, version, key, extra = {}) => request(`/api/mock-interviews/${encodeURIComponent(id)}/${action}`, { method: "POST", body: JSON.stringify({ expected_version: version, idempotency_key: key, ...extra }) });
export const mutateCareerEvidence = (id, action, version, extra = {}) => request(`/api/evidence/${encodeURIComponent(id)}/${action}`, { method: "POST", body: JSON.stringify({ expected_version: version, ...extra }) });
export const submitMockInterviewFeedback = (id, helpful, feedback = null, editedStructure = null) => request(`/api/mock-interviews/${encodeURIComponent(id)}/feedback`, { method: "POST", body: JSON.stringify({ source_action_id: crypto.randomUUID(), helpful, feedback, edited_structure: editedStructure }) });
