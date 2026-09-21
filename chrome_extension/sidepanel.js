"use strict";

import {
  ApiError,
  activateSkill,
  approveSkill,
  approveToolCall,
  cancelSession,
  confirmMemory,
  createMemory,
  createRun,
  createSession,
  deleteMemory,
  getRun,
  getSession,
  getSessionContext,
  getSessionMessages,
  health,
  listMemories,
  listSessions,
  listSkills,
  listSkillVersions,
  recoverSession,
  rejectMemory,
  rejectSkill,
  rejectToolCall,
  retireSkill,
  reviewRun,
  sendSessionMessage,
  supersedeMemory,
} from "./api-client.js";
import { extractJobDescriptionFromPage } from "./extractor.js";
import { createWorkspaceController } from "./workspace-controller.js";
import { createMultiAgentController } from "./multi-agent-controller.js";
import { createEvidenceController } from "./evidence-controller.js";
import { createInterviewController } from "./interview-controller.js";
import { createMockInterviewController } from "./mock-interview-controller.js";
import { createPackController } from "./pack-controller.js";
import { createTaskActivity } from "./task-activity.js";

const MAX_TEXT_LENGTH = 50_000;
const POLL_INTERVAL_MS = 1_500;
const POLL_TIMEOUT_MS = 60_000;
const SAVED_RESUME_KEY = "jobAgentSavedResume";
const ACTIVE_JOB_TAB_KEY = "jobAgentActiveTab";
const ACTIVE_SESSION_KEY = "jobAgentActiveSessionId";
const MAX_SESSION_MESSAGE_LENGTH = 20_000;
const SESSION_POLL_INTERVAL_MS = 1_500;
const SESSION_POLL_TIMEOUT_MS = 30_000;
const TERMINAL_SESSION_STATUSES = new Set(["completed", "failed", "cancelled", "timed_out"]);

const elements = {
  healthButton: document.querySelector("#health-button"),
  healthText: document.querySelector("#health-text"),
  resume: document.querySelector("#resume-text"),
  saveResume: document.querySelector("#save-resume"),
  clearResume: document.querySelector("#clear-resume-button"),
  resumeCount: document.querySelector("#resume-count"),
  job: document.querySelector("#job-description"),
  jobTitle: document.querySelector("#job-title"),
  jobCompany: document.querySelector("#job-company"),
  jobLocation: document.querySelector("#job-location"),
  jobCount: document.querySelector("#job-count"),
  extractionSource: document.querySelector("#extraction-source"),
  extract: document.querySelector("#extract-button"),
  analyze: document.querySelector("#analyze-button"),
  message: document.querySelector("#message"),
  results: document.querySelector("#results"),
  runStatus: document.querySelector("#run-status"),
  matchScore: document.querySelector("#match-score"),
  matchedList: document.querySelector("#matched-list"),
  partialList: document.querySelector("#partial-list"),
  missingList: document.querySelector("#missing-list"),
  confirmationList: document.querySelector("#confirmation-list"),
  tailoredResume: document.querySelector("#tailored-resume"),
  verification: document.querySelector("#verification"),
  copy: document.querySelector("#copy-button"),
  reviewPanel: document.querySelector("#review-panel"),
  approve: document.querySelector("#approve-button"),
  feedback: document.querySelector("#review-feedback"),
  reject: document.querySelector("#reject-button"),
  askRun: document.querySelector("#ask-run-button"),
  analysisTab: document.querySelector("#analysis-tab"),
  jobsTab: document.querySelector("#jobs-tab"),
  assistantTab: document.querySelector("#assistant-tab"),
  contextTab: document.querySelector("#context-tab"),
  analysisPanel: document.querySelector("#analysis-panel"),
  jobsPanel: document.querySelector("#jobs-panel"),
  assistantPanel: document.querySelector("#assistant-panel"),
  contextPanel: document.querySelector("#context-panel"),
  sessionMessage: document.querySelector("#session-message"),
  sessionStatus: document.querySelector("#session-status"),
  sessionRun: document.querySelector("#session-run"),
  assistantMessages: document.querySelector("#assistant-messages"),
  toolCallDetails: document.querySelector("#tool-call-details"),
  toolApprovals: document.querySelector("#tool-approvals"),
  assistantInput: document.querySelector("#assistant-input"),
  assistantCount: document.querySelector("#assistant-count"),
  newSession: document.querySelector("#new-session-button"),
  sendSession: document.querySelector("#send-session-button"),
  cancelSession: document.querySelector("#cancel-session-button"),
  recoverSession: document.querySelector("#recover-session-button"),
  sessionHistory: document.querySelector("#session-history"),
  refreshSessions: document.querySelector("#refresh-sessions-button"),
  contextMessage: document.querySelector("#context-message"),
  usedContext: document.querySelector("#used-context"),
  refreshContext: document.querySelector("#refresh-context-button"),
  memoryKey: document.querySelector("#memory-key"),
  memoryDisplay: document.querySelector("#memory-display"),
  memoryType: document.querySelector("#memory-type"),
  memoryScope: document.querySelector("#memory-scope"),
  memorySensitivity: document.querySelector("#memory-sensitivity"),
  memoryContent: document.querySelector("#memory-content"),
  createMemory: document.querySelector("#create-memory-button"),
  memoryList: document.querySelector("#memory-list"),
  refreshMemories: document.querySelector("#refresh-memories-button"),
  skillList: document.querySelector("#skill-list"),
  refreshSkills: document.querySelector("#refresh-skills-button"),
  evidenceList: document.querySelector("#evidence-list"),
  evidenceRefresh: document.querySelector("#refresh-evidence-button"),
  evidenceClaim: document.querySelector("#evidence-claim"),
  evidenceCategory: document.querySelector("#evidence-category"),
  evidenceCreate: document.querySelector("#create-evidence-button"),
  evidenceSearch: document.querySelector("#evidence-search"),
  evidenceFilter: document.querySelector("#evidence-filter"),
  interviewStart: document.querySelector("#start-interview-button"),
  interviewProgress: document.querySelector("#interview-progress"),
  interviewRequirement: document.querySelector("#interview-requirement"),
  interviewQuestion: document.querySelector("#interview-question"),
  interviewAnswer: document.querySelector("#interview-answer"),
  interviewSubmit: document.querySelector("#submit-interview-answer"),
  interviewNoExperience: document.querySelector("#interview-no-experience"),
  interviewSkip: document.querySelector("#interview-skip"),
  interviewCandidate: document.querySelector("#interview-candidate"),
  interviewSaveLater: document.querySelector("#interview-save-later"),
  interviewRecover: document.querySelector("#interview-resume"),
  interviewCancel: document.querySelector("#interview-cancel"),
  interviewStatusList: document.querySelector("#interview-status-list"),
  saveJob: document.querySelector("#save-job-button"),
  openWorkspace: document.querySelector("#open-workspace-button"),
  savedApplicationId: document.querySelector("#saved-application-id"),
  workspaceMessage: document.querySelector("#workspace-message"),
  applicationList: document.querySelector("#application-list"),
  applicationSearch: document.querySelector("#application-search"),
  applicationStatusFilter: document.querySelector("#application-status-filter"),
  refreshApplications: document.querySelector("#refresh-applications-button"),
  loadMoreApplications: document.querySelector("#load-more-applications-button"),
  workspaceDetail: document.querySelector("#workspace-detail"),
  workspaceTitle: document.querySelector("#workspace-title"),
  workspaceCompany: document.querySelector("#workspace-company"),
  workspaceSourceLink: document.querySelector("#workspace-source-link"),
  workspaceApplicationId: document.querySelector("#workspace-application-id"),
  workspaceStatus: document.querySelector("#workspace-status"),
  workspaceNextAction: document.querySelector("#workspace-next-action"),
  workspaceDeadline: document.querySelector("#workspace-deadline"),
  workspaceStatusSelect: document.querySelector("#workspace-status-select"),
  updateWorkspace: document.querySelector("#update-workspace-button"),
  transitionWorkspace: document.querySelector("#transition-workspace-button"),
  analyzeWorkspace: document.querySelector("#analyze-workspace-button"),
  workspaceAssistant: document.querySelector("#workspace-assistant-button"),
  workspaceMatchScore: document.querySelector("#workspace-match-score"),
  workspaceMissing: document.querySelector("#workspace-missing"),
  workspaceResume: document.querySelector("#workspace-resume"),
  workspaceRuns: document.querySelector("#workspace-runs"),
  workspaceEvents: document.querySelector("#workspace-events"),
  workspaceArtifacts: document.querySelector("#workspace-artifacts"),
  packStatus: document.querySelector("#pack-status"),
  packCreate: document.querySelector("#pack-create"),
  packResume: document.querySelector("#pack-resume"),
  packCover: document.querySelector("#pack-cover"),
  packQuestion: document.querySelector("#pack-question"),
  packMaxLength: document.querySelector("#pack-max-length"),
  packAnswer: document.querySelector("#pack-answer"),
  packTabResume: document.querySelector("#pack-tab-resume"),
  packTabCover: document.querySelector("#pack-tab-cover"),
  packTabAnswers: document.querySelector("#pack-tab-answers"),
  packItems: document.querySelector("#pack-items"),
};

