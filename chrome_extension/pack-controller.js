"use strict";

import {
  createApplicationPack, listApplicationPacks, getApplicationPack,
  generatePackItem, mutatePackItem, getPackItemEvidence, getPackItemVersions,
} from "./api-client.js";

const mutationKey = () => crypto.randomUUID();
const node = (tag, text = "") => {
  const element = document.createElement(tag);
  element.textContent = String(text ?? "");
  return element;
};

export function packText(item) {
  const content = item.content;
  if (!content) return "";
  if (item.artifact_type === "tailored_resume") {
    return ["professional_summary", "experience_bullets", "highlighted_skills"]
      .flatMap((key) => (content[key] || []).map((block) => block.text)).join("\n");
  }
  if (item.artifact_type === "cover_letter") {
    return [content.greeting, ...(content.blocks || []).map((block) => block.text), content.closing]
      .filter(Boolean).join("\n\n");
  }
  return (content.answer_blocks || []).map((block) => block.text).join(" ");
}

function blocksFor(item) {
  if (!item.content) return [];
  if (item.artifact_type === "tailored_resume") {
    return ["professional_summary", "experience_bullets", "highlighted_skills"]
      .flatMap((key) => (item.content[key] || []).map((block, index) => ({ key, index, block })));
  }
  const key = item.artifact_type === "cover_letter" ? "blocks" : "answer_blocks";
  return (item.content[key] || []).map((block, index) => ({ key, index, block }));
}

