"use strict";

function textOf(value) {
  if (typeof value === "string") return value.trim();
  if (value && typeof value.text === "string") return value.text.trim();
  return "";
}

function joinedClaims(values, separator = "\n") {
  if (!Array.isArray(values)) return "";
  return values.map(textOf).filter(Boolean).join(separator);
}

function renderCoverLetter(content) {
  const paragraphs = Array.isArray(content?.paragraphs)
    ? content.paragraphs : Array.isArray(content?.blocks) ? content.blocks : [];
  return [
    textOf(content?.greeting) || "Dear Hiring Team,",
    ...paragraphs.map(textOf).filter(Boolean),
    [textOf(content?.closing) || "Sincerely,", textOf(content?.signer_name)]
      .filter(Boolean).join("\n"),
  ].filter(Boolean).join("\n\n");
}

function renderStructuredResume(content) {
  if (!Array.isArray(content?.sections)) return "";
  const blocks = [];
  const header = [textOf(content?.header?.name), ...(content?.header?.contact_lines || [])]
    .map(textOf).filter(Boolean);
  if (header.length) blocks.push(header.join("\n"));
  for (const section of content.sections) {
    const lines = [textOf(section?.title || section?.section_type).toUpperCase()].filter(Boolean);
    for (const entry of (section?.entries || [])) {
      const heading = [textOf(entry?.heading), textOf(entry?.subheading)].filter(Boolean).join(" | ");
      if (heading) lines.push(heading);
      const bullets = (entry?.bullets || []).map(textOf).filter(Boolean);
      lines.push(...bullets.map(value => section?.section_type === "summary" ? value : `- ${value}`));
    }
    if (lines.length > 1) blocks.push(lines.join("\n"));
  }
  return blocks.join("\n\n");
}

function renderResume(content) {
  const structured = renderStructuredResume(content);
  if (structured) return structured;
  const sections = [
    ["PROFESSIONAL SUMMARY", joinedClaims(content?.professional_summary, " ")],
    ["EXPERIENCE", joinedClaims(content?.experience_bullets, "\n")],
    ["SKILLS", joinedClaims(content?.highlighted_skills, " • ")],
  ];
  return sections.filter(([, body]) => body).map(([title, body]) => `${title}\n${body}`).join("\n\n");
}

export function renderArtifactDocument(artifactType, rawContent) {
  const content = rawContent?.data && typeof rawContent.data === "object"
    ? rawContent.data : rawContent || {};
  if (artifactType === "cover_letter") return renderCoverLetter(content);
  if (["tailored_resume", "resume"].includes(artifactType)) return renderResume(content);
  if (artifactType === "application_answer") {
    return joinedClaims(content?.answer_blocks, "\n\n") || textOf(content?.answer);
  }
  if (artifactType === "interview_report") {
    const blocks = ["Mock interview report"];
    const scores = Object.entries(content?.average_scores || {})
      .filter(([, score]) => score != null)
      .map(([name, score]) => `${name.replaceAll("_", " ")} ${score}/5`);
    if (scores.length) blocks.push(`Scores: ${scores.join(", ")}`);
    const gaps = (content?.recurring_gaps || []).map(textOf).filter(Boolean);
    if (gaps.length) blocks.push(`Areas to improve:\n${gaps.map(item => `- ${item}`).join("\n")}`);
    const priorities = (content?.practice_priorities || []).map(textOf).filter(Boolean);
    if (priorities.length) {
      blocks.push(`Practice priorities:\n${priorities.map(item => `- ${item}`).join("\n")}`);
    }
    for (const [index, item] of (content?.question_answer_summaries || []).entries()) {
      const lines = [`Question ${index + 1}: ${textOf(item?.question)}`];
      if (textOf(item?.answer)) lines.push(`Answer: ${textOf(item.answer)}`);
      if (item?.overall_score != null) lines.push(`Score: ${item.overall_score}/5`);
      blocks.push(lines.join("\n"));
    }
    return blocks.join("\n\n");
  }
  return textOf(content?.text) || textOf(content?.summary);
}

export function artifactFileName(artifactType) {
  const names = {
    tailored_resume: "tailored-resume.txt",
    resume: "tailored-resume.txt",
    cover_letter: "cover-letter.txt",
    application_answer: "application-answer.txt",
    interview_report: "interview-report.txt",
  };
  return names[artifactType] || "application-document.txt";
}