let currentRunId = null;
let interviewController = null;
let mockInterviewController = null;
let packController = null;
let multiAgentController = null;
let currentResumeText = "";
let analysisBusy = false;
let sessionBusy = false;
let contextBusy = false;
let currentSession = null;
let currentExtraction = null;

function setMessage(text, kind = "info") {
  elements.message.textContent = text;
  elements.message.dataset.kind = kind;
  elements.message.hidden = !text;
}

function setBusy(value, message = "") {
  analysisBusy = value;
  elements.analyze.disabled = value;
  elements.extract.disabled = value;
  elements.approve.disabled = value;
  elements.reject.disabled = value;
  if (message) {
    setMessage(message, "info");
  }
}

function setSessionMessage(text, kind = "info") {
  elements.sessionMessage.textContent = text;
  elements.sessionMessage.dataset.kind = kind;
  elements.sessionMessage.hidden = !text;
}

function setSessionBusy(value, message = "") {
  sessionBusy = value;
  elements.newSession.disabled = value;
  elements.sendSession.disabled = value
    || !currentSession
    || !["active", "awaiting_user"].includes(currentSession.status);
  elements.cancelSession.disabled = value
    || !currentSession
    || TERMINAL_SESSION_STATUSES.has(currentSession.status);
  elements.recoverSession.disabled = value
    || !currentSession
    || currentSession.recovery_available !== true;
  elements.sessionHistory.disabled = value;
  elements.refreshSessions.disabled = value;
  if (message) {
    setSessionMessage(message, "info");
  }
}

function updateCount(textarea, counter) {
  const length = textarea.value.length;
  counter.textContent = `${length.toLocaleString()} / ${MAX_TEXT_LENGTH.toLocaleString()}`;
  counter.classList.toggle("over-limit", length > MAX_TEXT_LENGTH);
}

function validateText(label, value) {
  if (!value.trim()) {
    throw new Error(`${label} is required.`);
  }
  if (value.length > MAX_TEXT_LENGTH) {
    throw new Error(`${label} exceeds 50,000 characters. Select or keep only the relevant content.`);
  }
}

function appendTextElement(parent, tagName, text, className = "") {
  const element = document.createElement(tagName);
  element.textContent = text;
  if (className) {
    element.className = className;
  }
  parent.append(element);
  return element;
}

function replaceChildren(element) {
  while (element.firstChild) {
    element.removeChild(element.firstChild);
  }
}