export function createPackController({ elements, getApplication, onMessage }) {
  let applicationId = null;
  let detail = null;
  let selectedType = "tailored_resume";
  let busy = false;
  const message = (value, kind = "info") => onMessage(value, kind);
  const button = (label, action) => {
    const control = node("button", label);
    control.type = "button";
    control.className = "text-button";
    control.addEventListener("click", action);
    return control;
  };

  function render() {
    const pack = detail?.pack;
    elements.status.textContent = pack
      ? `Pack #${pack.generation_number} · ${pack.status}${pack.stale_reason ? ` · ${pack.stale_reason}` : ""}`
      : "No Pack loaded.";
    elements.items.replaceChildren();
    if (!pack) return;
    for (const item of detail.items.filter((entry) => entry.artifact_type === selectedType)) {
      const card = node("section");
      card.className = "card";
      card.append(node("h4", `${item.artifact_type.replaceAll("_", " ")} · ${item.status}`));
      if (item.source_question) card.append(node("p", `Question: ${item.source_question}`));
      if (item.requires_manual_answer) {
        card.append(node("p", "Manual answer required. The agent will not infer identity, eligibility or personal details."));
      }
      if (item.content) {
        const body = node("pre", packText(item));
        body.className = "pack-body";
        card.append(body);
      }
      if (item.verification) {
        const details = node("details");
        details.append(node("summary", `Verification: ${item.verification.passed ? "passed" : "failed"}`));
        for (const issue of item.verification.issues) details.append(node("p", `${issue.block_id}: ${issue.reason}`));
        card.append(details);
      }
      const actions = node("div");
      actions.className = "workspace-actions";
      if (item.content) actions.append(button("Copy", async () => {
        try { await navigator.clipboard.writeText(packText(item)); message("Copied material.", "success"); }
        catch { message("Could not copy material.", "error"); }
      }));
      if (item.status === "awaiting_review" && !item.requires_manual_answer) {
        actions.append(button("Approve", () => mutate(item, "approve")));
        actions.append(button("Reject", () => mutate(item, "reject")));
      }
      if (["awaiting_review", "needs_revision", "rejected"].includes(item.status) && item.content) {
        const edit = node("details");
        edit.append(node("summary", "Edit cited blocks"));
        const edits = blocksFor(item).map(({ key, index, block }) => {
          const label = node("label", `${key} ${index + 1}`);
          const field = node("textarea");
          field.rows = 3;
          field.maxLength = 5000;
          field.value = block.text;
          label.append(field);
          edit.append(label);
          return { key, index, field };
        });
        edit.append(button("Save new verified version", () => {
          const content = structuredClone(item.content);
          for (const { key, index, field } of edits) content[key][index].text = field.value.trim();
          if (item.artifact_type === "application_answer") {
            const joined = content.answer_blocks.map((block) => block.text).join(" ");
            content.character_count = joined.length;
            content.word_count = joined.trim() ? joined.trim().split(/\s+/).length : 0;
          }
          mutate(item, "edit", { content });
        }));
        card.append(edit);
      }
      if (["awaiting_review", "needs_revision", "rejected", "failed"].includes(item.status) && !item.requires_manual_answer) {
        actions.append(button("Regenerate", () => mutate(item, "regenerate")));
      }
      actions.append(button("Evidence", async () => {
        try {
          const response = await getPackItemEvidence(pack.pack_id, item.pack_item_id);
          const details = node("details"); details.open = true;
          details.append(node("summary", "Cited confirmed evidence"));
          for (const evidence of response.evidence) details.append(node("p",
            `${evidence.evidence_id} · version ${evidence.evidence_version_id} · ${evidence.claim_text} · ${evidence.source_type} · ${evidence.source_section || "Source section unavailable"} · ${evidence.confirmation_label}`));
          card.append(details);
        } catch (error) { message(error.message, "error"); }
      }));
      actions.append(button("Versions", async () => {
        try {
          const response = await getPackItemVersions(pack.pack_id, item.pack_item_id);
          const details = node("details"); details.open = true;
          details.append(node("summary", "Immutable version history"));
          for (const version of response.versions) details.append(node("p",
            `Version ${version.version} · ${version.status} · ${version.verification_status}`));
          card.append(details);
        } catch (error) { message(error.message, "error"); }
      }));
      card.append(actions);
      elements.items.append(card);
    }
  }

  async function refresh() {
    if (!applicationId) return;
    const response = await listApplicationPacks(applicationId);
    detail = response.packs.length ? await getApplicationPack(response.packs[0].pack_id) : null;
    render();
  }

  async function run(action) {
    if (busy || !applicationId) return;
    busy = true;
    try { await action(); await refresh(); }
    catch (error) {
      if (error.status === 409) await refresh();
      message(error.message || "Application Pack request failed.", "error");
    } finally { busy = false; }
  }

  function mutate(item, action, extra = {}) {
    run(async () => {
      detail = await mutatePackItem(detail.pack.pack_id, item.pack_item_id,
        action, item.version, mutationKey(), extra);
      message(`${action} completed.`, "success");
    });
  }

  elements.create.addEventListener("click", () => run(async () => {
    const application = getApplication();
    detail = await createApplicationPack(applicationId, application.version, mutationKey());
    message("New Pack created from current evidence and Job snapshot.", "success");
  }));
  const generate = (action, extra = {}) => run(async () => {
    if (!detail) throw new Error("Create a Pack first.");
    detail = await generatePackItem(detail.pack.pack_id, action, detail.pack.version, mutationKey(), extra);
    message("Pack item generated. Review its citations before approval.", "success");
  });
  elements.resume.addEventListener("click", () => generate("resume"));
  elements.cover.addEventListener("click", () => generate("cover-letter"));
  elements.answer.addEventListener("click", () => {
    const question = elements.question.value.trim();
    if (!question) { message("Enter an application question first.", "error"); return; }
    const value = elements.maxLength.value;
    const max_length = value ? Number(value) : null;
    generate("questions", { question, max_length });
  });
  for (const [control, type] of [[elements.tabResume, "tailored_resume"],
    [elements.tabCover, "cover_letter"], [elements.tabAnswers, "application_answer"]]) {
    control.addEventListener("click", () => { selectedType = type; render(); });
  }
  return { load: async (id) => {
    applicationId = id; detail = null; render();
    try { await refresh(); }
    catch (error) { message(error.message || "Could not load Application Pack.", "error"); }
  }, refresh };
}
