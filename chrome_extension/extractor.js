"use strict";

// This function is serialized by chrome.scripting.executeScript. Keep all of
// its dependencies inside the function body.
export function extractJobDescriptionFromPage() {
  const clean = (value) => (typeof value === "string"
    ? value.replace(/\r/g, "").replace(/[ \t]+/g, " ").replace(/\n{3,}/g, "\n\n").trim()
    : "");
  const meta = (name) => clean(
    document.querySelector(`meta[name="${name}"], meta[property="${name}"]`)?.content,
  ) || null;
  const firstText = (selectors) => {
    for (const selector of selectors) {
      const value = clean(document.querySelector(selector)?.textContent);
      if (value) return value;
    }
    return null;
  };
  const htmlText = (value) => typeof value === "string"
    ? clean(new DOMParser().parseFromString(value, "text/html").body?.textContent)
    : "";
  const findJobPosting = (value) => {
    if (Array.isArray(value)) {
      for (const item of value) {
        const found = findJobPosting(item);
        if (found) return found;
      }
      return null;
    }
    if (!value || typeof value !== "object") return null;
    const types = Array.isArray(value["@type"]) ? value["@type"] : [value["@type"]];
    if (types.includes("JobPosting")) return value;
    return findJobPosting(value["@graph"]);
  };
  const structuredPosting = () => {
    for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
      try {
        const found = findJobPosting(JSON.parse(script.textContent || "null"));
        if (found) return found;
      } catch (_error) {
        // Invalid page-owned JSON is ignored.
      }
    }
    return null;
  };
  const addressText = (posting) => {
    const item = Array.isArray(posting?.jobLocation) ? posting.jobLocation[0] : posting?.jobLocation;
    const address = item?.address;
    if (typeof address === "string") return clean(address) || null;
    if (!address || typeof address !== "object") return null;
    return [address.addressLocality, address.addressRegion, address.addressCountry]
      .map(clean).filter(Boolean).join(", ") || null;
  };
  const relevantFallback = (value) => {
    const lines = clean(value).split("\n").map(clean).filter(Boolean);
    const headings = /^(about the job|job description|position summary|the role|role overview)$/i;
    const stop = /^(similar jobs|people also viewed|recommended jobs|more jobs|about the company)$/i;
    const start = lines.findIndex((line) => headings.test(line));
    if (start < 0) return lines.join("\n");
    const endOffset = lines.slice(start + 1).findIndex((line) => stop.test(line));
    return lines.slice(start, endOffset >= 0 ? start + 1 + endOffset : undefined).join("\n");
  };
  const metadata = () => ({
    company: firstText(["[data-company-name]", ".job-details-jobs-unified-top-card__company-name", ".topcard__org-name-link", "[class*='company-name' i]"]) || meta("og:site_name"),
    job_title: firstText(["[data-job-title]", "[data-testid*='job-title' i]", ".job-details-jobs-unified-top-card__job-title h1", ".top-card-layout__title", "main h1"]),
    location: firstText(["[data-job-location]", "[data-testid*='job-location' i]", ".topcard__flavor--bullet", "[class*='job-location' i]"]),
  });
  const base = (text, source, confidence, values = metadata()) => ({
    source_url: location.href,
    source_site: location.hostname,
    page_title: clean(document.title) || null,
    company: values.company || null,
    job_title: values.job_title || null,
    location: values.location || null,
    raw_page_text: text || null,
    cleaned_job_description: text,
    extraction_source: source,
    extraction_confidence: confidence,
    extracted_at: new Date().toISOString(),
  });

  const selectedText = clean(window.getSelection()?.toString());
  if (selectedText) return base(selectedText, "selection", 1);

  const posting = structuredPosting();
  const structuredText = htmlText(posting?.description);
  if (structuredText) {
    return base(structuredText, "structured:JobPosting", 0.98, {
      company: clean(posting?.hiringOrganization?.name) || null,
      job_title: clean(posting?.title) || null,
      location: addressText(posting),
    });
  }

  const selectors = [
    "[data-job-description]",
    "[data-testid*='job-description' i]",
    ".jobs-description__content",
    ".jobs-box__html-content",
    ".show-more-less-html__markup",
    "#job-description",
    ".job-description",
    "[class*='job-description' i]",
  ];
  for (const selector of selectors) {
    const text = clean(document.querySelector(selector)?.innerText);
    if (text) return base(text, `container:${selector}`, 0.9);
  }

  const mainText = relevantFallback(document.querySelector("main")?.innerText);
  if (mainText) return base(mainText, "main:filtered", 0.55);
  const bodyText = relevantFallback(document.body?.innerText);
  if (bodyText) return base(bodyText, "body:filtered", 0.25);
  return base("", "none", 0);
}
