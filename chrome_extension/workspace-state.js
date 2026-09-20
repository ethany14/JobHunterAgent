"use strict";

export const CURRENT_APPLICATION_KEY = "jobAgentCurrentApplicationId";
export const LAST_OPENED_APPLICATION_KEY = "jobAgentLastOpenedApplicationId";

export function createWorkspaceState() {
  return { current: null, items: [], nextCursor: null, busy: false };
}

export function safeHttpUrl(value) {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? url.href : null;
  } catch (_error) {
    return null;
  }
}

export function canSaveJob(text, busy = false) {
  return !busy && typeof text === "string" && text.trim().length > 0 && text.length <= 50_000;
}
