"use strict";

import { api } from "./api.js";

const linkedApplication = new URLSearchParams(location.search).get("application");
const state = {
  applications: [],
  selectedApplication: linkedApplication || localStorage.getItem("northstar.application"),
  applicationDetail: null,
  session: localStorage.getItem("northstar.session"),
  sessionVersion: null,
  sessionBusy: false,
};
if (linkedApplication) localStorage.setItem("northstar.application", linkedApplication);

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const node = (tag, className = "", text = null) => {
  const value = document.createElement(tag);
  if (className) value.className = className;
  if (text !== null) value.textContent = text;
  return value;
};
const clear = (value) => { value.replaceChildren(); value.classList.remove("empty-state"); };
const label = (value, fallback = "Untitled role") => typeof value === "string" && value.trim() ? value.trim() : fallback;
const score = (value) => Number.isFinite(Number(value)) ? `${Math.round(Number(value))}%` : "Not analyzed";
const requestKey = () => crypto.randomUUID();

function conversationMessage(role, content, { messageId = null, pending = false } = {}) {
  const box = node("div", `${role}-message${pending ? " pending-message" : ""}`);
  if (messageId) box.dataset.messageId = messageId;
  if (role === "assistant") box.append(node("span", "message-mark", "A"));
  box.append(node("p", pending ? "thinking-label" : "", content));
  return box;
}

function appendConversationMessage(role, content, options = {}) {
  const area = $("#conversation"), box = conversationMessage(role, content, options);
  area.append(box); area.scrollTop = area.scrollHeight;
  return box;
}

function setSessionBusy(value) {
  state.sessionBusy = value;
  const composer = $("#composer"), send = composer.querySelector(".send-button");
  composer.setAttribute("aria-busy", String(value));
  send.disabled = value;
}

function toast(message, kind = "info") {
  const value = $("#toast");
  value.textContent = message; value.dataset.kind = kind; value.hidden = false;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => { value.hidden = true; }, 4200);
}

const titles = {
  overview: "Good morning", jobs: "Your opportunities", copilot: "Think it through",
  materials: "Build with evidence", evidence: "Your proof, organized",
  learning: "What your copilot has learned", settings: "Workspace settings",
};

function showView(name) {
  if (!titles[name]) name = "overview";
  $$(".view").forEach((item) => item.classList.toggle("is-active", item.dataset.viewPanel === name));
  $$(".nav-item").forEach((item) => item.classList.toggle("is-active", item.dataset.view === name));
  $("#view-title").textContent = titles[name];
  history.replaceState(null, "", `${location.pathname}${location.search}#${name}`);
  if (name === "evidence") loadEvidence();
  if (name === "learning") loadLearning();
  if (name === "settings") loadResumes();
  if (name === "materials") loadMaterials();
  if (name === "copilot") loadSessionHistory();
}

async function health() {
  try {
    await api.health(); $("#health-dot").className = "online"; $("#health-label").textContent = "Local service online";
  } catch (error) {
    $("#health-dot").className = "offline"; $("#health-label").textContent = "Service unavailable"; toast(error.message, "error");
  }
}

function applicationCard(item, compact = false) {
  const card = node("button", `job-card${state.selectedApplication === item.application_id ? " selected" : ""}`); card.type = "button";
  const top = node("div", "job-card-top");
  top.append(node("span", "company-mark", label(item.company, "?").slice(0, 1).toUpperCase()), node("span", "status-pill", String(item.status).replaceAll("_", " ")));
  card.append(top, node("h3", "", label(item.title)), node("p", "", [item.company, item.location].filter(Boolean).join(" / ") || "Company details not set"));
  if (!compact) card.append(node("strong", "job-score", score(item.match_score)));
  card.addEventListener("click", () => selectApplication(item.application_id)); return card;
}

async function loadApplications(search = "") {
  try {
    const data = await api.listApplications(search); state.applications = data.applications || [];
    const list = $("#job-list"), overview = $("#overview-jobs"); clear(list); clear(overview);
    if (!state.applications.length) {
      list.textContent = "No saved jobs yet."; overview.textContent = "No saved jobs yet.";
      list.classList.add("empty-state"); overview.classList.add("empty-state");
    }
    state.applications.forEach((item) => list.append(applicationCard(item)));
    state.applications.slice(0, 4).forEach((item) => overview.append(applicationCard(item, true)));
    $("#metric-jobs").textContent = String(state.applications.length);
    const scores = state.applications.map((item) => Number(item.match_score)).filter(Number.isFinite);
    $("#metric-score").textContent = scores.length ? `${Math.round(scores.reduce((a, b) => a + b, 0) / scores.length)}%` : "--";
    if (state.selectedApplication) await renderApplication(state.selectedApplication);
  } catch (error) { toast(error.message, "error"); }
}