function displayLabel(value, fallback) {
  if (typeof value !== "string" || !value.trim()) {
    return fallback;
  }
  return value
    .trim()
    .replaceAll("_", " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

function appendRequirementDetails(item, match) {
  const details = document.createElement("details");
  appendTextElement(details, "summary", "Evidence and reasoning");
  appendTextElement(
    details,
    "p",
    typeof match.match_reason === "string" && match.match_reason.trim()
      ? match.match_reason
      : "No match explanation was provided.",
    "match-reason",
  );

  const evidence = Array.isArray(match.resume_evidence)
    ? match.resume_evidence.filter((entry) => typeof entry === "string" && entry.trim())
    : [];
  appendTextElement(details, "strong", "Resume evidence", "evidence-heading");
  if (evidence.length) {
    const evidenceList = document.createElement("ul");
    evidence.forEach((entry) => appendTextElement(evidenceList, "li", entry));
    details.append(evidenceList);
  } else {
    appendTextElement(details, "p", "No resume evidence cited.", "muted");
  }
  item.append(details);
}

function renderRequirements(skillMatch, jobAnalysis) {
  const lists = {
    matched: elements.matchedList,
    partial: elements.partialList,
    missing: elements.missingList,
    needs_confirmation: elements.confirmationList,
  };
  Object.values(lists).forEach(replaceChildren);

  const requirements = Array.isArray(jobAnalysis?.requirements)
    ? jobAnalysis.requirements
    : [];
  const requirementsById = new Map(
    requirements.map((requirement) => [requirement.requirement_id, requirement]),
  );
  const matches = Array.isArray(skillMatch?.matches) ? skillMatch.matches : [];
  for (const match of matches) {
    const list = lists[match.match_status] || lists.missing;
    const requirement = requirementsById.get(match.requirement_id) || {};
    const item = document.createElement("li");
    const name = requirement.display_name
      || displayLabel(match.job_skill, "Unnamed requirement");
    appendTextElement(item, "span", name, "requirement-name");

    const metadata = [
      displayLabel(requirement.category, "Other"),
      displayLabel(requirement.level || match.requirement_level, "Requirement"),
    ];
    if (Number.isFinite(requirement.minimum_years)) {
      const unit = requirement.minimum_years === 1 ? "year" : "years";
      metadata.push(`Minimum ${requirement.minimum_years} ${unit}`);
    }
    if (
      ["matched", "partial"].includes(match.match_status)
      && Number.isFinite(match.confidence)
    ) {
      metadata.push(`Evidence strength: ${Math.round(match.confidence * 100)}%`);
    }
    appendTextElement(
      item,
      "span",
      metadata.join(" \u2022 "),
      "requirement-meta",
    );
    appendRequirementDetails(item, match);
    list.append(item);
  }

  for (const list of Object.values(lists)) {
    if (!list.childElementCount) {
      appendTextElement(list, "li", "None");
    }
  }
}
function claimTexts(claims) {
  return Array.isArray(claims)
    ? claims.map((claim) => claim?.text).filter((text) => typeof text === "string" && text.trim())
    : [];
}

function buildResumeText(tailoredResume) {
  const summary = claimTexts(tailoredResume?.professional_summary);
  const bullets = claimTexts(tailoredResume?.experience_bullets);
  const skills = claimTexts(tailoredResume?.highlighted_skills);
  const sections = [];
  if (summary.length) {
    sections.push(`PROFESSIONAL SUMMARY\n${summary.join(" ")}`);
  }
  if (bullets.length) {
    sections.push(`EXPERIENCE\n${bullets.map((text) => `- ${text}`).join("\n")}`);
  }
  if (skills.length) {
    sections.push(`SKILLS\n${skills.join(", ")}`);
  }
  return sections.join("\n\n");
}

function renderResume(tailoredResume) {
  replaceChildren(elements.tailoredResume);
  const sections = [
    ["Professional Summary", claimTexts(tailoredResume?.professional_summary), false],
    ["Experience", claimTexts(tailoredResume?.experience_bullets), true],
    ["Highlighted Skills", claimTexts(tailoredResume?.highlighted_skills), true],
  ];
  for (const [title, texts, asList] of sections) {
    if (!texts.length) {
      continue;
    }
    const section = document.createElement("section");
    appendTextElement(section, "h3", title);
    if (asList) {
      const list = document.createElement("ul");
      if (title === "Highlighted Skills") {
        list.className = "skills-list";
      }
      texts.forEach((text) => appendTextElement(list, "li", text));
      section.append(list);
    } else {
      texts.forEach((text) => appendTextElement(section, "p", text));
    }
    elements.tailoredResume.append(section);
  }
  currentResumeText = buildResumeText(tailoredResume);
  elements.copy.disabled = !currentResumeText;
}

function renderVerification(verification) {
  replaceChildren(elements.verification);
  if (!verification) {
    elements.verification.className = "verification";
    return;
  }
  elements.verification.className = `verification ${verification.passed ? "passed" : "failed"}`;
  appendTextElement(
    elements.verification,
    "strong",
    verification.passed ? "Verification passed" : "Verification needs attention",
  );
  const unsupported = Array.isArray(verification.unsupported_claims)
    ? verification.unsupported_claims
    : [];
  if (unsupported.length) {
    const list = document.createElement("ul");
    unsupported.forEach((item) => {
      appendTextElement(list, "li", `${item.claim}: ${item.reason}`);
    });
    elements.verification.append(list);
  }
}

function publicStatus(status) {
  const labels = {
    running: "Running",
    awaiting_review: "Awaiting review",
    revising: "Revising",
    approved: "Approved",
    failed: "Failed",
  };
  return labels[status] || status || "Unknown";
}

function renderRun(run) {
  currentRunId = run.run_id || currentRunId;
  elements.askRun.disabled = !currentRunId;
  elements.results.hidden = false;
  elements.runStatus.textContent = publicStatus(run.status);
  elements.reviewPanel.hidden = run.status !== "awaiting_review";

  if (run.result) {
    const score = run.result.skill_match?.overall_score;
    elements.matchScore.textContent = Number.isFinite(score) ? `${score.toFixed(1)}%` : "\u2014";
    renderRequirements(run.result.skill_match, run.result.job_analysis);
    renderResume(run.result.tailored_resume);
    renderVerification(run.result.verification);
  }

  if (run.status === "failed") {
    setMessage(run.error || "The agent failed to complete this run.", "error");
  } else if (run.status === "awaiting_review") {
    setMessage("The tailored resume is ready for your review.", "success");
  } else if (run.status === "approved") {
    setMessage("This tailored resume has been approved.", "success");
  } else {
    setMessage(`Agent status: ${publicStatus(run.status)}.`, "info");
  }
}

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function resolveRun(run) {
  const startedAt = Date.now();
  let current = run;
  while (["running", "revising"].includes(current.status)) {
    if (Date.now() - startedAt >= POLL_TIMEOUT_MS) {
      throw new ApiError("The run is still processing. Use Analyze again only if you want to start a new run.");
    }
    await delay(POLL_INTERVAL_MS);
    current = await getRun(current.run_id);
    renderRun(current);
  }
  return current;
}

function showError(error) {
  const message = error instanceof Error ? error.message : "An unexpected error occurred.";
  setMessage(message, "error");
}

function pageAccessError(error, tab) {
  const message = String(error?.message || "");
  const url = typeof tab?.url === "string" ? tab.url : "";
  const protectedScheme = url && !url.startsWith("http://") && !url.startsWith("https://");
  const accessMissing = message.includes("Cannot access contents")
    || message.includes("Missing host permission")
    || message.includes("Extension manifest must request permission");

  if (protectedScheme || message.includes("extensions gallery cannot be scripted")) {
    return new Error(
      "Chrome blocks extraction from this protected page. Open a regular HTTP or HTTPS job page, or paste the description manually.",
    );
  }
  if (accessMissing) {
    return new Error(
      "Page access is no longer active for the selected job tab. Keep the job page active and click the Job Agent toolbar icon again.",
    );
  }
  return error;
}

async function checkHealth() {
  elements.healthButton.dataset.state = "checking";
  elements.healthText.textContent = "Checking backend...";
  try {
    const response = await health();
    const healthy = response?.status === "ok";
    elements.healthButton.dataset.state = healthy ? "healthy" : "unhealthy";
    elements.healthText.textContent = healthy ? "Backend ready" : "Backend unavailable";
  } catch (_error) {
    elements.healthButton.dataset.state = "unhealthy";
    elements.healthText.textContent = "Backend unavailable";
  }
}

async function getTargetJobTab() {
  const stored = await chrome.storage.session.get(ACTIVE_JOB_TAB_KEY);
  const target = stored[ACTIVE_JOB_TAB_KEY];

  if (!target?.tabId) {
    throw new Error("Open a job page and click the Job Agent toolbar icon first.");
  }

  if (!target.validPage) {
    throw new Error(
      "Chrome cannot extract content from this protected page. Open a regular HTTP or HTTPS job page.",
    );
  }

  const [activeTab] = await chrome.tabs.query({
    active: true,
    currentWindow: true,
  });

  if (activeTab?.id !== target.tabId) {
    throw new Error(
      "The active tab has changed. Click the Job Agent toolbar icon again on the job page you want to analyze.",
    );
  }

  return target;
}

async function extractJobDescription() {
  if (analysisBusy) {
    return;
  }
  setBusy(true, "Reading the current page...");
  let tab = null;
  try {
    const target = await getTargetJobTab();
    tab = { id: target.tabId, url: target.url };
    const injection = await chrome.scripting.executeScript({
      target: { tabId: target.tabId },
      func: extractJobDescriptionFromPage,
    });
    const extracted = injection?.[0]?.result;
    if (!extracted?.cleaned_job_description) {
      throw new Error("No job description text was found. Select the job text or paste it manually.");
    }
    currentExtraction = extracted;
    elements.job.value = extracted.cleaned_job_description;
    elements.jobTitle.value = extracted.job_title || "";
    elements.jobCompany.value = extracted.company || "";
    elements.jobLocation.value = extracted.location || "";
    const confidence = Number.isFinite(extracted.extraction_confidence)
      ? `confidence ${Math.round(extracted.extraction_confidence * 100)}/100`
      : "confidence unavailable";
    elements.extractionSource.textContent = `Extracted from ${extracted.extraction_source} • ${confidence}`;
    updateCount(elements.job, elements.jobCount);
    if (extracted.cleaned_job_description.length > MAX_TEXT_LENGTH) {
      throw new Error("The extracted page exceeds 50,000 characters. Select only the job description and extract again, or edit the text below.");
    }
    if (extracted.extraction_confidence < 0.6) {
      setMessage(
        "Chrome could not isolate a reliable JD container. Select the job-description text on the page and extract again, or edit the result before saving.",
        "error",
      );
    } else {
      setMessage("Job description and metadata extracted. Review or edit them before saving.", "success");
    }
  } catch (error) {
    showError(pageAccessError(error, tab));
  } finally {
    setBusy(false);
  }
}

async function analyze() {
  if (analysisBusy) {
    return;
  }
  try {
    validateText("Resume", elements.resume.value);
    validateText("Job description", elements.job.value);
  } catch (error) {
    showError(error);
    return;
  }

  setBusy(true, "Analyzing the resume and job description...");
  elements.results.hidden = true;
  currentRunId = null;
  try {
    if (elements.saveResume.checked) {
      await chrome.storage.local.set({ [SAVED_RESUME_KEY]: elements.resume.value });
    }
    const created = await createRun(elements.resume.value, elements.job.value);
    currentRunId = created.run_id;
    renderRun({ ...created, result: null, error: null });
    const complete = await resolveRun(await getRun(created.run_id));
    renderRun(complete);
  } catch (error) {
    showError(error);
  } finally {
    setBusy(false);
  }
}

async function submitReview(approved) {
  if (analysisBusy || !currentRunId) {
    return;
  }
  const feedback = elements.feedback.value.trim();
  if (!approved && !feedback) {
    setMessage("Revision feedback is required when rejecting a resume.", "error");
    elements.feedback.focus();
    return;
  }
  setBusy(true, approved ? "Approving this resume..." : "Revising this resume...");
  try {
    const reviewed = await reviewRun(currentRunId, approved, approved ? null : feedback);
    renderRun(reviewed);
    const complete = await resolveRun(reviewed);
    renderRun(complete);
    if (!approved) {
      elements.feedback.value = "";
    }
  } catch (error) {
    showError(error);
  } finally {
    setBusy(false);
  }
}

async function copyResume() {
  if (!currentResumeText) {
    setMessage("No tailored resume is available to copy.", "error");
    return;
  }
  try {
    await navigator.clipboard.writeText(currentResumeText);
    setMessage("Tailored resume copied to the clipboard.", "success");
  } catch (_error) {
    setMessage("Chrome could not copy the resume. Select the displayed text and copy it manually.", "error");
  }
}

async function loadSavedResume() {
  const stored = await chrome.storage.local.get(SAVED_RESUME_KEY);
  if (typeof stored[SAVED_RESUME_KEY] === "string" && stored[SAVED_RESUME_KEY]) {
    elements.resume.value = stored[SAVED_RESUME_KEY];
    elements.saveResume.checked = true;
    updateCount(elements.resume, elements.resumeCount);
  }
}

async function clearSavedResume() {
  await chrome.storage.local.remove(SAVED_RESUME_KEY);
  elements.saveResume.checked = false;
  elements.resume.value = "";
  updateCount(elements.resume, elements.resumeCount);
  setMessage("The locally saved resume was cleared.", "success");
}

function switchPanel(name) {
  const assistant = name === "assistant";
  const context = name === "context";
  const jobs = name === "jobs";
  const analysis = !assistant && !context && !jobs;
  elements.analysisPanel.hidden = !analysis;
  elements.assistantPanel.hidden = !assistant;
  elements.contextPanel.hidden = !context;
  elements.jobsPanel.hidden = !jobs;
  elements.analysisTab.classList.toggle("active", analysis);
  elements.assistantTab.classList.toggle("active", assistant);
  elements.contextTab.classList.toggle("active", context);
  elements.jobsTab.classList.toggle("active", jobs);
  elements.analysisTab.setAttribute("aria-selected", String(analysis));
  elements.assistantTab.setAttribute("aria-selected", String(assistant));
  elements.contextTab.setAttribute("aria-selected", String(context));
  elements.jobsTab.setAttribute("aria-selected", String(jobs));
}

function sessionStatusLabel(status) {
  const labels = {
    active: "Active",
    running: "Running",
    awaiting_user: "Awaiting user",
    awaiting_tool_approval: "Awaiting tool approval",
    completed: "Completed",
    failed: "Failed",
    cancelled: "Cancelled",
    timed_out: "Timed out",
  };
  return labels[status] || "No session";
}

function updateSessionCount() {
  const length = elements.assistantInput.value.length;
  elements.assistantCount.textContent = `${length.toLocaleString()} / ${MAX_SESSION_MESSAGE_LENGTH.toLocaleString()}`;
  elements.assistantCount.classList.toggle("over-limit", length > MAX_SESSION_MESSAGE_LENGTH);
}

function renderSessionMessages(messages) {
  replaceChildren(elements.assistantMessages);
  const publicMessages = Array.isArray(messages)
    ? messages.filter((item) => ["user", "assistant"].includes(item?.role))
    : [];
  if (!publicMessages.length) {
    appendTextElement(
      elements.assistantMessages,
      "p",
      currentSession ? "No conversation messages yet." : "Start a session to ask about your saved Job Agent runs.",
      "muted empty-session",
    );
    return;
  }
  for (const item of publicMessages) {
    const article = document.createElement("article");
    article.className = `chat-message ${item.role}`;
    appendTextElement(article, "span", item.role === "user" ? "You" : "Assistant", "chat-role");
    appendTextElement(article, "p", typeof item.content === "string" ? item.content : "");
    elements.assistantMessages.append(article);
  }
  elements.assistantMessages.scrollTop = elements.assistantMessages.scrollHeight;
}

function renderToolApprovals(approvals) {
  replaceChildren(elements.toolApprovals);
  const safeApprovals = Array.isArray(approvals) ? approvals : [];
  elements.toolApprovals.hidden = !safeApprovals.length;
  for (const approval of safeApprovals) {
    const card = document.createElement("section");
    card.className = "card approval-card";
    appendTextElement(card, "p", "Approval required", "step-label");
    appendTextElement(card, "h2", approval.tool_name || "Tool call");
    appendTextElement(card, "p", approval.description || "", "hint");
    const metadata = [
      approval.tool_version ? `Version ${approval.tool_version}` : null,
      approval.side_effect ? `Side effect: ${approval.side_effect}` : null,
      approval.data_classification ? `Data: ${approval.data_classification}` : null,
    ].filter(Boolean);
    appendTextElement(card, "p", metadata.join(" • "), "requirement-meta");
    appendTextElement(card, "strong", "Arguments (redacted by server)");
    appendTextElement(card, "pre", JSON.stringify(approval.arguments || {}, null, 2), "approval-arguments");
    const actions = document.createElement("div");
    actions.className = "assistant-actions";
    const approve = appendTextElement(actions, "button", "Approve", "primary-button");
    approve.type = "button";
    approve.addEventListener("click", () => respondToToolApproval(approval.call_id, true));
    const reject = appendTextElement(actions, "button", "Reject", "secondary-button");
    reject.type = "button";
    reject.addEventListener("click", () => respondToToolApproval(approval.call_id, false));
    card.append(actions);
    elements.toolApprovals.append(card);
  }
}

function renderToolCalls(toolCalls) {
  replaceChildren(elements.toolCallDetails);
  const calls = Array.isArray(toolCalls) ? toolCalls : [];
  elements.toolCallDetails.hidden = !calls.length;
  for (const call of calls) {
    const details = document.createElement("details");
    details.className = "card tool-call-card";
    const provider = call.provider === "MCP"
      ? `MCP · ${call.mcp_server_id || "unknown server"}`
      : "Built-in";
    const summary = document.createElement("summary");
    appendTextElement(summary, "span", provider, "step-label");
    appendTextElement(summary, "strong", call.display_name || call.public_tool_name || "Tool call");
    details.append(summary);
    const sideEffect = call.side_effect === "none" ? "Read-only" : displayLabel(call.side_effect, "Unknown effect");
    const duration = Number.isFinite(call.duration_ms) ? `${call.duration_ms} ms` : null;
    appendTextElement(
      details,
      "p",
      [displayLabel(call.status, "Unknown"), duration, sideEffect].filter(Boolean).join(" · "),
      "requirement-meta",
    );
    appendTextElement(
      details,
      "p",
      `Approval: ${displayLabel(call.approval_status, "Unknown")}`,
      "muted",
    );
    if (call.idempotently_reused === true) {
      appendTextElement(details, "span", "Reused", "tool-badge");
    }
    if (call.result_truncated === true) {
      appendTextElement(details, "p", "The persisted tool result was truncated for safety.", "tool-warning");
    }
    elements.toolCallDetails.append(details);
  }
}

function renderSession(response) {
  if (!response?.session) {
    return;
  }
  currentSession = response.session;
  elements.sessionStatus.textContent = sessionStatusLabel(currentSession.status);
  elements.sessionStatus.dataset.status = currentSession.status;
  elements.sessionRun.textContent = currentSession.active_application_id
    ? `Workspace ${currentSession.active_application_id}`
    : currentSession.active_run_id
      ? `Run ${currentSession.active_run_id}`
      : "General run assistant";
  elements.recoverSession.hidden = currentSession.recovery_available !== true;
  elements.cancelSession.hidden = TERMINAL_SESSION_STATUSES.has(currentSession.status);
  elements.sendSession.disabled = sessionBusy
    || !["active", "awaiting_user"].includes(currentSession.status);
  renderToolApprovals(response.pending_tool_approvals);
  renderToolCalls(response.tool_calls);
  if (currentSession.status === "failed") {
    setSessionMessage(currentSession.error_message || "The session failed safely.", "error");
  } else if (currentSession.status === "timed_out") {
    setSessionMessage("The session turn timed out. Recover it only when the backend allows recovery.", "error");
  } else if (currentSession.status === "cancelled") {
    setSessionMessage("The session was cancelled.", "info");
  } else if (currentSession.status === "awaiting_tool_approval") {
    setSessionMessage("Review the pending tool request before the assistant continues.", "info");
  } else if (response.response) {
    setSessionMessage("Assistant response ready.", "success");
  }
}

async function refreshSession() {
  if (!currentSession?.session_id) {
    return null;
  }
  const response = await getSession(currentSession.session_id);
  renderSession(response);
  const messageResponse = await getSessionMessages(currentSession.session_id);
  renderSessionMessages(messageResponse.messages);
  await refreshContextSummary({ quiet: true });
  return response;
}

function historyLabel(session) {
  const title = typeof session.title === "string" && session.title.trim()
    ? session.title.trim()
    : session.active_run_id
      ? `Run ${session.active_run_id}`
      : "General conversation";
  const date = new Date(session.updated_at);
  const updated = Number.isNaN(date.getTime()) ? "" : date.toLocaleString();
  return `${title} • ${sessionStatusLabel(session.status)}${updated ? ` • ${updated}` : ""}`;
}

async function loadSessionHistory() {
  const response = await listSessions(50);
  const sessions = Array.isArray(response?.sessions) ? response.sessions : [];
  replaceChildren(elements.sessionHistory);
  if (!sessions.length) {
    const option = appendTextElement(elements.sessionHistory, "option", "No saved conversations");
    option.value = "";
    return sessions;
  }
  const placeholder = appendTextElement(elements.sessionHistory, "option", "Choose a previous conversation");
  placeholder.value = "";
  for (const session of sessions) {
    const option = appendTextElement(elements.sessionHistory, "option", historyLabel(session));
    option.value = session.session_id;
    option.selected = session.session_id === currentSession?.session_id;
  }
  return sessions;
}

async function openSession(sessionId) {
  if (sessionBusy || !sessionId) {
    return;
  }
  setSessionBusy(true, "Loading the previous conversation...");
  try {
    const response = await getSession(sessionId);
    renderSession(response);
    await chrome.storage.local.set({ [ACTIVE_SESSION_KEY]: sessionId });
    const messages = await getSessionMessages(sessionId);
    renderSessionMessages(messages.messages);
    switchPanel("assistant");
    if (currentSession?.status === "running") {
      await pollRestoredSession();
    }
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      const stored = await chrome.storage.local.get(ACTIVE_SESSION_KEY);
      if (stored[ACTIVE_SESSION_KEY] === sessionId) {
        await chrome.storage.local.remove(ACTIVE_SESSION_KEY);
      }
      setSessionMessage("That conversation no longer exists on the backend.", "error");
      await loadSessionHistory();
    } else {
      showSessionError(error);
    }
  } finally {
    setSessionBusy(false);
  }
}

async function startSession(activeRunId = null, applicationId = null) {
  if (sessionBusy) {
    return;
  }
  setSessionBusy(true, "Creating a persistent session...");
  switchPanel("assistant");
  try {
    const response = await createSession(activeRunId, applicationId);
    currentSession = response.session;
    await chrome.storage.local.set({ [ACTIVE_SESSION_KEY]: currentSession.session_id });
    renderSession(response);
    renderSessionMessages([]);
    await loadSessionHistory();
    setSessionMessage(
      applicationId ? "Session created for the current Workspace." : activeRunId ? "Session created for the current run." : "New session created.",
      "success",
    );
    elements.assistantInput.focus();
  } catch (error) {
    showSessionError(error);
  } finally {
    setSessionBusy(false);
  }
}

function showSessionError(error) {
  setSessionMessage(
    error instanceof Error ? error.message : "The session request failed.",
    "error",
  );
}

async function handleConcurrency(error) {
  if (!(error instanceof ApiError) || error.status !== 409) {
    return false;
  }
  try {
    await refreshSession();
  } catch (refreshError) {
    showSessionError(refreshError);
    return true;
  }
  setSessionMessage(
    "The session changed in another request. The latest state was loaded; your message was not resent.",
    "error",
  );
  return true;
}

async function sendMessage() {
  if (sessionBusy || !currentSession) {
    return;
  }
  const content = elements.assistantInput.value.trim();
  if (!content) {
    setSessionMessage("Enter a message before sending.", "error");
    elements.assistantInput.focus();
    return;
  }
  if (content.length > MAX_SESSION_MESSAGE_LENGTH) {
    setSessionMessage("The message exceeds 20,000 characters.", "error");
    return;
  }
  setSessionBusy(true, "The assistant is working...");
  try {
    const response = await sendSessionMessage(
      currentSession.session_id,
      crypto.randomUUID(),
      content,
      currentSession.version,
    );
    elements.assistantInput.value = "";
    updateSessionCount();
    renderSession(response);
    await refreshSession();
  } catch (error) {
    if (!(await handleConcurrency(error))) {
      showSessionError(error);
    }
  } finally {
    setSessionBusy(false);
  }
}

async function respondToToolApproval(toolCallId, approved) {
  if (sessionBusy || !currentSession) {
    return;
  }
  setSessionBusy(true, approved ? "Approving tool call..." : "Rejecting tool call...");
  try {
    const response = approved
      ? await approveToolCall(currentSession.session_id, toolCallId, currentSession.version)
      : await rejectToolCall(currentSession.session_id, toolCallId, currentSession.version);
    renderSession(response);
    await refreshSession();
  } catch (error) {
    if (!(await handleConcurrency(error))) {
      showSessionError(error);
    }
  } finally {
    setSessionBusy(false);
  }
}

async function cancelCurrentSession() {
  if (sessionBusy || !currentSession) {
    return;
  }
  setSessionBusy(true, "Cancelling the session...");
  try {
    const response = await cancelSession(
      currentSession.session_id,
      currentSession.version,
      "Cancelled by the user from the Chrome Side Panel.",
    );
    renderSession(response);
    await refreshSession();
  } catch (error) {
    if (!(await handleConcurrency(error))) {
      showSessionError(error);
    }
  } finally {
    setSessionBusy(false);
  }
}

async function recoverCurrentSession() {
  if (sessionBusy || !currentSession || currentSession.recovery_available !== true) {
    return;
  }
  setSessionBusy(true, "Recovering the persisted session...");
  try {
    const response = await recoverSession(currentSession.session_id, currentSession.version);
    renderSession(response);
    await refreshSession();
  } catch (error) {
    if (!(await handleConcurrency(error))) {
      showSessionError(error);
    }
  } finally {
    setSessionBusy(false);
  }
}

async function pollRestoredSession() {
  const startedAt = Date.now();
  while (currentSession?.status === "running") {
    if (Date.now() - startedAt >= SESSION_POLL_TIMEOUT_MS) {
      setSessionMessage(
        "The session is still running. Polling stopped; no replacement session was created.",
        "info",
      );
      return;
    }
    await delay(SESSION_POLL_INTERVAL_MS);
    await refreshSession();
  }
}

async function restoreSession() {
  try {
    await loadSessionHistory();
  } catch (error) {
    // History failure must not discard the active session pointer.
    showSessionError(error);
  }
  const stored = await chrome.storage.local.get(ACTIVE_SESSION_KEY);
  const sessionId = stored[ACTIVE_SESSION_KEY];
  if (typeof sessionId !== "string" || !sessionId) {
    return;
  }
  try {
    const response = await getSession(sessionId);
    renderSession(response);
    await refreshSession();
    if (currentSession?.status === "running") {
      await pollRestoredSession();
    }
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      await chrome.storage.local.remove(ACTIVE_SESSION_KEY);
      currentSession = null;
      renderSessionMessages([]);
      setSessionMessage("The saved session no longer exists on the backend.", "error");
      return;
    }
    // Preserve the ID during temporary backend or network failures.
    showSessionError(error);
  }
}

