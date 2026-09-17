"use strict";

const ACTIVE_JOB_TAB_KEY = "jobAgentActiveTab";

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

  // This call must happen synchronously within the action-click handler.
  // Awaiting another promise first causes Chrome to discard the user gesture.
  const openPanel = chrome.sidePanel.open({
    tabId: tab.id,
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