async function selectApplication(id) {
  state.selectedApplication = id; localStorage.setItem("northstar.application", id);
  await renderApplication(id); showView("jobs"); await loadApplications($("#job-search").value.trim());
}

async function renderApplication(id) {
  try {
    const item = await api.getApplication(id); state.applicationDetail = item;
    const panel = $("#job-detail"); clear(panel);
    panel.append(node("p", "eyebrow", "CURRENT ROLE"), node("h2", "", label(item.job.title)), node("p", "detail-company", [item.job.company, item.job.location].filter(Boolean).join(" / ") || "Company details not set"));
    if (item.job.canonical_url) {
      const link = node("a", "text-button", "Open original job ->");
      link.href = item.job.canonical_url; link.target = "_blank"; link.rel = "noopener noreferrer"; panel.append(link);
    }
    const metric = node("div", "score-block"); metric.append(node("strong", "", score(item.latest_match_score)), node("span", "", "Match score"));
    panel.append(metric, node("p", "description-preview", item.snapshot.cleaned_job_description));
    const actions = node("div", "detail-actions");
    const analyze = node("button", "primary-button", "Analyze fit");
    analyze.addEventListener("click", async () => {
      analyze.disabled = true; analyze.textContent = "Analyzing...";
      try {
        const outcome = await api.analyzeApplication(item.application_id, { expected_version: item.version, resume_id: null });
        toast(`Analysis ${outcome.run_status.replaceAll("_", " ")}.`, "success"); await loadApplications();
      } catch (error) { toast(error.message, "error"); }
      finally { analyze.disabled = false; analyze.textContent = "Analyze fit"; }
    });
    const ask = node("button", "primary-button", "Ask Copilot"); ask.addEventListener("click", () => openCopilotForApplication(item));
    const materials = node("button", "soft-button", "View materials"); materials.addEventListener("click", () => showView("materials"));
    actions.append(analyze, ask, materials); panel.append(actions);
  } catch (error) {
    if (error.status === 404) { state.selectedApplication = null; localStorage.removeItem("northstar.application"); }
    toast(error.message, "error");
  }
}

async function saveJob(event) {
  event.preventDefault(); const form = new FormData(event.currentTarget);
  const jd = String(form.get("job_description") || "").trim(); if (!jd) return;
  const sourceUrl = String(form.get("source_url") || "").trim() || null;
  let sourceSite = "web";
  if (sourceUrl) {
    try {
      const parsed = new URL(sourceUrl);
      if (!["http:", "https:"].includes(parsed.protocol)) throw new Error("unsupported protocol");
      sourceSite = parsed.hostname;
    } catch { return toast("Enter a valid HTTP or HTTPS job URL.", "error"); }
  }
  try {
    const result = await api.saveJob({ source_url: sourceUrl, source_site: sourceSite,
      title: String(form.get("title") || "").trim() || null,
      company: String(form.get("company") || "").trim() || null,
      location: String(form.get("location") || "").trim() || null,
      raw_page_text: null, cleaned_job_description: jd,
      extraction_metadata: { source: "manual_web" }, reopen_existing: true });
    $("#job-dialog").close(); event.currentTarget.reset();
    state.selectedApplication = result.application.application_id;
    localStorage.setItem("northstar.application", state.selectedApplication);
    await loadApplications(); toast("Job workspace saved.", "success");
  } catch (error) { toast(error.message, "error"); }
}

async function loadSessionHistory() {
  try {
    const result = await api.listSessions(), list = $("#session-history"); clear(list);
    for (const item of result.sessions || []) {
      const button = node("button", `session-row${state.session === item.session_id ? " is-active" : ""}`); button.type = "button";
      button.append(node("strong", "", label(item.title, "Conversation")), node("small", "", `${String(item.status).replaceAll("_", " ")} / ${new Date(item.updated_at).toLocaleDateString()}`));
      button.addEventListener("click", async () => {
        state.session = item.session_id; localStorage.setItem("northstar.session", state.session);
        await renderSession(); await loadSessionHistory();
      });
      list.append(button);
    }
    if (!list.childElementCount) list.append(node("p", "session-history-empty", "No previous conversations."));
  } catch (error) { toast(error.message, "error"); }
}

