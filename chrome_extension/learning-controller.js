"use strict";

import { createFeedback, listLearningCandidates, getLearningCandidateEvents,
  getLearningCandidateConflicts, mutateLearningCandidate,
  materializeSkillCandidate, getStagedSkillCandidate, restageSkillCandidate, evaluateSkillCandidate,
  getSkillEvaluation, publishSkillCandidate, rejectStagedSkillCandidate,
  listGovernedSkills, getGovernedSkillMetrics, activateGovernedSkill,
  rollbackGovernedSkill } from "./api-client.js";

export function formatEvolutionSummary(evaluation) {
  const summary = evaluation?.summary;
  if (!summary) return "No completed evaluation.";
  const baseline = summary.baseline || {};
  const candidate = summary.candidate || {};
  const gates = Object.entries(summary.hard_gates || {})
    .map(([name, passed]) => `${name}: ${passed ? "pass" : "FAIL"}`).join("; ");
  return `Baseline task success ${baseline.task_success_rate ?? "?"}; `
    + `candidate ${candidate.task_success_rate ?? "?"}. `
    + `Activation accuracy ${candidate.activation_accuracy ?? "?"}; `
    + `false activation ${candidate.false_activation_rate ?? "?"}. `
    + `Mean latency ${baseline.mean_latency ?? "?"}s / ${candidate.mean_latency ?? "?"}s. `
    + `Mean tokens ${baseline.mean_tokens ?? "?"} / ${candidate.mean_tokens ?? "?"}. `
    + `Hard gates: ${gates}. Soft regressions: ${(summary.soft_regressions || []).join(", ") || "none"}. `
    + `Forward test: ${summary.forward_test?.passed ? "passed" : "failed"}.`;
}

