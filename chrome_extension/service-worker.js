"use strict";

import { extractJobDescriptionFromPage } from "./extractor.js";

const ACTIVE_JOB_TAB_KEY = "jobAgentActiveTab";
const ACTIVE_JOB_EXTRACTION_KEY = "jobAgentActiveExtraction";

chrome.sidePanel
  .setPanelBehavior({
    openPanelOnActionClick: false,
  })
  .catch(console.error);

chrome.action.onClicked.addListener((tab) => {
  if (!tab?.id) {
    return;
  }

  const validPage = typeof tab.url === "string" && /^https?:\/\//.test(tab.url);

  const saveTarget = chrome.storage.session.set({
    [ACTIVE_JOB_TAB_KEY]: {
      tabId: tab.id,
      url: tab.url || null,
      validPage,
      selectedAt: Date.now(),
    },
  });

  // Start extraction directly inside the toolbar-click gesture. This makes
  // activeTab access deterministic even when the Side Panel document has not
  // finished loading yet.
  const extractPage = validPage
    ? chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: extractJobDescriptionFromPage,
      })
    : Promise.resolve([]);

  // This call must happen synchronously within the action-click handler.
  // Awaiting another promise first causes Chrome to discard the user gesture.
  const openPanel = chrome.sidePanel.open({
    tabId: tab.id,
  });

  extractPage.then((results) => chrome.storage.session.set({
    [ACTIVE_JOB_EXTRACTION_KEY]: {
      tabId: tab.id,
      selectedAt: Date.now(),
      result: results?.[0]?.result || null,
    },
  })).catch(() => {
    // The Side Panel reports a safe extraction error and retains its manual
    // fallback. Do not persist page or Chrome exception details here.
  });

  Promise.allSettled([saveTarget, openPanel]).then(([saveResult, openResult]) => {
    if (saveResult.status === "rejected") {
      console.error("Could not remember the selected job tab.", saveResult.reason);
    }
    if (openResult.status === "rejected") {
      console.error("Could not open the Job Agent side panel.", openResult.reason);
    }
  });
});
