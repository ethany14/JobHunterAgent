"use strict";

import { analyzeApplication, health, saveWorkspace } from "./api-client.js";
import { extractJobDescriptionFromPage } from "./extractor.js";

const ACTIVE_JOB_TAB_KEY = "jobAgentActiveTab";
const ACTIVE_JOB_EXTRACTION_KEY = "jobAgentActiveExtraction";
const MAX_TEXT_LENGTH = 50_000;
const elements = {
  health: document.querySelector("#health-dot"), extract: document.querySelector("#extract-button"),
  analyze: document.querySelector("#analyze-button"), analyzeLabel: document.querySelector("#analyze-label"),
  title: document.querySelector("#job-title"), company: document.querySelector("#job-company"),
  description: document.querySelector("#job-description"), note: document.querySelector("#extract-note"),
  message: document.querySelector("#message"), result: document.querySelector("#result"),
  score: document.querySelector("#match-score"), scoreRing: document.querySelector("#score-ring"),
  resultTitle: document.querySelector("#result-title"), resultStatus: document.querySelector("#result-status"),
  matched: document.querySelector("#matched-count"), partial: document.querySelector("#partial-count"),
  missing: document.querySelector("#missing-count"), suggestions: document.querySelector("#suggestion-list"),
  details: document.querySelector("#requirement-details"), openWeb: document.querySelector("#open-web-button"),
  saveJob: document.querySelector("#save-job-button"), openApplication: document.querySelector("#open-application-button"),
};
let busy = false;
let currentExtraction = null;
let currentApplicationId = null;

