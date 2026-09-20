"use strict";

import { safeHttpUrl } from "./workspace-state.js";

function clear(element) { while (element.firstChild) element.removeChild(element.firstChild); }
function add(parent, tag, text, className = "") {
  const node = document.createElement(tag); node.textContent = text;
  if (className) node.className = className; parent.append(node); return node;
}
function label(value) { return String(value || "unknown").replaceAll("_", " "); }

export function renderApplicationList(container, applications, onOpen) {
  clear(container);
  if (!applications.length) { add(container, "p", "No saved applications match this view.", "muted"); return; }
  applications.forEach((application) => {
    const card = add(container, "article", "", "context-item workspace-list-item");
    add(card, "h3", application.title || "Untitled job");
    add(card, "p", [application.company, application.location].filter(Boolean).join(" • ") || "Company not provided");
    add(card, "p", `${label(application.status)}${Number.isFinite(application.match_score) ? ` • ${application.match_score}% match` : ""}`, "requirement-meta");
    add(card, "p", application.next_action || "No next action", "muted");
    const button = add(card, "button", "Open Workspace", "secondary-button"); button.type = "button";
    button.addEventListener("click", () => onOpen(application.application_id));
  });
}

export function renderApplicationDetail(elements, detail, events, artifacts) {
  elements.detail.hidden = false;
  elements.title.textContent = detail.job?.title || "Untitled job";
  elements.company.textContent = [detail.job?.company, detail.job?.location].filter(Boolean).join(" • ") || "Company not provided";
  elements.status.textContent = label(detail.status); elements.status.dataset.status = detail.status;
  elements.id.textContent = `Application ${detail.application_id}`;
  const href = safeHttpUrl(detail.job?.canonical_url);
  elements.source.hidden = !href; if (href) elements.source.href = href;
  elements.nextAction.value = detail.next_action || "";
  elements.deadline.value = detail.deadline_at ? detail.deadline_at.slice(0, 16) : "";
  elements.statusSelect.value = detail.status;
  elements.score.textContent = Number.isFinite(detail.latest_match_score) ? `Latest match score: ${detail.latest_match_score}%` : "Match score unavailable";
  clear(elements.missing); (detail.latest_missing_requirements || []).forEach((item) => add(elements.missing, "p", item.display_name || item.original_text || item.canonical_name || "Requirement"));
  clear(elements.resume); const resume = detail.latest_tailored_resume;
  if (resume) add(elements.resume, "pre", JSON.stringify(resume, null, 2), "workspace-json"); else add(elements.resume, "p", "No tailored resume yet.", "muted");
  clear(elements.runs); (detail.associated_runs || []).forEach((run) => add(elements.runs, "p", `${run.run_id} • ${label(run.status)}`));
  clear(elements.events); (events || []).slice(-10).reverse().forEach((event) => add(elements.events, "p", `${event.sequence}. ${label(event.event_type)}`));
  clear(elements.artifacts); (artifacts || []).forEach((artifact) => add(elements.artifacts, "p", `${label(artifact.artifact_type)} v${artifact.version} • ${label(artifact.status)}`));
}
