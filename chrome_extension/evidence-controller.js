"use strict";

import {
  createCareerEvidence, getCareerEvidenceVersions, listApplicationEvidence,
  listCareerEvidence, mutateCareerEvidence,
} from "./api-client.js";

export function provenanceLabel(item) {
  if (item.status === "candidate") return "Pending confirmation";
  if (item.current?.source_type === "resume") return "Grounded in resume";
  if (item.status === "confirmed") return "Confirmed by you";
  return item.status || "Unknown";
}

export function createEvidenceController({ elements, applicationId, onError }) {
  let busy = false;

  async function refresh() {
    if (busy) return;
    busy = true;
    try {
      const response = await listCareerEvidence({
        search: elements.search.value.trim(), category: elements.filter.value,
      });
      const items = (response.items || []).filter(
        (item) => item.status === "candidate" || item.status === "confirmed",
      );
      const currentApplication = applicationId();
      const links = currentApplication
        ? (await listApplicationEvidence(currentApplication)).links || [] : [];
      const linked = new Set(links.map((link) => link.evidence_id));
      elements.list.replaceChildren();
      if (!items.length) {
        const empty = document.createElement("p");
        empty.textContent = "No active Career Evidence found.";
        elements.list.append(empty);
      }
      for (const item of items) {
        const card = document.createElement("div");
        card.className = "context-item";
        const title = document.createElement("h3");
        title.textContent = item.current.claim_text;
        const meta = document.createElement("p");
        meta.className = "muted";
        meta.textContent = [item.current.category, provenanceLabel(item),
          linked.has(item.evidence_id) ? "Linked to current application" : ""].filter(Boolean).join(" · ");
        card.append(title, meta);
        if (item.status === "candidate") {
          for (const action of ["confirm", "reject"]) {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "secondary-button";
            button.textContent = action === "confirm" ? "Confirm" : "Reject";
            button.addEventListener("click", async () => {
              try {
                await mutateCareerEvidence(item.evidence_id, action, item.version);
                await refresh();
              } catch (error) { onError(error); }
            });
            card.append(button);
          }
        }
        const details = document.createElement("details");
        const summary = document.createElement("summary");
        summary.textContent = "Version history";
        details.append(summary);
        details.addEventListener("toggle", async () => {
          if (!details.open || details.dataset.loaded) return;
          try {
            const response = await getCareerEvidenceVersions(item.evidence_id);
            for (const version of response.versions || []) {
              const line = document.createElement("p");
              line.textContent = `v${version.version_number} · ${version.claim_text}`;
              details.append(line);
            }
            details.dataset.loaded = "true";
          } catch (error) { onError(error); }
        });
        card.append(details);
        elements.list.append(card);
      }
    } catch (error) { onError(error); }
    finally { busy = false; }
  }

  async function create() {
    const statement = elements.claim.value.trim();
    if (!statement) { onError(new Error("Enter a statement first.")); return; }
    try {
      await createCareerEvidence({
        category: elements.category.value, claim_text: statement,
        source_type: "user_attested", exact_quote: statement,
      });
      elements.claim.value = "";
      await refresh();
    } catch (error) { onError(error); }
  }

  elements.refresh.addEventListener("click", refresh);
  elements.create.addEventListener("click", create);
  elements.search.addEventListener("change", refresh);
  elements.filter.addEventListener("change", refresh);
  return { refresh };
}
