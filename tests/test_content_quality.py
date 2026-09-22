from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from agent_runtime.application_pack.rendering import render_cover_letter
from agent_runtime.application_pack.types import (
    ApplicationAnswer, CoverLetter, CoverLetterParagraph, EvidenceSnapshotItem,
    GroundedBlock,
)
from agent_runtime.application_pack.verifier import ArtifactVerifier
from agent_runtime.evidence.extraction import (
    career_fact_candidates, extract_evidence_candidates, validate_candidate_source,
)
from agent_runtime.memory.proposals import (
    MemoryProposal, extract_memory_proposals, proposal_conflicts,
)
from job_agent.domain import normalize_resume_analysis, normalize_tailored_resume_claim_ids
from job_agent.quality import (
    claim_similarity, clean_tailored_resume, deduplicate_claims,
    find_duplicate_claims, validate_resume_structure,
)
from job_agent.rendering import render_tailored_resume, render_tailored_resume_markdown
from job_agent.schemas import (
    ResumeAnalysis, ResumeEntry, ResumeEvidence, ResumeSection, ResumeSourceEntry,
    SupportedClaim, TailoredResume,
)


def claim(text: str, claim_id: str, source: str = "SRC-work", evidence: str = "EXP-1"):
    return SupportedClaim(claim_id=claim_id, text=text, evidence_ids=[evidence],
                          source_entry_id=source)


def structured_resume(*, duplicate=False) -> TailoredResume:
    work = [claim("Built Python APIs for internal reporting.", "CLM-work")]
    if duplicate:
        work.append(claim("built python APIs for internal reporting!", "CLM-duplicate"))
    return TailoredResume(header={"name": "Alex"}, sections=[
        ResumeSection(section_type="summary", title="Professional Summary", entries=[
            ResumeEntry(entry_id="summary", bullets=[claim(
                "Backend developer focused on reliable data services.", "CLM-summary")])]),
        ResumeSection(section_type="experience", title="Experience", entries=[
            ResumeEntry(entry_id="SRC-work", heading="Developer", subheading="Acme",
                        start_date="2022", end_date="2024", bullets=work)]),
        ResumeSection(section_type="projects", title="Projects", entries=[
            ResumeEntry(entry_id="SRC-project", heading="Forecasting", bullets=[claim(
                "Created a Python forecasting prototype.", "CLM-project", "SRC-project", "EXP-2")])]),
        ResumeSection(section_type="education", title="Education", entries=[
            ResumeEntry(entry_id="SRC-education", heading="B.S. Computer Science",
                        subheading="Example University", bullets=[claim(
                            "B.S. Computer Science, Example University.", "CLM-education",
                            "SRC-education", "EXP-3")])]),
        ResumeSection(section_type="skills", title="Skills", entries=[
            ResumeEntry(entry_id="skills", bullets=[claim("Python", "CLM-skill")])]),
    ])


def sources():
    return [
        ResumeSourceEntry(source_entry_id="SRC-work", entry_type="experience",
                          heading="Developer", evidence_ids=["EXP-1"]),
        ResumeSourceEntry(source_entry_id="SRC-project", entry_type="project",
                          heading="Forecasting", evidence_ids=["EXP-2"]),
        ResumeSourceEntry(source_entry_id="SRC-education", entry_type="education",
                          heading="B.S. Computer Science", evidence_ids=["EXP-3"]),
    ]


def test_structured_resume_preserves_sections_and_renders_cleanly():
    resume = structured_resume()
    assert resume.schema_version == 2
    assert [section.section_type for section in resume.sections] == [
        "summary", "experience", "projects", "education", "skills"]
    text = render_tailored_resume(resume)
    assert "Developer | Acme" in text and "PROJECTS\nForecasting" in text
    assert "EDUCATION\nB.S. Computer Science | Example University" in text
    assert "SKILLS\nPython" in text and "None" not in text and "null" not in text
    assert render_tailored_resume(resume) == text
    assert "## Experience" in render_tailored_resume_markdown(resume)


def test_v1_resume_upgrades_explicitly_and_claim_ids_are_stable():
    old = {"professional_summary": [{"text": "Python developer.", "evidence_ids": ["EXP-1"]}],
           "experience_bullets": [], "highlighted_skills": []}
    first = TailoredResume.model_validate(old)
    second = TailoredResume.model_validate(old)
    assert first.schema_version == 2
    assert first.claims()[0].claim_id == second.claims()[0].claim_id
    with pytest.raises(Exception):
        TailoredResume.model_validate({"professional_summary": []})


def test_source_and_claim_ids_ignore_temporary_model_ids():
    raw = lambda evidence_id, source_id: ResumeAnalysis(
        summary="Developer", skills=["Python"], education=[],
        evidence=[ResumeEvidence(evidence_id=evidence_id, source_section="Experience",
                                 exact_text="Built Python APIs.")],
        source_entries=[ResumeSourceEntry(source_entry_id=source_id, entry_type="experience",
                                          heading="Developer", organization="Acme",
                                          evidence_ids=[evidence_id])])
    left, right = normalize_resume_analysis(raw("temp-a", "source-a")), normalize_resume_analysis(raw("temp-b", "source-b"))
    assert left.evidence[0].evidence_id == right.evidence[0].evidence_id
    assert left.source_entries[0].source_entry_id == right.source_entries[0].source_entry_id


