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
    return validation || "This run no longer exists.";
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