function setContextMessage(text, kind = "info") {
  elements.contextMessage.textContent = text;
  elements.contextMessage.dataset.kind = kind;
  elements.contextMessage.hidden = !text;
}

function setContextBusy(value, message = "") {
  contextBusy = value;
  elements.refreshContext.disabled = value;
  elements.refreshMemories.disabled = value;
  elements.refreshSkills.disabled = value;
  elements.createMemory.disabled = value;
  if (message) {
    setContextMessage(message, "info");
  }
}

function contextAction(parent, label, handler, className = "secondary-button") {
  const button = appendTextElement(parent, "button", label, className);
  button.type = "button";
  button.addEventListener("click", handler);
  return button;
}

function renderUsedContext(response) {
  replaceChildren(elements.usedContext);
  const snapshots = Array.isArray(response?.snapshots) ? response.snapshots : [];
  if (!snapshots.length) {
    appendTextElement(
      elements.usedContext,
      "p",
      currentSession ? "No prepared or used context snapshot is available." : "Open a Session to inspect its safe context summary.",
      "muted",
    );
    return;
  }
  for (const snapshot of snapshots) {
    const card = document.createElement("article");
    card.className = "context-item";
    appendTextElement(card, "h3", `Snapshot ${snapshot.snapshot_id}`);
    appendTextElement(
      card,
      "p",
      `${displayLabel(snapshot.status, "Unknown")} • ${Number(snapshot.estimated_input_tokens || 0).toLocaleString()} estimated tokens`,
      "requirement-meta",
    );
    const skills = Array.isArray(snapshot.skills) ? snapshot.skills : [];
    appendTextElement(
      card,
      "p",
      skills.length
        ? `Skills: ${skills.map((item) => `${item.name} ${item.version}`).join(", ")}`
        : "Skills: none",
    );
    const memories = Array.isArray(snapshot.memories) ? snapshot.memories : [];
    appendTextElement(
      card,
      "p",
      memories.length
        ? `Memory: ${memories.map((item) => `${item.memory_key} — ${item.display_text}`).join("; ")}`
        : "Memory: none",
    );
    appendTextElement(
      card,
      "p",
      `Effective tools: ${(snapshot.effective_tools || []).join(", ") || "none"}`,
      "muted",
    );
    elements.usedContext.append(card);
  }
}