async function openCopilotForApplication(application) {
  try {
    const created = await api.createSession({ title: `Discuss ${label(application.job?.title)}`, application_id: application.application_id });
    state.session = created.session.session_id; localStorage.setItem("northstar.session", state.session);
    showView("copilot"); await renderSession(created); await loadSessionHistory();
  } catch (error) { toast(error.message, "error"); }
}

async function newSession() {
  try {
    const created = await api.createSession({ title: "Career conversation", ...(state.selectedApplication ? { application_id: state.selectedApplication } : {}) });
    state.session = created.session.session_id; localStorage.setItem("northstar.session", state.session);
    await renderSession(created); await loadSessionHistory();
  } catch (error) { toast(error.message, "error"); }
}

async function renderSession(response = null) {
  if (!state.session) return;
  try {
    const current = response || await api.getSession(state.session), data = await api.messages(state.session), area = $("#conversation"); clear(area);
    for (const message of data.messages || []) {
      area.append(conversationMessage(message.role, message.content, { messageId: message.message_id }));
    }
    area.scrollTop = area.scrollHeight; state.sessionVersion = current.session.version;
    $("#conversation-title").textContent = label(current.session.title, "Career Copilot");
    const context = $("#copilot-context"); clear(context);
    context.append(node("strong", "", String(current.session.status).replaceAll("_", " ")), node("p", "", `${current.session.total_input_tokens + current.session.total_output_tokens} tokens / ${current.session.executed_tool_calls} tool calls`));
  } catch (error) {
    if (error.status === 404) { state.session = null; state.sessionVersion = null; localStorage.removeItem("northstar.session"); await loadSessionHistory(); }
    toast(error.message, "error");
  }
}

async function sendMessage(event) {
  event.preventDefault(); const input = $("#message-input"), content = input.value.trim(); if (!content) return;
  if (state.sessionBusy) return;
  if (!state.session) await newSession();
  if (!state.session) return;
  const sessionId = state.session, expectedVersion = state.sessionVersion, messageId = crypto.randomUUID();
  input.value = "";
  const optimisticUser = appendConversationMessage("user", content, { messageId, pending: true });
  const thinking = appendConversationMessage("assistant", "Thinking…", { pending: true });
  setSessionBusy(true);
  try {
    const result = await api.sendMessage(sessionId, { message_id: messageId, content, expected_version: expectedVersion });
    if (state.session === sessionId) await renderSession(result);
    await loadSessionHistory();
  } catch (error) {
    thinking.remove(); optimisticUser.classList.remove("pending-message"); optimisticUser.classList.add("send-failed");
    optimisticUser.append(node("small", "message-delivery", error.status === 409 ? "Not sent — the conversation changed." : "Delivery failed — edit and try again."));
    if (!input.value) input.value = content;
    toast(error.status === 409 ? "The conversation changed. Your message remains here and was not resent." : error.message, "error");
    try {
      const current = await api.getSession(sessionId);
      if (state.session === sessionId) state.sessionVersion = current.session.version;
    } catch (_refreshError) { /* Preserve the visible unsent message. */ }
  } finally { setSessionBusy(false); }
}

async function deleteCurrentSession() {
  if (!state.session) return toast("Select a conversation first.", "error");
  if (!confirm("Remove this conversation from your history?")) return;
  try {
    await api.deleteSession(state.session, state.sessionVersion);
    state.session = null; state.sessionVersion = null; localStorage.removeItem("northstar.session");
    const area = $("#conversation"); clear(area); area.append(node("div", "assistant-message", "Start a new conversation when you are ready."));
    await loadSessionHistory(); toast("Conversation removed.", "success");
  } catch (error) { if (error.status === 409) await renderSession(); toast(error.message, "error"); }
}

async function loadEvidence() {
  try {
    const data = await api.listEvidence(), list = $("#evidence-list"); clear(list);
    $("#metric-evidence").textContent = String((data.items || []).filter((item) => item.status === "confirmed").length);
    for (const item of data.items || []) {
      const card = node("article", "data-card");
      card.append(node("span", "status-pill", item.status), node("h3", "", item.current?.claim_text || "Evidence"), node("p", "", item.current?.category || item.category || "Career evidence"));
      if (item.status === "candidate") {
        const actions = node("div", "inline-actions");
        actions.append(action("Confirm", async () => { await api.confirmEvidence(item.evidence_id, item.version); await loadEvidence(); }), action("Reject", async () => { await api.rejectEvidence(item.evidence_id, item.version); await loadEvidence(); }, true));
        card.append(actions);
      }
      list.append(card);
    }
    if (!data.items?.length) list.textContent = "No evidence yet.";
  } catch (error) { toast(error.message, "error"); }
}

