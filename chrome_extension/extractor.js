"use strict";

// This function is serialized by chrome.scripting.executeScript. Keep all of
// its dependencies inside the function body.
export function extractJobDescriptionFromPage() {
  const clean = (value) => (typeof value === "string" ? value.trim() : "");

  const selectedText = clean(window.getSelection()?.toString());
  if (selectedText) {
    return { text: selectedText, source: "selection" };
  }

  const selectors = [
    "[data-job-description]",
    "[data-testid*='job-description' i]",
    "[class*='job-description' i]",
    "#job-description",
    ".job-description",
    "article",
  ];

  for (const selector of selectors) {
    const element = document.querySelector(selector);
    const text = clean(element?.innerText);
    if (text) {
      return { text, source: `container:${selector}` };
    }
  }

  const mainText = clean(document.querySelector("main")?.innerText);
  if (mainText) {
    return { text: mainText, source: "main" };
  }

  const bodyText = clean(document.body?.innerText);
  if (bodyText) {
    return { text: bodyText, source: "body" };
  }

  return { text: "", source: "none" };
}