async function refreshContextSummary({ quiet = false } = {}) {
  if (!currentSession?.session_id) {
    renderUsedContext({ snapshots: [] });
    return;
  }
  try {
    renderUsedContext(await getSessionContext(currentSession.session_id));
  } catch (error) {
    if (!quiet) {
      setContextMessage(error instanceof Error ? error.message : "Could not load Session context.", "error");
    }
  }
}

function memoryIdentity(memory) {
  return `${memory.scope}:${memory.session_id || ""}:${memory.memory_key}`;
}

async function mutateMemory(operation, progress) {
  if (contextBusy) {
    return;
  }
  setContextBusy(true, progress);
  try {
    await operation();
    await Promise.all([refreshMemories(), refreshContextSummary({ quiet: true })]);
    setContextMessage("Memory state updated.", "success");
  } catch (error) {
    if (error instanceof ApiError && error.status === 409) {
      await refreshMemories().catch(() => {});
      setContextMessage(
        "The Memory changed or conflicts with a confirmed value. The latest list was loaded; use Replace confirmed when appropriate.",
        "error",
      );
    } else {
      setContextMessage(error instanceof Error ? error.message : "Memory update failed.", "error");
    }
  } finally {
    setContextBusy(false);
  }
}

function renderMemories(memories) {
  replaceChildren(elements.memoryList);
  const safeMemories = Array.isArray(memories) ? memories : [];
  const confirmedByIdentity = new Map(
    safeMemories
      .filter((item) => item.status === "confirmed")
      .map((item) => [memoryIdentity(item), item]),
  );
  if (!safeMemories.length) {
    appendTextElement(elements.memoryList, "p", "No Memory items.", "muted");
    return;
  }
  for (const memory of safeMemories) {
    const card = document.createElement("article");
    card.className = "context-item";
    appendTextElement(card, "h3", memory.memory_key || "Memory");
    appendTextElement(card, "p", memory.display_text || "");
    appendTextElement(
      card,
      "p",
      [memory.status, memory.memory_type, memory.scope, memory.sensitivity]
        .map((item) => displayLabel(item, "Unknown"))
        .join(" • "),
      "requirement-meta",
    );
    const details = document.createElement("details");
    appendTextElement(details, "summary", "Structured content");
    appendTextElement(details, "pre", JSON.stringify(memory.content || {}, null, 2), "approval-arguments");
    card.append(details);
    const actions = document.createElement("div");
    actions.className = "context-actions";
    if (memory.status === "candidate") {
      const existing = confirmedByIdentity.get(memoryIdentity(memory));
      if (existing) {
        contextAction(actions, "Replace confirmed", () => mutateMemory(
          () => supersedeMemory(
            existing.memory_id,
            existing.version,
            memory.memory_id,
            memory.version,
          ),
          "Replacing the confirmed Memory...",
        ));
      } else {
        contextAction(actions, "Confirm", () => mutateMemory(
          () => confirmMemory(memory.memory_id, memory.version),
          "Confirming Memory...",
        ), "primary-button");
      }
      contextAction(actions, "Reject", () => mutateMemory(
        () => rejectMemory(memory.memory_id, memory.version),
        "Rejecting Memory...",
      ));
    }
    if (memory.status !== "deleted") {
      contextAction(actions, "Delete", () => mutateMemory(
        () => deleteMemory(memory.memory_id, memory.version),
        "Soft-deleting Memory...",
      ), "text-button danger-text");
    }
    card.append(actions);
    elements.memoryList.append(card);
  }
}