function action(text, handler, danger = false) {
  const button = node("button", `text-button${danger ? " danger" : ""}`, text); button.type = "button";
  button.addEventListener("click", async () => { try { await handler(); } catch (error) { toast(error.message, "error"); } }); return button;
}

async function renderSkillVersions(container, name) {
  try {
    const data = await api.skillVersions(name); clear(container);
    for (const item of data.versions || []) {
      const row = node("div", "learning-row"); row.append(node("strong", "", `${item.name} ${item.version_label}`), node("small", "", item.status));
      const details = node("details"); details.append(node("summary", "", "Inspect instructions"), node("pre", "artifact-content", item.instructions)); row.append(details);
      const actions = node("div", "inline-actions"), invoke = async (method) => { await method(item.version_id, item.version); await loadLearning(); };
      if (item.status === "approval_required") actions.append(action("Approve", () => invoke(api.approveSkill)), action("Reject", () => invoke(api.rejectSkill), true));
      if (item.status === "approved") actions.append(action("Activate", () => invoke(api.activateSkill)), action("Reject", () => invoke(api.rejectSkill), true));
      if (item.status === "active") actions.append(action("Remove", () => invoke(api.retireSkill), true));
      if (actions.childElementCount) row.append(actions); container.append(row);
    }
  } catch (error) { toast(error.message, "error"); }
}

async function loadLearning() {
  try {
    const [memories, skills] = await Promise.all([api.listMemories(), api.listSkills()]);
    const memory = $("#memory-list"), skill = $("#skill-list"); clear(memory); clear(skill);
    for (const item of memories.memories || []) {
      const row = node("div", "learning-row"); row.append(node("strong", "", item.display_text || item.memory_key), node("small", "", `${item.memory_type} / ${item.status}`));
      const actions = node("div", "inline-actions");
      if (item.status === "candidate") actions.append(action("Confirm", async () => { await api.confirmMemory(item.memory_id, item.version); await loadLearning(); }), action("Reject", async () => { await api.rejectMemory(item.memory_id, item.version); await loadLearning(); }, true));
      if (!["deleted", "superseded", "expired"].includes(item.status)) actions.append(action("Delete", async () => { await api.deleteMemory(item.memory_id, item.version); await loadLearning(); }, true));
      if (actions.childElementCount) row.append(actions); memory.append(row);
    }
    for (const item of skills.skills || []) {
      const wrapper = node("div", "skill-entry"), button = node("button", "job-card"), versions = node("div", "skill-versions");
      button.type = "button"; button.append(node("h3", "", item.name), node("p", "", `${item.latest_version} / ${item.latest_status}`)); versions.hidden = true;
      button.addEventListener("click", async () => { versions.hidden = !versions.hidden; if (!versions.hidden && !versions.childElementCount) await renderSkillVersions(versions, item.name); });
      wrapper.append(button, versions); skill.append(wrapper);
    }
    if (!memory.childElementCount) memory.textContent = "No conversation-learned memory candidates yet.";
    if (!skill.childElementCount) skill.textContent = "No registered skills.";
  } catch (error) { toast(error.message, "error"); }
}

async function loadResumes() {
  try {
    const data = await api.listResumes(), list = $("#resume-list"); clear(list);
    for (const item of data.resumes) {
      const row = node("div", "resume-row"), copy = node("div"), actions = node("div", "inline-actions");
      copy.append(node("strong", "", item.display_name), node("small", "", `${item.page_count} page${item.page_count === 1 ? "" : "s"}${item.is_default ? " / Default" : ""}`));
      if (!item.is_default) actions.append(action("Use", async () => { await api.setDefaultResume(item.resume_id); await loadResumes(); }));
      actions.append(action("Delete", async () => { await api.deleteResume(item.resume_id); await loadResumes(); }, true)); row.append(copy, actions); list.append(row);
    }
    if (!data.resumes.length) list.textContent = "No resume uploaded.";
  } catch (error) { toast(error.message, "error"); }
}