export function createLearningController(root) {
  const field = root.querySelector("#learning-feedback");
  const list = root.querySelector("#learning-list");
  const message = root.querySelector("#learning-message");
  let kind = "preference_memory";
  let busy = false;

  function note(text) { message.textContent = text; }
  async function refresh() {
    if (busy) return;
    busy = true;
    list.replaceChildren();
    try {
      const response = await listLearningCandidates(kind);
      for (const candidate of response.candidates || []) {
        const card = document.createElement("article");
        card.className = "context-item";
        const heading = document.createElement("h3");
        heading.textContent = candidate.proposed_key_or_name;
        const content = document.createElement("p");
        content.textContent = candidate.proposed_content;
        const meta = document.createElement("p");
        meta.className = "hint";
        meta.textContent = `${candidate.status} · ${candidate.scope} · ${candidate.occurrence_count} sources · confidence ${candidate.confidence} · ${candidate.applications_represented?.length || 0} applications`;
        card.append(heading, content, meta);
        if (candidate.type_metadata || candidate.candidate_type === "career_evidence") {
          const privacy = document.createElement("p");
          privacy.className = "hint";
          privacy.textContent = candidate.candidate_type === "skill"
            ? "Review for personal details before approving evaluation. This will not publish the Skill."
            : "This candidate may contain personal career information; review its sources before confirming.";
          card.append(privacy);
        }
        const details = document.createElement("details");
        const summary = document.createElement("summary");
        summary.textContent = "Sources and conflicts";
        details.append(summary);
        details.addEventListener("toggle", async () => {
          if (!details.open || details.dataset.loaded) return;
          try {
            const [events, conflicts] = await Promise.all([
              getLearningCandidateEvents(candidate.candidate_id),
              getLearningCandidateConflicts(candidate.candidate_id),
            ]);
            for (const event of events.events || []) {
              const item = document.createElement("p");
              item.textContent = `${event.source_type}: ${event.original_content || event.after_content || "No text"}`;
              details.append(item);
            }
            for (const conflict of conflicts.conflicts || []) {
              const item = document.createElement("p");
              item.textContent = `Conflict: ${conflict.proposed_content}`;
              details.append(item);
            }
            details.dataset.loaded = "true";
          } catch { note("Could not load candidate sources. Refresh and retry."); }
        });
        card.append(details);
        const actions = document.createElement("div");
        actions.className = "context-actions";
        const actionNames = kind === "skill" ? ["approve-for-evaluation", "reject", "keep-collecting"]
          : kind === "product_policy" ? ["confirm", "reject"]
          : ["confirm", "edit", "reject", "keep-collecting"];
        if (!["confirmed", "rejected", "superseded", "expired"].includes(candidate.status)) {
          for (const action of actionNames) {
            if (["confirm", "approve-for-evaluation"].includes(action)
                && candidate.status !== "ready_for_review") continue;
            const button = document.createElement("button");
            button.type = "button";
            button.className = "secondary-button";
            button.textContent = kind === "product_policy" && action === "confirm"
              ? "Acknowledge issue" : action.replaceAll("-", " ");
            button.addEventListener("click", async () => {
              const edited = action === "edit" ? window.prompt("Edit candidate proposal", candidate.proposed_content) : null;
              if (action === "edit" && edited === null) return;
              try {
                await mutateLearningCandidate(candidate.candidate_id, action, candidate.version, edited);
                note("Candidate updated.");
                await refresh();
              } catch (error) {
                if (error?.status === 409) {
                  await refresh();
                  note("Candidate changed. The list was refreshed; review it before trying again.");
                } else {
                  note("Candidate action failed. Refresh and retry.");
                }
              }
            });
            actions.append(button);
          }
        }
        card.append(actions);
        if (kind === "skill" && candidate.status === "confirmed") {
          const evolution = document.createElement("section");
          const label = document.createElement("p");
          label.textContent = "Approved for evaluation; publication and activation require separate actions.";
          evolution.append(label);
          let staged = null;
          try { staged = await getStagedSkillCandidate(candidate.candidate_id); }
          catch (error) { if (error?.status !== 404) note("Could not load staged Skill state."); }
          if (!staged) {
            const stage = document.createElement("button");
            stage.type = "button";
            stage.textContent = "Materialize staged package";
            stage.addEventListener("click", async () => {
              const suggested = String(candidate.type_metadata?.proposed_skill_name || "application-answer-structure")
                .toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 63);
              const name = window.prompt("Skill name (lowercase with hyphens)", suggested);
              if (name === null) return;
              try {
                await materializeSkillCandidate(candidate.candidate_id, candidate.version, name);
                note("Staged package created. Inspect it before evaluation."); await refresh();
              } catch { note("Staging failed. Check the candidate for personal data, policy conflicts, or an invalid name."); }
            });
            evolution.append(stage);
          } else {
            const status = document.createElement("p");
            status.textContent = `Staged status: ${staged.status}; package hash ${staged.content_hash || "unavailable"}`;
            evolution.append(status);
            const preview = document.createElement("details");
            const previewTitle = document.createElement("summary");
            previewTitle.textContent = "Proposed SKILL.md and validation";
            const instructions = document.createElement("pre");
            instructions.textContent = staged.instructions || "";
            const report = document.createElement("p");
            report.textContent = `Static validation: ${staged.validation?.valid ? "passed" : "failed"}; `
              + `warnings: ${(staged.validation?.warnings || []).join("; ") || "none"}; `
              + `required tools: ${(staged.required_tools || []).join(", ") || "none"}; `
              + `active Skill conflict: ${staged.conflicting_active_skill?.version_id || "none"}`;
            preview.append(previewTitle, report, instructions);
            for (const [path, content] of Object.entries(staged.references || {})) {
              const reference = document.createElement("pre");
              reference.textContent = `${path}\n${content}`;
              preview.append(reference);
            }
            evolution.append(preview);
            if (staged.validation?.content_changed) {
              const restage = document.createElement("button");
              restage.type = "button";
              restage.textContent = "Validate edited package and reset evaluation";
              restage.addEventListener("click", async () => {
                try { await restageSkillCandidate(candidate.candidate_id, staged.version);
                  note("Edited package validated. Run a new evaluation."); await refresh(); }
                catch { note("Edited package failed validation or conflicted. No evaluation was reused."); }
              });
              evolution.append(restage);
            }
            if (!staged.validation?.content_changed &&
                ["approved_for_evaluation", "evaluation_failed"].includes(staged.status)) {
              const evaluate = document.createElement("button");
              evaluate.type = "button";
              evaluate.textContent = "Run paired evaluation (3 repetitions)";
              evaluate.addEventListener("click", async () => {
                note("Evaluation is running on the synthetic dataset. This may take several minutes.");
                try {
                  await evaluateSkillCandidate(candidate.candidate_id, staged.version, 3);
                  note("Evaluation saved. Review its gates and forward tests."); await refresh();
                } catch { note("Evaluation failed. Refresh for its safe status; nothing was published."); await refresh(); }
              });
              evolution.append(evaluate);
            }
            if (staged.evaluation_run_id) {
              try {
                const evaluation = await getSkillEvaluation(staged.evaluation_run_id, true);
                const results = document.createElement("p");
                results.textContent = `${evaluation.status}: ${formatEvolutionSummary(evaluation)}`;
                evolution.append(results);
                const outputs = document.createElement("details");
                const outputsTitle = document.createElement("summary");
                outputsTitle.textContent = "Paired outputs and forward tests";
                outputs.append(outputsTitle);
                for (const item of evaluation.case_results || []) {
                  const line = document.createElement("p");
                  line.textContent = `${item.case_id} · ${item.variant} · repetition ${item.repetition}: `
                    + `${item.output_text || "No saved output"}`;
                  outputs.append(line);
                }
                for (const item of evaluation.forward_test_results || []) {
                  const line = document.createElement("p");
                  line.textContent = `Holdout ${item.case_id}: ${item.metrics?.output_text || "No saved output"}`;
                  outputs.append(line);
                }
                evolution.append(outputs);
                staged.soft_regressions = [
                  ...(evaluation.summary?.soft_regressions || []),
                  ...(evaluation.summary?.forward_test?.soft_regressions || []),
                ];
              } catch { /* A stale evaluation never authorizes publication. */ }
            }
            if (staged.status === "ready_for_publication" && !staged.validation?.content_changed) {
              const publish = document.createElement("button");
              publish.type = "button";
              publish.textContent = "Publish immutable version (inactive)";
              publish.addEventListener("click", async () => {
                const regressions = [...new Set(staged.soft_regressions || [])];
                const message = regressions.length
                  ? `Soft regressions: ${regressions.join(", ")}. Publish anyway? It will remain inactive.`
                  : "Publish this evaluated Skill version? It will remain inactive.";
                if (!window.confirm(message)) return;
                try { await publishSkillCandidate(candidate.candidate_id, staged.version, regressions.length > 0);
                  note("Skill published inactive. Activate separately after review."); await refresh(); }
                catch { note("Publication was blocked. Refresh and inspect the evaluation or package hash."); }
              });
              evolution.append(publish);
            }
            if (staged.status !== "published" && staged.status !== "rejected") {
              const reject = document.createElement("button");
              reject.type = "button"; reject.textContent = "Reject staged candidate";
              reject.addEventListener("click", async () => {
                try { await rejectStagedSkillCandidate(candidate.candidate_id, staged.version);
                  note("Candidate rejected."); await refresh(); }
                catch { note("Rejection conflicted with current state. Refresh and retry."); }
              });
              evolution.append(reject);
            }
          }
          card.append(evolution);
        }
        list.append(card);
      }
      if (kind === "skill") {
        const governed = await listGovernedSkills();
        for (const skill of governed.skills || []) {
          const card = document.createElement("article");
          card.className = "context-item";
          const title = document.createElement("h3");
          title.textContent = `${skill.name} — Published versions`;
          card.append(title);
          const metrics = await getGovernedSkillMetrics(skill.name);
          for (const version of skill.versions || []) {
            const details = document.createElement("details");
            const summary = document.createElement("summary");
            summary.textContent = `${version.semantic_version} · ${version.status} · ${version.activation_mode}`;
            const hash = document.createElement("p");
            hash.textContent = `Content hash ${version.content_hash}; evaluation ${version.evaluation_run_id}`;
            const usage = document.createElement("p");
            const metric = (metrics.metrics || []).find(item => item.skill_version_id === version.skill_version_id);
            usage.textContent = `Selections ${metric?.selection_count ?? 0}; successes ${metric?.successful_task_count ?? 0}; failures ${metric?.failure_count ?? 0}; unsupported claims ${metric?.unsupported_claim_count ?? 0}`;
            details.append(summary, hash, usage);
            if (version.status !== "rolled_back") {
              for (const mode of ["shadow", "canary", "active"]) {
                const button = document.createElement("button");
                button.type = "button"; button.textContent = `Set ${mode}`;
                button.addEventListener("click", async () => {
                  if (!window.confirm(`Set ${skill.name} ${version.semantic_version} to ${mode}?`)) return;
                  try { await activateGovernedSkill(skill.name, version.skill_version_id, version.version, mode);
                    note("Activation mode saved."); await refresh(); }
                  catch { note("Activation conflicted or package validation failed. Refresh and retry."); }
                });
                details.append(button);
              }
            }
            if (version.activation_mode === "active") {
              const rollback = document.createElement("button");
              rollback.type = "button"; rollback.textContent = "Rollback";
              rollback.addEventListener("click", async () => {
                const reason = window.prompt("Why roll back this Skill?");
                if (!reason?.trim()) return;
                const prior = (skill.versions || []).find(item => item.parent_version_id === version.skill_version_id);
                try { await rollbackGovernedSkill(skill.name, version.skill_version_id,
                  prior?.skill_version_id || null, version.version, reason);
                  note("Rollback saved. Existing snapshots are unchanged."); await refresh(); }
                catch { note("Rollback conflicted. Refresh and choose a prior version."); }
              });
              details.append(rollback);
            }
            card.append(details);
          }
          list.append(card);
        }
      }
    } catch { note("Could not load learning candidates."); }
    finally { busy = false; }
  }
  root.querySelector("#learning-submit").addEventListener("click", async () => {
    const content = field.value.trim();
    if (!content) { note("Enter feedback first."); return; }
    try {
      await createFeedback("explicit_instruction", content);
      field.value = "";
      note("Feedback saved. Review any candidate before it is used.");
      await refresh();
    } catch { note("Could not save feedback."); }
  });
  root.querySelector("#learning-refresh").addEventListener("click", refresh);
  root.querySelectorAll("#learning-tabs button").forEach(button => button.addEventListener("click", () => {
    kind = button.dataset.kind;
    refresh();
  }));
  return { refresh };
}
