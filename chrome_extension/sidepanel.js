"use strict";

import { ApiError, createRun, getRun, health, reviewRun } from "./api-client.js";
import { extractJobDescriptionFromPage } from "./extractor.js";

const MAX_TEXT_LENGTH = 50_000;
const POLL_INTERVAL_MS = 1_500;
const POLL_TIMEOUT_MS = 60_000;
const SAVED_RESUME_KEY = "jobAgentSavedResume";
const ACTIVE_JOB_TAB_KEY = "jobAgentActiveTab";

const elements = {
  healthButton: document.querySelector("#health-button"),
  healthText: document.querySelector("#health-text"),
  resume: document.querySelector("#resume-text"),
  saveResume: document.querySelector("#save-resume"),
  clearResume: document.querySelector("#clear-resume-button"),
  resumeCount: document.querySelector("#resume-count"),
  job: document.querySelector("#job-description"),
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
};

let currentRunId = null;
let currentResumeText = "";
let busy = false;

function setMessage(text, kind = "info") {
  elements.message.textContent = text;
  elements.message.dataset.kind = kind;
  elements.message.hidden = !text;
}

function setBusy(value, message = "") {
  busy = value;
  elements.analyze.disabled = value;
  elements.extract.disabled = value;
  elements.approve.disabled = value;
  elements.reject.disabled = value;
  if (message) {
    setMessage(message, "info");
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
  if (busy) {
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
    if (!extracted?.text) {
      throw new Error("No job description text was found. Select the job text or paste it manually.");
    }
    elements.job.value = extracted.text;
    elements.extractionSource.textContent = `Extracted from ${extracted.source}`;
    updateCount(elements.job, elements.jobCount);
    if (extracted.text.length > MAX_TEXT_LENGTH) {
      throw new Error("The extracted page exceeds 50,000 characters. Select only the job description and extract again, or edit the text below.");
    }
    setMessage("Job description extracted. Review or edit it before analysis.", "success");
  } catch (error) {
    showError(pageAccessError(error, tab));
  } finally {
    setBusy(false);
  }
}

async function analyze() {
  if (busy) {
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
  if (busy || !currentRunId) {
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

elements.healthButton.addEventListener("click", checkHealth);
elements.extract.addEventListener("click", extractJobDescription);
elements.analyze.addEventListener("click", analyze);
elements.approve.addEventListener("click", () => submitReview(true));
elements.reject.addEventListener("click", () => submitReview(false));
elements.copy.addEventListener("click", copyResume);
elements.clearResume.addEventListener("click", clearSavedResume);
elements.resume.addEventListener("input", () => updateCount(elements.resume, elements.resumeCount));
elements.job.addEventListener("input", () => updateCount(elements.job, elements.jobCount));
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
updateCount(elements.resume, elements.resumeCount);
updateCount(elements.job, elements.jobCount);
loadSavedResume().catch(showError);
checkHealth();