async function refreshMemories() {
  const response = await listMemories();
  renderMemories(response.memories);
  return response;
}

async function createMemoryCandidate() {
  if (contextBusy) {
    return;
  }
  const key = elements.memoryKey.value.trim();
  const displayText = elements.memoryDisplay.value.trim();
  if (!key || !displayText) {
    setContextMessage("Memory key and display text are required.", "error");
    return;
  }
  let content;
  try {
    content = JSON.parse(elements.memoryContent.value || "{}");
  } catch (_error) {
    setContextMessage("Structured content must be a valid JSON object.", "error");
    return;
  }
  if (!content || Array.isArray(content) || typeof content !== "object") {
    setContextMessage("Structured content must be a JSON object.", "error");
    return;
  }
  const scope = elements.memoryScope.value;
  if (scope === "session" && !currentSession?.session_id) {
    setContextMessage("Open a Session before creating session-scoped Memory.", "error");
    return;
  }
  setContextBusy(true, "Creating a Memory candidate...");
  try {
    await createMemory({
      scope,
      session_id: scope === "session" ? currentSession.session_id : null,
      memory_key: key,
      memory_type: elements.memoryType.value,
      display_text: displayText,
      content,
      sensitivity: elements.memorySensitivity.value,
      confidence: 1,
    });
    elements.memoryDisplay.value = "";
    elements.memoryContent.value = "{}";
    await refreshMemories();
    setContextMessage("Candidate created. Confirm it before it can enter model context.", "success");
  } catch (error) {
    setContextMessage(error instanceof Error ? error.message : "Could not create Memory.", "error");
  } finally {
    setContextBusy(false);
  }
}