function setBusy(value, label = "Analyze fit") { busy = value; elements.analyze.disabled = value; elements.extract.disabled = value; elements.saveJob.disabled = value; elements.analyzeLabel.textContent = label; document.body.classList.toggle("is-busy", value); }
function showMessage(text, kind = "error") { elements.message.textContent = text; elements.message.dataset.kind = kind; elements.message.hidden = !text; }
function validHttpUrl(url) { return typeof url === "string" && /^https?:\/\//.test(url); }

async function getTargetJobTab() {
  let target = null;
  for (let attempt = 0; attempt < 6 && !target?.tabId; attempt += 1) {
    const stored = await chrome.storage.session.get(ACTIVE_JOB_TAB_KEY); target = stored[ACTIVE_JOB_TAB_KEY];
    if (!target?.tabId) await new Promise((resolve) => setTimeout(resolve, 80));
  }
  if (!target?.tabId) throw new Error("Open a job page and click the Job Lens toolbar icon first.");
  if (!target.validPage || !validHttpUrl(target.url)) throw new Error("Chrome cannot extract from this protected page. Open a regular HTTP or HTTPS job page.");
  return target;
}

function extractionScore(value) {
  if (!value?.cleaned_job_description) return -1;
  return Math.min(value.cleaned_job_description.length, 20_000)
    + (value.job_title ? 20_000 : 0)
    + (value.company ? 20_000 : 0);
}

function applyExtraction(extracted) {
  if (!extracted?.cleaned_job_description) return false;
  if (extracted.cleaned_job_description.length > MAX_TEXT_LENGTH) {
    throw new Error("The extracted page is over 50,000 characters. Select only the job description and extract again.");
  }
  elements.description.value = extracted.cleaned_job_description;
  currentExtraction = extracted;
  elements.title.value = extracted.job_title || "";
  elements.company.value = extracted.company || "";
  elements.note.textContent = `Extracted from ${extracted.extraction_source}. Review or edit it before analysis.`;
  showMessage("Job details detected automatically. Nothing has been sent yet.", "success");
  return true;
}

async function cachedExtraction(target) {
  const stored = await chrome.storage.session.get(ACTIVE_JOB_EXTRACTION_KEY);
  const cached = stored[ACTIVE_JOB_EXTRACTION_KEY];
  if (cached?.tabId !== target.tabId || !cached.result) return null;
  await chrome.storage.session.remove(ACTIVE_JOB_EXTRACTION_KEY);
  return cached.result;
}

async function extract() {
  if (busy) return; setBusy(true, "Reading page…"); showMessage("");
  try {
    const target = await getTargetJobTab();
    let extracted = await cachedExtraction(target);
    // LinkedIn updates the detail pane asynchronously. Prefer the toolbar-click
    // result, then retry briefly and keep the most complete visible result.
    for (let attempt = 0; attempt < 4; attempt += 1) {
      if (attempt > 0 || !extracted) {
        if (attempt > 0) await new Promise((resolve) => setTimeout(resolve, 350));
        const results = await chrome.scripting.executeScript({ target: { tabId: target.tabId }, func: extractJobDescriptionFromPage });
        const candidate = results?.[0]?.result;
        if (extractionScore(candidate) > extractionScore(extracted)) extracted = candidate;
      }
      if (extracted?.job_title && extracted?.company && extracted.cleaned_job_description?.length >= 200) break;
    }
    if (!extracted?.cleaned_job_description) throw new Error("No job description was found. Select the job text and try again, or paste it manually.");
    applyExtraction(extracted);
  } catch (error) { showMessage(error?.message || "The page could not be extracted."); }
  finally { setBusy(false); }
}

function requirementName(item) { return item?.display_name || item?.canonical_name || item?.job_skill || item?.original_text || "Requirement"; }
function addRequirementGroup(title, items, className) {
  if (!items?.length) return; const section = document.createElement("section"); section.className = "requirement-group"; const heading = document.createElement("h3"); heading.textContent = title; section.append(heading);
  for (const item of items) { const row = document.createElement("div"); row.className = `requirement-row ${className}`; const mark = document.createElement("span"); mark.textContent = className === "matched" ? "✓" : className === "partial" ? "~" : "—"; const text = document.createElement("span"); text.textContent = requirementName(item); row.append(mark, text); section.append(row); }
  elements.details.append(section);
}
function render(result) {
  const score = Math.round(Number(result.match_score) || 0); elements.score.textContent = `${score}%`; elements.scoreRing.style.setProperty("--score", `${score * 3.6}deg`);
  elements.resultTitle.textContent = score >= 80 ? "Strong alignment" : score >= 60 ? "Promising fit" : "A stretch role";
  elements.resultStatus.textContent = "Compared with your default resume";
  elements.matched.textContent = String(result.matched_requirements.length); elements.partial.textContent = String(result.partial_requirements.length); elements.missing.textContent = String(result.missing_requirements.length);
  elements.suggestions.replaceChildren(); for (const suggestion of result.suggestions) { const item = document.createElement("li"); item.textContent = suggestion; elements.suggestions.append(item); }
  elements.details.replaceChildren(); addRequirementGroup("Matched", result.matched_requirements, "matched"); addRequirementGroup("Partial", result.partial_requirements, "partial"); addRequirementGroup("Missing evidence", result.missing_requirements, "missing"); addRequirementGroup("Confirm before applying", result.confirmation_requirements, "confirm");
  elements.result.hidden = false; elements.result.scrollIntoView({ behavior: "smooth", block: "start" });
}
function workspacePayload() {
  return {
    source_url: currentExtraction?.source_url || null,
    source_site: currentExtraction?.source_site || null,
    company: elements.company.value.trim() || null,
    title: elements.title.value.trim() || null,
    location: currentExtraction?.location || null,
    raw_page_text: null,
    cleaned_job_description: elements.description.value.trim(),
    extraction_metadata: {
      source: currentExtraction?.extraction_source || "manual_extension",
      confidence: currentExtraction?.extraction_confidence ?? null,
      page_title: currentExtraction?.page_title || null,
    },
  };
}
async function saveCurrentJob() {
  const jd = elements.description.value.trim();
  if (!jd) throw new Error("Extract or paste a job description first.");
  const saved = await saveWorkspace(workspacePayload());
  currentApplicationId = saved.application.application_id;
  elements.openApplication.hidden = false;
  elements.saveJob.textContent = "Saved to workspace";
  return saved;
}
function quickResultFromAnalysis(analysis) {
  const report = (analysis.artifacts || []).find((item) => item.artifact_type === "match_report")?.content || {};
  const matches = report.matches || [];
  const matched = matches.filter((item) => item.match_status === "matched");
  const partial = matches.filter((item) => item.match_status === "partial");
  const missing = [...(report.missing_required_requirements || []), ...(report.missing_preferred_requirements || [])];
  const confirmations = report.confirmation_requirements || [];
  const suggestions = missing.slice(0, 3).map((item) => `Add concrete resume evidence for ${requirementName(item)} if you have it; otherwise keep it as a gap.`);
  suggestions.push(...partial.slice(0, 2).map((item) => `Strengthen ${requirementName(item)} with a specific example or outcome.`));
  if (!suggestions.length) suggestions.push("Keep the strongest matched requirements prominent and preserve factual evidence links.");
  return { match_score: report.overall_score || 0, matched_requirements: matched,
    partial_requirements: partial, missing_requirements: missing,
    confirmation_requirements: confirmations, suggestions };
}
async function analyze() {
  const jd = elements.description.value.trim(); if (!jd) return showMessage("Extract or paste a job description first."); if (jd.length > MAX_TEXT_LENGTH) return showMessage("The job description must be 50,000 characters or fewer.");
  setBusy(true, "Analyzing…"); showMessage(""); elements.result.hidden = true;
  try {
    const saved = await saveCurrentJob();
    const analysis = await analyzeApplication(saved.application.application_id, saved.application.version);
    render(quickResultFromAnalysis(analysis));
    showMessage("Analysis complete and saved. Open this job in the full workspace for materials and Copilot.", "success");
  }
  catch (error) { showMessage(error.message); }
  finally { setBusy(false); }
}
async function checkHealth() { try { await health(); elements.health.className = "online"; elements.health.title = "Backend online"; } catch (_error) { elements.health.className = "offline"; elements.health.title = "Backend unavailable"; } }

elements.extract.addEventListener("click", extract); elements.analyze.addEventListener("click", analyze);
elements.openWeb.addEventListener("click", () => chrome.tabs.create({ url: "http://localhost:8000/app" }));
elements.openApplication.addEventListener("click", () => {
  if (currentApplicationId) chrome.tabs.create({ url: `http://localhost:8000/app?application=${encodeURIComponent(currentApplicationId)}#jobs` });
});
elements.saveJob.addEventListener("click", async () => {
  if (busy) return; setBusy(true, "Saving…"); showMessage("");
  try { await saveCurrentJob(); showMessage("Job saved. You can open it in the full workspace.", "success"); }
  catch (error) { showMessage(error.message); }
  finally { setBusy(false); }
});
elements.description.addEventListener("input", () => { elements.note.textContent = `${elements.description.value.length.toLocaleString()} / 50,000 characters`; });
chrome.storage.onChanged.addListener((changes, areaName) => {
  if (areaName === "session" && changes[ACTIVE_JOB_TAB_KEY]?.newValue) extract();
  if (areaName === "session" && changes[ACTIVE_JOB_EXTRACTION_KEY]?.newValue && !busy) {
    const cached = changes[ACTIVE_JOB_EXTRACTION_KEY].newValue;
    chrome.storage.session.get(ACTIVE_JOB_TAB_KEY).then((stored) => {
      if (stored[ACTIVE_JOB_TAB_KEY]?.tabId !== cached.tabId) return;
      try { applyExtraction(cached.result); } catch (error) { showMessage(error.message); }
    });
  }
});
await checkHealth();
await extract();