async function uploadResume(event) {
  const file = event.target.files?.[0]; if (!file) return;
  if (file.type !== "application/pdf" && !file.name.toLowerCase().endsWith(".pdf")) return toast("Choose a PDF file.", "error");
  try { toast("Reading resume..."); await api.uploadResume(file); await loadResumes(); toast("Resume uploaded and set as default.", "success"); }
  catch (error) { toast(error.message, "error"); } finally { event.target.value = ""; }
}

function readableArtifact(value, key = "") {
  const hidden = /(^|_)(id|ids|hash|version|requirement|evidence|artifact|snapshot|schema)(_|$)/i;
  if (value == null || hidden.test(key)) return [];
  if (typeof value === "string") return value.trim() ? [value.trim()] : [];
  if (Array.isArray(value)) return value.flatMap((item) => readableArtifact(item, key));
  if (typeof value === "object") return Object.entries(value).flatMap(([childKey, child]) => readableArtifact(child, childKey));
  return [];
}

function artifactText(content) {
  const unique = []; for (const text of readableArtifact(content)) if (!unique.includes(text)) unique.push(text);
  return unique.join("\n\n") || "This artifact has no displayable text yet.";
}

function openMaterial(title, content, actions = []) {
  const text = artifactText(content);
  $("#material-title").textContent = title; $("#material-content").textContent = text;
  const copy = action("Copy", async () => { await navigator.clipboard.writeText(text); toast("Material copied.", "success"); });
  const download = action("Download .txt", async () => {
    const url = URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
    const link = document.createElement("a");
    link.href = url; link.download = `${title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "application-material"}.txt`;
    link.click(); URL.revokeObjectURL(url);
  });
  const area = $("#material-actions"); clear(area); area.append(copy, download); actions.forEach((item) => area.append(item)); $("#material-dialog").showModal();
}

function materialCard(title, subtitle, status, onOpen) {
  const card = node("button", "artifact-card"); card.type = "button";
  card.append(node("span", "status-pill", String(status || "available").replaceAll("_", " ")), node("h3", "", title), node("p", "", subtitle));
  card.addEventListener("click", onOpen); return card;
}

function pdfDownload(path) {
  const button = node("button", "primary-button", "Download PDF"); button.type = "button";
  button.addEventListener("click", () => {
    const link = document.createElement("a"); link.href = path; link.download = "tailored-resume.pdf"; link.click();
  });
  return button;
}

function renderPackDetail(detail) {
  const area = $("#material-list"), pack = detail.pack; clear(area);
  area.append(node("p", "eyebrow", `APPLICATION PACK ${pack.generation_number} / ${String(pack.status).replaceAll("_", " ")}`));
  for (const item of detail.items || []) {
    const title = item.artifact_type.replaceAll("_", " ");
    area.append(materialCard(title, `Version ${item.version} / ${item.verification?.passed ? "Verified" : "Review available"}`, item.status, () => {
      const actions = [];
      if (!["approved", "rejected", "failed"].includes(item.status)) {
        actions.push(action("Approve", async () => { const next = await api.approvePackItem(pack.pack_id, item.pack_item_id, { expected_version: item.version, idempotency_key: requestKey(), feedback: null }); $("#material-dialog").close(); renderPackDetail(next); }));
        actions.push(action("Reject", async () => { const next = await api.rejectPackItem(pack.pack_id, item.pack_item_id, { expected_version: item.version, idempotency_key: requestKey(), feedback: null }); $("#material-dialog").close(); renderPackDetail(next); }, true));
      }
      if (item.artifact_type === "tailored_resume" && item.content) {
        actions.unshift(pdfDownload(`/api/packs/${encodeURIComponent(pack.pack_id)}/items/${encodeURIComponent(item.pack_item_id)}/resume.pdf`));
      }
      actions.push(action("Regenerate", async () => {
        const feedback = prompt("What should change?", ""); if (feedback === null) return;
        const next = await api.regeneratePackItem(pack.pack_id, item.pack_item_id, { expected_version: item.version, idempotency_key: requestKey(), feedback: feedback.trim() || null });
        $("#material-dialog").close(); renderPackDetail(next);
      }));
      openMaterial(title, item.content, actions);
    }));
  }
  if (!(detail.items || []).some((item) => item.artifact_type === "tailored_resume")) {
    const button = node("button", "primary-button", "Generate tailored resume");
    button.addEventListener("click", async () => { try { renderPackDetail(await api.generatePackResume(pack.pack_id, { expected_version: pack.version, idempotency_key: requestKey(), feedback: null, max_length: null })); } catch (error) { toast(error.message, "error"); } }); area.append(button);
  }
  if (!(detail.items || []).some((item) => item.artifact_type === "cover_letter")) {
    const button = node("button", "primary-button", "Generate cover letter");
    button.addEventListener("click", async () => { try { renderPackDetail(await api.generatePackCoverLetter(pack.pack_id, { expected_version: pack.version, idempotency_key: requestKey(), feedback: null, max_length: null })); } catch (error) { toast(error.message, "error"); } }); area.append(button);
  }
}