def test_identical_text_under_two_employers_is_not_merged():
    analysis = ResumeAnalysis(summary="Developer", skills=[], education=[], evidence=[
        ResumeEvidence(evidence_id="a", source_section="Acme", exact_text="Built reporting APIs."),
        ResumeEvidence(evidence_id="b", source_section="Beta", exact_text="Built reporting APIs."),
    ], source_entries=[
        ResumeSourceEntry(source_entry_id="tmp-a", entry_type="experience", heading="Engineer",
                          organization="Acme", evidence_ids=["a"]),
        ResumeSourceEntry(source_entry_id="tmp-b", entry_type="experience", heading="Engineer",
                          organization="Beta", evidence_ids=["b"]),
    ])
    normalized = normalize_resume_analysis(analysis)
    assert len(normalized.evidence) == 2
    assert len({item.evidence_id for item in normalized.evidence}) == 2
    assert len({item.source_entry_id for item in normalized.evidence}) == 2


def test_dedup_detects_exact_variants_and_paraphrases_without_python_false_positive():
    values = [
        claim("- Built Python APIs for internal reporting.", "a"),
        claim("built python APIs for internal reporting!", "b"),
        claim("Built Python APIs for scheduled internal reporting", "c"),
        claim("Used Python to analyze customer survey data.", "d"),
    ]
    assert claim_similarity(values[0].text, values[1].text) == 1
    duplicates = find_duplicate_claims(values, threshold=.70)
    assert {item.removed_claim_id for item in duplicates} >= {"b", "c"}
    kept, _ = deduplicate_claims(values, threshold=.70)
    assert any(item.claim_id == "d" for item in kept)


def test_cleanup_removes_duplicate_and_records_adjustment():
    cleaned, duplicates = clean_tailored_resume(structured_resume(duplicate=True))
    assert len(duplicates) == 1
    assert len(cleaned.claims_for("experience")) == 1
    assert cleaned.quality_adjustments


def test_cleanup_normalizes_postgres_skill_aliases():
    resume = structured_resume()
    data = resume.model_dump(mode="python")
    data["sections"][-1]["entries"][0]["bullets"] = [
        claim("Postgres", "postgres").model_dump(mode="python"),
        claim("PostgreSQL", "postgresql").model_dump(mode="python"),
    ]
    cleaned, _ = clean_tailored_resume(TailoredResume.model_validate(data))
    assert [item.text for item in cleaned.highlighted_skills] == ["Postgres"]


def test_quality_catches_summary_repeat_unknown_source_and_duplicate_skill():
    resume = structured_resume()
    data = resume.model_dump(mode="python")
    data["sections"][0]["entries"][0]["bullets"][0] = claim(
        "Built Python APIs for internal reporting.", "sum-copy").model_dump(mode="python")
    data["sections"][-1]["entries"][0]["bullets"].append(
        claim("python", "skill-copy").model_dump(mode="python"))
    data["sections"][2]["entries"][0]["bullets"][0]["source_entry_id"] = "SRC-missing"
    issues = validate_resume_structure(TailoredResume.model_validate(data), sources(),
                                       known_evidence_ids={"EXP-1", "EXP-2", "EXP-3"})
    assert {item.code for item in issues} >= {
        "summary_repeats_bullet", "duplicate_skill", "unknown_source_entry"}


def test_evidence_candidates_split_facts_and_exclude_preference_from_vault_path():
    message = "Keep my resume summary to two sentences. I built Python APIs and I reduced latency by 20%."
    candidates = extract_evidence_candidates(message, message_id="m1", session_id="s1")
    facts = career_fact_candidates(candidates)
    assert len(candidates) == 3 and len(facts) == 2
    assert all(item.source_quote in message for item in candidates)
    assert all("Keep my" not in item.normalized_fact for item in facts)
    assert any(item.fact_type == "preference" for item in candidates)
    broken = candidates[0].model_copy(update={"source_quote": "not present"})
    with pytest.raises(ValueError):
        validate_candidate_source(broken, message)


def test_conflicting_evidence_is_flagged_for_confirmation():
    old = SimpleNamespace(evidence_id="old", current=SimpleNamespace(
        claim_text="Worked at Acme as an analyst.", employer_or_project="Acme"))
    candidates = extract_evidence_candidates("I worked at Acme as an engineer.", existing=[old])
    assert candidates[0].requires_confirmation
    assert candidates[0].conflict_evidence_ids == ["old"]


def test_memory_proposal_is_typed_concise_and_separate_from_fact():
    message = "Keep my resume summary to two sentences. I built Python APIs."
    proposals = extract_memory_proposals(message)
    assert [(item.key, item.value) for item in proposals] == [("resume.summary.max_sentences", 2)]
    assert proposals[0].value != message
    assert not extract_memory_proposals("I built Python APIs.")
    with pytest.raises(Exception):
        MemoryProposal(key="resume.summary.max_sentences", value="two", scope="resume",
            source_quote="two", confidence=1, durability="long_term", operation="create", reason="x")


