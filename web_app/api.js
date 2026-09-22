"use strict";

const BASE = "";
const TIMEOUT = 300_000;

export class ApiError extends Error {
  constructor(message, status = 0) { super(message); this.status = status; }
}

async function request(path, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), options.timeout || TIMEOUT);
  try {
    const response = await fetch(`${BASE}${path}`, { ...options, signal: controller.signal,
      headers: { Accept: "application/json", ...(options.body && !(options.body instanceof ArrayBuffer) ? { "Content-Type": "application/json" } : {}), ...(options.headers || {}) } });
    const payload = response.status === 204 ? null : await response.json().catch(() => null);
    if (!response.ok) {
      const detail = payload?.detail;
      const message = typeof detail === "string" ? detail : detail?.message || "The request could not be completed.";
      throw new ApiError(message, response.status);
    }
    return payload;
  } catch (error) {
    if (error?.name === "AbortError") throw new ApiError("The request timed out.");
    if (error instanceof ApiError) throw error;
    throw new ApiError("Cannot reach the local JobHunterAgent backend.");
  } finally { clearTimeout(timer); }
}

const json = (method, body) => ({ method, body: JSON.stringify(body) });
export const api = {
  health: () => request("/health", { timeout: 8_000 }),
  listApplications: (search = "") => request(`/api/applications?limit=100${search ? `&search=${encodeURIComponent(search)}` : ""}`),
  getApplication: (id) => request(`/api/applications/${encodeURIComponent(id)}`),
  analyzeApplication: (id, body) => request(`/api/applications/${encodeURIComponent(id)}/analyze-with-resume`, json("POST", body)),
  saveJob: (body) => request("/api/workspaces", json("POST", body)),
  artifacts: (id) => request(`/api/applications/${encodeURIComponent(id)}/artifacts`),
  createPack: (id, body) => request(`/api/applications/${encodeURIComponent(id)}/packs`, json("POST", body)),
  getPack: (id) => request(`/api/packs/${encodeURIComponent(id)}`),
  generatePackResume: (id, body) => request(`/api/packs/${encodeURIComponent(id)}/resume`, json("POST", body)),
  generatePackCoverLetter: (id, body) => request(`/api/packs/${encodeURIComponent(id)}/cover-letter`, json("POST", body)),
  approvePackItem: (packId, itemId, body) => request(`/api/packs/${encodeURIComponent(packId)}/items/${encodeURIComponent(itemId)}/approve`, json("POST", body)),
  rejectPackItem: (packId, itemId, body) => request(`/api/packs/${encodeURIComponent(packId)}/items/${encodeURIComponent(itemId)}/reject`, json("POST", body)),
  regeneratePackItem: (packId, itemId, body) => request(`/api/packs/${encodeURIComponent(packId)}/items/${encodeURIComponent(itemId)}/regenerate`, json("POST", body)),
  createSession: (body) => request("/sessions", json("POST", { capability_profile: "job_assistant_readonly", ...body })),
  listSessions: () => request("/sessions?limit=100"),
  getSession: (id) => request(`/sessions/${encodeURIComponent(id)}`),
  deleteSession: (id, version) => request(`/sessions/${encodeURIComponent(id)}?expected_version=${version}`, { method: "DELETE" }),
  messages: (id) => request(`/sessions/${encodeURIComponent(id)}/messages`),
  sendMessage: (id, body) => request(`/sessions/${encodeURIComponent(id)}/messages`, json("POST", body)),
  listEvidence: () => request("/api/evidence?limit=100"),
  createEvidence: (body) => request("/api/evidence/candidates", json("POST", body)),
  confirmEvidence: (id, version) => request(`/api/evidence/${encodeURIComponent(id)}/confirm`, json("POST", { expected_version: version })),
  rejectEvidence: (id, version) => request(`/api/evidence/${encodeURIComponent(id)}/reject`, json("POST", { expected_version: version, reason: "Rejected in web workspace" })),
  listMemories: () => request("/memories"),
  createMemory: (body) => request("/memories", json("POST", body)),
  confirmMemory: (id, version) => request(`/memories/${encodeURIComponent(id)}/confirm`, json("POST", { expected_version: version })),
  rejectMemory: (id, version) => request(`/memories/${encodeURIComponent(id)}/reject`, json("POST", { expected_version: version })),
  deleteMemory: (id, version) => request(`/memories/${encodeURIComponent(id)}?expected_version=${version}`, { method: "DELETE" }),
  listSkills: () => request("/skills"),
  skillVersions: (name) => request(`/skills/${encodeURIComponent(name)}/versions`),
  approveSkill: (id, version) => request(`/skill-versions/${encodeURIComponent(id)}/approve`, json("POST", { expected_version: version })),
  activateSkill: (id, version) => request(`/skill-versions/${encodeURIComponent(id)}/activate`, json("POST", { expected_version: version })),
  rejectSkill: (id, version) => request(`/skill-versions/${encodeURIComponent(id)}/reject`, json("POST", { expected_version: version })),
  retireSkill: (id, version) => request(`/skill-versions/${encodeURIComponent(id)}/retire`, json("POST", { expected_version: version })),
  listResumes: () => request("/api/resumes"),
  uploadResume: async (file) => request("/api/resumes", { method: "POST", body: await file.arrayBuffer(), headers: { "Content-Type": "application/pdf", "X-Resume-Filename": encodeURIComponent(file.name), "X-Resume-Name": encodeURIComponent(file.name.replace(/\.pdf$/i, "")) } }),
  setDefaultResume: (id) => request(`/api/resumes/${encodeURIComponent(id)}/default`, { method: "POST" }),
  deleteResume: (id) => request(`/api/resumes/${encodeURIComponent(id)}`, { method: "DELETE" }),
  startMockInterview: (id) => request(`/api/applications/${encodeURIComponent(id)}/mock-interviews`, json("POST", { mode: "mixed", difficulty: "standard", target_question_count: 5, max_followups_per_question: 1, idempotency_key: crypto.randomUUID(), new_attempt: false })),
  activeMockInterview: (id) => request(`/api/applications/${encodeURIComponent(id)}/mock-interviews/active`),
  packs: (id) => request(`/api/applications/${encodeURIComponent(id)}/packs`),
  taskActivity: (id) => request(`/api/applications/${encodeURIComponent(id)}/agent-tasks`),
  multiAgentRuns: (id) => request(`/api/applications/${encodeURIComponent(id)}/multi-agent-runs`),
};