async function loadMaterials() {
  const area = $("#material-list");
  if (!state.selectedApplication) { clear(area); area.textContent = "Select a saved job to view its materials."; return; }
  try {
    const [data, packs] = await Promise.all([api.artifacts(state.selectedApplication), api.packs(state.selectedApplication)]); clear(area);
    for (const item of data.artifacts || []) area.append(materialCard(item.artifact_type.replaceAll("_", " "), `Version ${item.version} / ${item.verification_status || "not verified"}`, item.status, () => {
      const actions = item.artifact_type === "tailored_resume"
        ? [pdfDownload(`/api/applications/${encodeURIComponent(state.selectedApplication)}/artifacts/${encodeURIComponent(item.artifact_id)}/resume.pdf`)]
        : [];
      openMaterial(item.artifact_type.replaceAll("_", " "), item.content, actions);
    }));
    for (const pack of packs.packs || []) area.append(materialCard(`Application pack ${pack.generation_number}`, `Updated ${new Date(pack.updated_at).toLocaleString()}`, pack.status, async () => { try { renderPackDetail(await api.getPack(pack.pack_id)); } catch (error) { toast(error.message, "error"); } }));
    if (!area.childElementCount) area.textContent = "No materials generated for this role.";
  } catch (error) { toast(error.message, "error"); }
}

$$('[data-view]').forEach((button) => button.addEventListener("click", () => showView(button.dataset.view)));
$$('[data-view-target]').forEach((button) => button.addEventListener("click", () => showView(button.dataset.viewTarget)));
$$('[data-close-dialog]').forEach((button) => button.addEventListener("click", () => document.getElementById(button.dataset.closeDialog)?.close()));
$("#refresh-button").addEventListener("click", () => loadApplications($("#job-search").value.trim()));
$("#reload-jobs").addEventListener("click", () => loadApplications($("#job-search").value.trim()));
$("#job-search").addEventListener("input", (event) => loadApplications(event.target.value.trim()));
$("#new-job-button").addEventListener("click", () => $("#job-dialog").showModal());
$("#job-form").addEventListener("submit", saveJob);
$("#new-session").addEventListener("click", newSession);
$("#delete-session").addEventListener("click", deleteCurrentSession);
$("#composer").addEventListener("submit", sendMessage);
$("#resume-file").addEventListener("change", uploadResume);
$("#check-health").addEventListener("click", health);
$("#generate-pack").addEventListener("click", async () => {
  if (!state.selectedApplication) return toast("Select a job first.", "error");
  try {
    const detail = state.applicationDetail?.application_id === state.selectedApplication ? state.applicationDetail : await api.getApplication(state.selectedApplication);
    const created = await api.createPack(state.selectedApplication, { expected_version: detail.version, idempotency_key: requestKey(), feedback: null });
    toast(`Application Pack ${created.pack.generation_number} created.`, "success"); renderPackDetail(created);
  } catch (error) { toast(error.message, "error"); }
});
$("#new-evidence").addEventListener("click", () => $("#evidence-dialog").showModal());
$("#evidence-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const values = new FormData(event.currentTarget);
  try {
    await api.createEvidence({ category: String(values.get("category")), claim_text: String(values.get("claim_text") || "").trim(), source_type: "user_attested", source_reference: null, exact_quote: null, source_section: null, employer_or_project: String(values.get("employer_or_project") || "").trim() || null, role: null, start_date: null, end_date: null, technologies: [], metrics: [], tags: [] });
    $("#evidence-dialog").close(); event.currentTarget.reset(); await loadEvidence(); toast("Evidence candidate saved. Confirm it before use.", "success");
  } catch (error) { toast(error.message, "error"); }
});

showView(location.hash.slice(1) || (linkedApplication ? "jobs" : "overview"));
await Promise.all([health(), loadApplications(), loadEvidence(), loadSessionHistory()]);
if (state.session) await renderSession();
