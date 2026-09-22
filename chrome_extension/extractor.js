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
  const isVisible = (element) => {
    if (!element) return false;
    const style = window.getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden"
      && Number(style.opacity || "1") !== 0 && rect.width > 0 && rect.height > 0;
  };
  const firstText = (selectors) => {
    for (const selector of selectors) {
      const candidates = [...document.querySelectorAll(selector)];
      const visible = candidates.find((element) => isVisible(element));
      const value = clean((visible || candidates[0])?.textContent);
      if (value) return value;
    }
    return null;
  };
  const bestContainerText = (selectors) => {
    const candidates = [];
    for (const selector of selectors) {
      for (const element of document.querySelectorAll(selector)) {
        const text = clean(element.innerText || element.textContent);
        if (text) candidates.push({ selector, text, visible: isVisible(element) });
      }
    }
    candidates.sort((left, right) => (
      Number(right.visible) - Number(left.visible)
      || right.text.length - left.text.length
      || left.selector.localeCompare(right.selector)
    ));
    return candidates[0] || null;
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
  const titleMetadata = () => {
    if (!/(^|\.)linkedin\.com$/i.test(location.hostname)) return {};
    const parts = clean(document.title).split(/\s+\|\s+/).map(clean).filter(Boolean);
    if (parts.length < 2) return {};
    return {
      job_title: parts[0] || null,
      company: parts[1] && !/^linkedin$/i.test(parts[1]) ? parts[1] : null,
    };
  };
  const jobDescriptionSlice = (value, requireHeading = false) => {
    const lines = clean(value).split("\n").map(clean).filter(Boolean);
    const headings = /^(about the job|job description|position summary|the role|role overview|关于职位|关于工作|职位描述|职位简介|岗位描述)$/i;
    const stop = /^(similar jobs|people also viewed|recommended jobs|more jobs|about the company|benefits found in job post|职位发布中说明的福利|公司简介|类似职位|推荐职位|更多职位|其他相似职位|查看类似职位)$/i;
    const start = lines.findIndex((line) => headings.test(line));
    if (start < 0 && requireHeading) return "";
    const contentStart = start >= 0 ? start + 1 : 0;
    const endOffset = lines.slice(contentStart).findIndex((line) => stop.test(line));
    return lines.slice(
      contentStart,
      endOffset >= 0 ? contentStart + endOffset : undefined,
    ).filter((line) => !/^(show more|show less|显示更多|收起)$/i.test(line)).join("\n");
  };
  const metadata = () => {
    const fromTitle = titleMetadata();
    const isLinkedIn = /(^|\.)linkedin\.com$/i.test(location.hostname);
    return {
      company: firstText(["[data-company-name]", ".job-details-jobs-unified-top-card__company-name a", ".job-details-jobs-unified-top-card__company-name", ".job-details-jobs-unified-top-card__primary-description-container a[href*='/company/']", ".job-details-jobs-unified-top-card__primary-description-container a", ".jobs-unified-top-card__company-name", ".topcard__org-name-link", "[class*='company-name' i]"]) || fromTitle.company || (isLinkedIn ? null : meta("og:site_name")),
      job_title: firstText(["[data-job-title]", "[data-testid*='job-title' i]", ".job-details-jobs-unified-top-card__job-title h1", ".job-details-jobs-unified-top-card__job-title", ".job-details-jobs-unified-top-card__job-title a", ".jobs-unified-top-card__job-title", ".top-card-layout__title", "main h1"]) || fromTitle.job_title || null,
      location: firstText(["[data-job-location]", "[data-testid*='job-location' i]", ".job-details-jobs-unified-top-card__tertiary-description-container span", ".jobs-unified-top-card__bullet", ".topcard__flavor--bullet", "[class*='job-location' i]"]),
    };
  };
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
    ".jobs-search__job-details--container .jobs-description__content",
    ".jobs-search__job-details--container .jobs-description-content__text",
    ".jobs-search__job-details--container #job-details",
    ".scaffold-layout__detail .jobs-description__content",
    ".scaffold-layout__detail #job-details",
    ".job-view-layout .jobs-description__content",
    ".job-view-layout #job-details",
    "[data-job-description]",
    "[data-testid*='job-description' i]",
    ".jobs-description__content",
    ".jobs-description-content__text",
    ".jobs-description",
    "#job-details",
    ".jobs-box__html-content",
    ".show-more-less-html__markup",
    "#job-description",
    ".job-description",
    "[class*='job-description' i]",
  ];
  const container = bestContainerText(selectors);
  if (container) {
    const description = jobDescriptionSlice(container.text);
    if (description) return base(description, `container:${container.selector}`, container.visible ? 0.95 : 0.75);
  }

  const isLinkedIn = /(^|\.)linkedin\.com$/i.test(location.hostname);
  const mainText = jobDescriptionSlice(document.querySelector("main")?.innerText, isLinkedIn);
  if (mainText) return base(mainText, "main:filtered", 0.55);
  const bodyText = jobDescriptionSlice(document.body?.innerText, isLinkedIn);
  if (bodyText) return base(bodyText, "body:filtered", 0.25);
  return base("", "none", 0);
}