async function mutateSkill(version, action, progress) {
  if (contextBusy) {
    return;
  }
  setContextBusy(true, progress);
  try {
    await action(version.version_id, version.version);
    await Promise.all([refreshSkills(), refreshContextSummary({ quiet: true })]);
    setContextMessage("Skill lifecycle updated.", "success");
  } catch (error) {
    if (error instanceof ApiError && error.status === 409) {
      await refreshSkills().catch(() => {});
      setContextMessage("The Skill changed. The latest lifecycle state was loaded.", "error");
    } else {
      setContextMessage(error instanceof Error ? error.message : "Skill update failed.", "error");
    }
  } finally {
    setContextBusy(false);
  }
}

function renderSkillVersions(versions) {
  replaceChildren(elements.skillList);
  if (!versions.length) {
    appendTextElement(elements.skillList, "p", "No registered Skills.", "muted");
    return;
  }
  for (const version of versions) {
    const card = document.createElement("article");
    card.className = "context-item";
    appendTextElement(card, "h3", `${version.name} ${version.version_label}`);
    appendTextElement(card, "p", version.description || "");
    appendTextElement(
      card,
      "p",
      `${displayLabel(version.status, "Unknown")} • Tools: ${(version.allowed_tools || []).join(", ") || "Session permissions only"}`,
      "requirement-meta",
    );
    const details = document.createElement("details");
    appendTextElement(details, "summary", "Inspect instructions");
    appendTextElement(details, "pre", version.instructions || "", "approval-arguments");
    card.append(details);
    const actions = document.createElement("div");
    actions.className = "context-actions";
    if (version.status === "approval_required") {
      contextAction(actions, "Approve", () => mutateSkill(version, approveSkill, "Approving Skill..."), "primary-button");
      contextAction(actions, "Reject", () => mutateSkill(version, rejectSkill, "Rejecting Skill..."));
    } else if (version.status === "approved") {
      contextAction(actions, "Activate", () => mutateSkill(version, activateSkill, "Activating Skill..."), "primary-button");
      contextAction(actions, "Reject", () => mutateSkill(version, rejectSkill, "Rejecting Skill..."));
    } else if (version.status === "active") {
      contextAction(actions, "Retire", () => mutateSkill(version, retireSkill, "Retiring Skill..."));
    }
    card.append(actions);
    elements.skillList.append(card);
  }
}

async function refreshSkills() {
  const response = await listSkills();
  const groups = await Promise.all(
    (response.skills || []).map((skill) => listSkillVersions(skill.name)),
  );
  renderSkillVersions(groups.flatMap((item) => item.versions || []));
  return groups;
}

async function refreshContextPanel() {
  if (contextBusy) {
    return;
  }
  setContextBusy(true, "Refreshing governed context...");
  try {
    await Promise.all([refreshContextSummary(), refreshMemories(), refreshSkills()]);
    setContextMessage("Context information refreshed.", "success");
  } catch (error) {
    setContextMessage(error instanceof Error ? error.message : "Could not refresh context.", "error");
  } finally {
    setContextBusy(false);
  }
}

function setWorkspaceMessage(text, kind = "info") {
  elements.workspaceMessage.textContent = text;
  elements.workspaceMessage.dataset.kind = kind;
  elements.workspaceMessage.hidden = !text;
}

const workspaceController = createWorkspaceController({
  elements: {
    save: elements.saveJob, open: elements.openWorkspace,
    savedId: elements.savedApplicationId, refresh: elements.refreshApplications,
    list: elements.applicationList, search: elements.applicationSearch,
    filter: elements.applicationStatusFilter, more: elements.loadMoreApplications,
    detail: elements.workspaceDetail, title: elements.workspaceTitle,
    company: elements.workspaceCompany, source: elements.workspaceSourceLink,
    id: elements.workspaceApplicationId, status: elements.workspaceStatus,
    nextAction: elements.workspaceNextAction, deadline: elements.workspaceDeadline,
    statusSelect: elements.workspaceStatusSelect, update: elements.updateWorkspace,
    transition: elements.transitionWorkspace, analyze: elements.analyzeWorkspace,
    assistant: elements.workspaceAssistant, score: elements.workspaceMatchScore,
    missing: elements.workspaceMissing, resume: elements.workspaceResume,
    runs: elements.workspaceRuns, events: elements.workspaceEvents,
    artifacts: elements.workspaceArtifacts,
  },
  getResume: () => elements.resume.value,
  getJobText: () => elements.job.value,
  getExtraction: () => ({
    ...(currentExtraction || {}),
    job_title: elements.jobTitle.value.trim() || null,
    company: elements.jobCompany.value.trim() || null,
    location: elements.jobLocation.value.trim() || null,
    extraction_source: currentExtraction?.extraction_source || "manual",
  }),
  onAnalyzeRun: async (runId) => {
    currentRunId = runId;
    renderRun(await getRun(runId));
  },
  onOpenAssistant: (applicationId) => startSession(null, applicationId),
  onWorkspaceOpen: (applicationId) => {
    interviewController?.load(applicationId);
    mockInterviewController?.load(applicationId);
    packController?.load(applicationId);
    taskActivity.load(applicationId);
    multiAgentController?.load(applicationId);
  },
  onMessage: setWorkspaceMessage,
});

