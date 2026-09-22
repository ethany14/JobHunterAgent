"use strict";

import { analyzeApplication, getApplication, listApplicationArtifacts, listApplicationEvents, listApplications, saveWorkspace, transitionApplication, updateApplication } from "./api-client.js";
import { CURRENT_APPLICATION_KEY, LAST_OPENED_APPLICATION_KEY, canSaveJob, createWorkspaceState } from "./workspace-state.js";
import { renderApplicationDetail, renderApplicationList } from "./workspace-renderer.js";

export function createWorkspaceController({ elements, getResume, getJobText, getExtraction, onAnalyzeRun, onOpenAssistant, onWorkspaceOpen, onMessage }) {
  const state = createWorkspaceState();
  const message = (text, kind = "info") => onMessage(text, kind);
  const setBusy = (busy) => { state.busy = busy; elements.save.disabled = !canSaveJob(getJobText(), busy); elements.refresh.disabled = busy; elements.analyze.disabled = busy; };

  async function save() {
    const text = getJobText().trim(); if (!canSaveJob(text, state.busy)) return;
    setBusy(true); message("Saving Job...");
    try {
      const extracted = getExtraction() || {};
      const rawText = typeof extracted.raw_page_text === "string" && extracted.raw_page_text.length <= 50_000
        ? extracted.raw_page_text : null;
      const result = await saveWorkspace({ source_url: extracted.source_url || null, source_site: extracted.source_site || null, company: extracted.company || null, title: extracted.job_title || null, location: extracted.location || null, raw_page_text: rawText, cleaned_job_description: text, extraction_metadata: { page_title: extracted.page_title || null, extraction_source: extracted.extraction_source || "manual", extraction_confidence: extracted.extraction_confidence ?? null, extracted_at: extracted.extracted_at || new Date().toISOString() }, reopen_existing: true });
      state.current = result.application;
      await chrome.storage.local.set({ [CURRENT_APPLICATION_KEY]: result.application.application_id, [LAST_OPENED_APPLICATION_KEY]: result.application.application_id });
      elements.savedId.textContent = `Application ${result.application.application_id}`; elements.open.hidden = false;
      message(result.duplicate_detected ? "This job was already saved. Opened the existing active Workspace." : "Saved. Analysis has not started yet.", "success");
      await refresh();
    } catch (error) { message(error.message || "Could not save this job.", "error"); } finally { setBusy(false); }
  }

  async function refresh({ append = false } = {}) {
    setBusy(true);
    try {
      const response = await listApplications({ status: elements.filter.value, search: elements.search.value.trim(), cursor: append ? state.nextCursor || "" : "" });
      state.items = append ? [...state.items, ...response.applications] : response.applications;
      state.nextCursor = response.next_cursor; renderApplicationList(elements.list, state.items, open);
      elements.more.hidden = !state.nextCursor;
    } catch (error) { message(error.message || "Could not load Applications.", "error"); } finally { setBusy(false); }
  }

  async function open(applicationId) {
    setBusy(true);
    try {
      const [detail, events, artifacts] = await Promise.all([getApplication(applicationId), listApplicationEvents(applicationId), listApplicationArtifacts(applicationId)]);
      state.current = detail;
      await chrome.storage.local.set({ [CURRENT_APPLICATION_KEY]: applicationId, [LAST_OPENED_APPLICATION_KEY]: applicationId });
      renderApplicationDetail(elements, detail, events.events, artifacts.artifacts); message("Workspace loaded.", "success");
      if (onWorkspaceOpen) onWorkspaceOpen(applicationId);
    } catch (error) {
      if (error.status === 404) await chrome.storage.local.remove(CURRENT_APPLICATION_KEY);
      message(error.message || "Could not open the Workspace.", "error");
    } finally { setBusy(false); }
  }

  async function analyze() {
    if (!state.current) return; const resume = getResume().trim(); if (!resume) { message("Resume is required.", "error"); return; }
    setBusy(true); message("Analyzing this saved Application...");
    try {
      const response = await analyzeApplication(state.current.application_id, state.current.current_snapshot_id, resume, state.current.version);
      state.current = response.application; await open(state.current.application_id);
      if (response.run_id) await onAnalyzeRun(response.run_id);
      message(response.run_status === "failed" ? "Analysis failed safely. You can retry." : "Analysis reached human review and Workspace artifacts were saved.", response.run_status === "failed" ? "error" : "success");
    } catch (error) { if (error.status === 409) await open(state.current.application_id); message(error.message || "Workspace analysis failed.", "error"); } finally { setBusy(false); }
  }

  async function update() {
    if (!state.current) return; setBusy(true);
    try { state.current = await updateApplication(state.current.application_id, { expected_version: state.current.version, next_action: elements.nextAction.value.trim() || null, deadline_at: elements.deadline.value ? new Date(elements.deadline.value).toISOString() : null }); await open(state.current.application_id); }
    catch (error) { if (error.status === 409) await open(state.current.application_id); message(error.message || "Could not update Workspace.", "error"); } finally { setBusy(false); }
  }

  async function transition() {
    if (!state.current) return; setBusy(true);
    try { state.current = await transitionApplication(state.current.application_id, elements.statusSelect.value, state.current.version, elements.statusSelect.value === "applied" ? new Date().toISOString() : null); await open(state.current.application_id); }
    catch (error) { if (error.status === 409) await open(state.current.application_id); message(error.message || "Status change was rejected.", "error"); } finally { setBusy(false); }
  }

  async function restore() { const stored = await chrome.storage.local.get(CURRENT_APPLICATION_KEY); if (stored[CURRENT_APPLICATION_KEY]) await open(stored[CURRENT_APPLICATION_KEY]); }
  elements.save.addEventListener("click", save); elements.open.addEventListener("click", () => state.current && open(state.current.application_id)); elements.refresh.addEventListener("click", () => refresh()); elements.more.addEventListener("click", () => refresh({ append: true })); elements.search.addEventListener("change", () => refresh()); elements.filter.addEventListener("change", () => refresh()); elements.analyze.addEventListener("click", analyze); elements.update.addEventListener("click", update); elements.transition.addEventListener("click", transition); elements.assistant.addEventListener("click", () => state.current && onOpenAssistant(state.current.application_id));
  return { state, refresh, open, restore, analyze, save,
    updateSaveState: () => { elements.save.disabled = !canSaveJob(getJobText(), state.busy); } };
}