def test_one_time_memory_and_explicit_preference_conflict():
    proposal = extract_memory_proposals("For this one, keep writing concise.")[0]
    assert proposal.scope == "job" and proposal.durability == "session"
    assert proposal_conflicts({"value": "detailed"}, proposal,
                              existing_is_explicit=True, proposal_is_inferred=True)


def test_cover_letter_v1_adapter_renderer_and_quality_checks():
    old = CoverLetter.model_validate({"blocks": [{"block_id": "one", "text": "Built APIs.",
        "block_type": "factual", "evidence_ids": ["E1"], "evidence_version_ids": ["V1"]}]})
    assert old.schema_version == 2 and old.greeting == "Dear Hiring Team,"
    assert "Built APIs." in render_cover_letter(old)
    paragraph = CoverLetterParagraph(paragraph_type="evidence", text="Built APIs.",
                                     evidence_ids=["E1"], evidence_version_ids=["V1"])
    letter = CoverLetter(paragraphs=[
        CoverLetterParagraph(paragraph_type="opening", text="Engineer role fit."), paragraph, paragraph])
    evidence = [EvidenceSnapshotItem(evidence_id="E1", evidence_version_id="V1",
        content_hash="h", source_type="user_attested", selection_reason="test",
        associated_requirement_ids=[], claim_text="Built APIs.")]
    verdict = ArtifactVerifier().verify(letter.model_dump(mode="json"), "cover_letter", evidence,
                                        expected_role="Engineer")
    assert not verdict.passed
    assert any("repeats a paragraph" in item.reason for item in verdict.issues)


def test_cover_letter_rejects_unknown_evidence_and_generic_template():
    letter = CoverLetter(paragraphs=[
        CoverLetterParagraph(paragraph_type="opening",
            text="I am writing to express my interest in the Engineer role."),
        CoverLetterParagraph(paragraph_type="fit", text="Built APIs.",
            evidence_ids=["unknown"], evidence_version_ids=["unknown-version"]),
        CoverLetterParagraph(paragraph_type="closing", text="Thank you for your consideration."),
    ])
    verdict = ArtifactVerifier().verify(letter.model_dump(mode="json"), "cover_letter", [],
                                        expected_role="Engineer")
    assert not verdict.passed
    reasons = " ".join(item.reason for item in verdict.issues)
    assert "generic template" in reasons and "Citation does not resolve" in reasons


def test_natural_grounded_cover_letter_passes_but_copied_bullet_does_not():
    evidence = [EvidenceSnapshotItem(evidence_id="E1", evidence_version_id="V1",
        content_hash="h", source_type="user_attested", selection_reason="test",
        associated_requirement_ids=["REQ-1"], claim_text="Built Python APIs.")]
    natural = CoverLetter(paragraphs=[
        CoverLetterParagraph(paragraph_type="opening",
            text="I am applying for the Backend Engineer role because it centers on reliable API delivery."),
        CoverLetterParagraph(paragraph_type="evidence",
            text="One relevant example from my background is that I Built Python APIs. This work aligns with the role's backend focus.",
            evidence_ids=["E1"], evidence_version_ids=["V1"],
            target_requirement_ids=["REQ-1"]),
        CoverLetterParagraph(paragraph_type="motivation",
            text="I would welcome the opportunity to contribute to the team's stated priorities."),
    ])
    assert ArtifactVerifier().verify(natural.model_dump(mode="json"), "cover_letter",
        evidence, expected_role="Backend Engineer").passed
    copied = CoverLetter(paragraphs=[
        CoverLetterParagraph(paragraph_type="opening", text="Backend Engineer role."),
        CoverLetterParagraph(paragraph_type="evidence", text="Built Python APIs.",
            evidence_ids=["E1"], evidence_version_ids=["V1"]),
        CoverLetterParagraph(paragraph_type="motivation",
            text="I would welcome the opportunity to contribute."),
    ])
    verdict = ArtifactVerifier().verify(copied.model_dump(mode="json"), "cover_letter",
        evidence, expected_role="Backend Engineer")
    assert not verdict.passed
    assert any("merely copies" in issue.reason for issue in verdict.issues)


def test_application_answer_must_not_be_a_bare_resume_bullet():
    evidence = [EvidenceSnapshotItem(evidence_id="E1", evidence_version_id="V1",
        content_hash="h", source_type="user_attested", selection_reason="test",
        associated_requirement_ids=[], claim_text="Built Python APIs.")]
    answer = ApplicationAnswer(question="What relevant experience do you have?",
        answer_blocks=[GroundedBlock(block_id="answer-1", text="Built Python APIs.",
            block_type="factual", evidence_ids=["E1"], evidence_version_ids=["V1"])],
        character_count=18, word_count=3)
    verdict = ArtifactVerifier().verify(answer.model_dump(mode="json"),
        "application_answer", evidence, expected_question=answer.question)
    assert not verdict.passed
    assert any("merely copies" in issue.reason for issue in verdict.issues)