const taskActivity = createTaskActivity({
  container: document.querySelector("#task-activity-list"),
  onError: (message) => setWorkspaceMessage(message, "error"),
});

interviewController = createInterviewController({
  elements: {
    start: elements.interviewStart, progress: elements.interviewProgress,
    requirement: elements.interviewRequirement, question: elements.interviewQuestion,
    answer: elements.interviewAnswer, submit: elements.interviewSubmit,
    noExperience: elements.interviewNoExperience, skip: elements.interviewSkip,
    candidate: elements.interviewCandidate, saveLater: elements.interviewSaveLater,
    recover: elements.interviewRecover, cancel: elements.interviewCancel,
    statusList: elements.interviewStatusList,
  },
  onMessage: setWorkspaceMessage,
  onChange: (status) => {
    if (status === "completed") multiAgentController?.resumeInterview();
    else multiAgentController?.refresh();
  },
});

mockInterviewController = createMockInterviewController({
  elements: {
    mode: document.querySelector("#mock-mode"),
    difficulty: document.querySelector("#mock-difficulty"),
    count: document.querySelector("#mock-count"),
    start: document.querySelector("#mock-start"),
    newAttempt: document.querySelector("#mock-new"),
    progress: document.querySelector("#mock-progress"),
    competency: document.querySelector("#mock-competency"),
    question: document.querySelector("#mock-question"),
    answer: document.querySelector("#mock-answer"),
    submit: document.querySelector("#mock-submit"),
    skip: document.querySelector("#mock-skip"),
    save: document.querySelector("#mock-save"),
    resume: document.querySelector("#mock-resume"),
    end: document.querySelector("#mock-end"),
    cancel: document.querySelector("#mock-cancel"),
    feedback: document.querySelector("#mock-feedback"),
    report: document.querySelector("#mock-report"),
  },
  onMessage: setWorkspaceMessage,
});

packController = createPackController({
  elements: {
    status: elements.packStatus, create: elements.packCreate,
    resume: elements.packResume, cover: elements.packCover,
    question: elements.packQuestion, maxLength: elements.packMaxLength,
    answer: elements.packAnswer, tabResume: elements.packTabResume,
    tabCover: elements.packTabCover, tabAnswers: elements.packTabAnswers,
    items: elements.packItems,
  },
  getApplication: () => workspaceController.state.current,
  onMessage: setWorkspaceMessage,
});

multiAgentController = createMultiAgentController({
  elements: {
    mode: document.querySelector("#workflow-mode"),
    options: document.querySelector("#multi-agent-options"),
    resume: document.querySelector("#multi-agent-resume"),
    cover: document.querySelector("#multi-agent-cover"),
    interview: document.querySelector("#multi-agent-interview"),
    question: document.querySelector("#multi-agent-question"),
    start: document.querySelector("#multi-agent-start"),
    cancel: document.querySelector("#multi-agent-cancel"),
    skipInterview: document.querySelector("#multi-agent-skip-interview"),
    status: document.querySelector("#multi-agent-status"),
    progress: document.querySelector("#multi-agent-progress"),
  },
  getApplication: () => workspaceController.state.current,
  onMessage: setWorkspaceMessage,
  onTasksChanged: (applicationId) => taskActivity.load(applicationId),
  onInterviewNeeded: (applicationId) => interviewController?.load(applicationId),
});

const evidenceController = createEvidenceController({
  elements: {
    list: elements.evidenceList, refresh: elements.evidenceRefresh,
    claim: elements.evidenceClaim, category: elements.evidenceCategory,
    create: elements.evidenceCreate, search: elements.evidenceSearch,
    filter: elements.evidenceFilter,
  },
  applicationId: () => workspaceController.state.current?.application_id || null,
  onError: (error) => setContextMessage(error?.message || "Career Evidence request failed.", "error"),
});

elements.healthButton.addEventListener("click", checkHealth);
elements.analysisTab.addEventListener("click", () => switchPanel("analysis"));
elements.jobsTab.addEventListener("click", () => {
  switchPanel("jobs");
  workspaceController.refresh();
});
elements.openWorkspace.addEventListener("click", () => switchPanel("jobs"));
elements.assistantTab.addEventListener("click", () => switchPanel("assistant"));
elements.contextTab.addEventListener("click", () => {
  switchPanel("context");
  refreshContextPanel();
  evidenceController.refresh();
});
elements.extract.addEventListener("click", extractJobDescription);
elements.analyze.addEventListener("click", analyze);
elements.approve.addEventListener("click", () => submitReview(true));
elements.reject.addEventListener("click", () => submitReview(false));
elements.copy.addEventListener("click", copyResume);
elements.clearResume.addEventListener("click", clearSavedResume);
elements.askRun.addEventListener("click", () => {
  if (currentRunId) {
    startSession(currentRunId);
  }
});
elements.newSession.addEventListener("click", () => startSession());
elements.refreshSessions.addEventListener("click", async () => {
  try {
    await loadSessionHistory();
    setSessionMessage("Conversation history refreshed.", "success");
  } catch (error) {
    showSessionError(error);
  }
});
elements.sessionHistory.addEventListener("change", () => {
  const sessionId = elements.sessionHistory.value;
  if (sessionId) {
    openSession(sessionId);
  }
});
elements.sendSession.addEventListener("click", sendMessage);
elements.cancelSession.addEventListener("click", cancelCurrentSession);
elements.recoverSession.addEventListener("click", recoverCurrentSession);
elements.refreshContext.addEventListener("click", refreshContextPanel);
elements.refreshMemories.addEventListener("click", async () => {
  try {
    await refreshMemories();
    setContextMessage("Memory list refreshed.", "success");
  } catch (error) {
    setContextMessage(error instanceof Error ? error.message : "Could not load Memory.", "error");
  }
});
elements.refreshSkills.addEventListener("click", async () => {
  try {
    await refreshSkills();
    setContextMessage("Skill list refreshed.", "success");
  } catch (error) {
    setContextMessage(error instanceof Error ? error.message : "Could not load Skills.", "error");
  }
});
elements.createMemory.addEventListener("click", createMemoryCandidate);
elements.assistantInput.addEventListener("input", updateSessionCount);
elements.resume.addEventListener("input", () => updateCount(elements.resume, elements.resumeCount));
elements.job.addEventListener("input", () => {
  updateCount(elements.job, elements.jobCount);
  workspaceController.updateSaveState();
});
elements.saveResume.addEventListener("change", async () => {
  if (elements.saveResume.checked) {
    await chrome.storage.local.set({ [SAVED_RESUME_KEY]: elements.resume.value });
    setMessage("Resume saved locally in this Chrome profile.", "success");
  } else {
    await chrome.storage.local.remove(SAVED_RESUME_KEY);
    setMessage("Local resume saving is off.", "info");
  }
});

elements.copy.disabled = true;
elements.askRun.disabled = true;
setSessionBusy(false);
setContextBusy(false);
updateCount(elements.resume, elements.resumeCount);
updateCount(elements.job, elements.jobCount);
updateSessionCount();
loadSavedResume().catch(showError);
restoreSession().catch(showSessionError);
workspaceController.restore().catch(() => {});
checkHealth();
