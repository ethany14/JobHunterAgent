import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "chrome_extension"
REQUIRED_FILES = {
    "manifest.json",
    "service-worker.js",
    "sidepanel.html",
    "sidepanel.css",
    "sidepanel.js",
    "api-client.js",
    "extractor.js",
    "README.md",
}


def test_extension_has_required_files() -> None:
    assert REQUIRED_FILES <= {path.name for path in EXTENSION.iterdir() if path.is_file()}


def test_manifest_is_valid_and_minimally_scoped() -> None:
    manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["manifest_version"] == 3
    assert manifest["minimum_chrome_version"] == "116"
    assert set(manifest["permissions"]) == {
        "activeTab",
        "scripting",
        "storage",
        "sidePanel",
    }
    assert manifest["host_permissions"] == ["http://localhost:8000/*"]
    assert "optional_host_permissions" not in manifest
    assert manifest["background"]["service_worker"] == "service-worker.js"
    assert manifest["side_panel"]["default_path"] == "sidepanel.html"
    assert "<all_urls>" not in json.dumps(manifest)
    assert {"cookies", "history", "webRequest", "downloads", "tabs"}.isdisjoint(
        manifest["permissions"]
    )


def test_api_client_paths_match_fastapi_routes() -> None:
    client = (EXTENSION / "api-client.js").read_text(encoding="utf-8")
    routes = (ROOT / "api" / "routes" / "runs.py").read_text(encoding="utf-8")
    main = (ROOT / "api" / "main.py").read_text(encoding="utf-8")
    session_routes = (ROOT / "api" / "session_routes.py").read_text(encoding="utf-8")

    assert 'API_BASE_URL = "http://localhost:8000"' in client
    assert 'request("/health")' in client
    assert 'request("/runs"' in client
    assert '/runs/${encodeURIComponent(runId)}' in client
    assert '/runs/${encodeURIComponent(runId)}/review' in client
    assert '@application.get("/health"' in main
    assert 'APIRouter(prefix="/runs"' in routes
    assert '@router.get("/{run_id}"' in routes
    assert '@router.post("/{run_id}/review"' in routes
    assert 'request("/sessions"' in client
    assert 'request(`/sessions?limit=${encodeURIComponent(limit)}`)' in client
    assert '/sessions/${encodeURIComponent(sessionId)}' in client
    assert '/messages`' in client
    assert '/tool-calls/${encodeURIComponent(toolCallId)}/approve' in client
    assert '/tool-calls/${encodeURIComponent(toolCallId)}/reject' in client
    assert '/cancel`' in client
    assert '/recover`' in client
    assert 'capability_profile: "job_assistant_readonly"' in client
    assert 'APIRouter(prefix="/sessions"' in session_routes
    for path in {
        'request("/memories")',
        '/memories/${encodeURIComponent(memoryId)}/confirm',
        '/memories/${encodeURIComponent(memoryId)}/reject',
        '/memories/${encodeURIComponent(memoryId)}/supersede',
        'request("/skills")',
        '/skills/${encodeURIComponent(skillName)}/versions',
        '/skill-versions/${encodeURIComponent(versionId)}',
        '/sessions/${encodeURIComponent(sessionId)}/context',
    }:
        assert path in client


def test_javascript_does_not_render_untrusted_markup() -> None:
    for path in EXTENSION.glob("*.js"):
        source = path.read_text(encoding="utf-8")
        assert "innerHTML" not in source, path.name


def test_requirement_ui_uses_v3_metadata_and_safe_details() -> None:
    panel = (EXTENSION / "sidepanel.js").read_text(encoding="utf-8")
    page = (EXTENSION / "sidepanel.html").read_text(encoding="utf-8")

    assert 'id="confirmation-list"' in page
    assert "Needs your confirmation" in page
    assert "requirement.display_name" in panel
    assert "requirement.category" in panel
    assert "requirement.level || match.requirement_level" in panel
    assert "requirement.minimum_years" in panel
    assert 'metadata.join(" \\u2022 ")' in panel
    assert "Evidence strength:" in panel
    assert '["matched", "partial"].includes(match.match_status)' in panel
    assert 'document.createElement("details")' in panel
    assert 'appendTextElement(details, "summary", "Evidence and reasoning")' in panel
    assert "match.match_reason" in panel
    assert "match.resume_evidence" in panel
    assert "innerHTML" not in panel
    assert "% confidence" not in panel


def test_extraction_and_input_limits_are_explicit() -> None:
    extractor = (EXTENSION / "extractor.js").read_text(encoding="utf-8")
    panel = (EXTENSION / "sidepanel.js").read_text(encoding="utf-8")

    selection_position = extractor.index("window.getSelection")
    container_position = extractor.index("const selectors")
    main_position = extractor.index('document.querySelector("main")')
    body_position = extractor.index("document.body")
    assert selection_position < container_position < main_position < body_position
    assert "MAX_TEXT_LENGTH = 50_000" in panel
    assert "chrome.scripting.executeScript" in panel


def test_service_worker_records_action_tab_before_opening_panel() -> None:
    worker = (EXTENSION / "service-worker.js").read_text(encoding="utf-8")

    assert 'ACTIVE_JOB_TAB_KEY = "jobAgentActiveTab"' in worker
    assert "openPanelOnActionClick: false" in worker
    assert "chrome.action.onClicked.addListener" in worker
    assert "chrome.storage.session.set" in worker
    assert "chrome.sidePanel.open" in worker
    assert "addListener(async (tab)" not in worker
    handler = worker[worker.index("chrome.action.onClicked.addListener"):]
    open_call = handler.index("chrome.sidePanel.open")
    assert "await " not in handler[:open_call]
    assert "Promise.allSettled([saveTarget, openPanel])" in worker


def test_side_panel_extracts_only_from_recorded_action_tab() -> None:
    panel = (EXTENSION / "sidepanel.js").read_text(encoding="utf-8")

    page = (EXTENSION / "sidepanel.html").read_text(encoding="utf-8")

    assert 'ACTIVE_JOB_TAB_KEY = "jobAgentActiveTab"' in panel
    assert "chrome.storage.session.get" in panel
    assert "activeTab?.id !== target.tabId" in panel
    assert "target: { tabId: target.tabId }" in panel
    assert "permissions.request" not in panel
    assert "site-permission-button" not in page


def test_extractor_is_self_contained_for_script_injection() -> None:
    extractor = (EXTENSION / "extractor.js").read_text(encoding="utf-8")
    function_start = extractor.index("export function extractJobDescriptionFromPage()")

    assert "const clean =" in extractor[function_start:]
    assert "const selectors =" in extractor[function_start:]
    assert '"[data-job-description]"' in extractor[function_start:]


def test_session_ui_has_required_controls_and_statuses() -> None:
    page = (EXTENSION / "sidepanel.html").read_text(encoding="utf-8")
    panel = (EXTENSION / "sidepanel.js").read_text(encoding="utf-8")

    for element_id in {
        "analysis-panel", "assistant-panel", "new-session-button",
        "send-session-button", "cancel-session-button", "recover-session-button",
        "ask-run-button", "assistant-messages", "tool-approvals",
    }:
        assert f'id="{element_id}"' in page
    for status in {
        "active", "running", "awaiting_user", "awaiting_tool_approval",
        "completed", "failed", "cancelled", "timed_out",
    }:
        assert f'{status}:' in panel
    assert 'currentSession.recovery_available !== true' in panel
    assert 'crypto.randomUUID()' in panel
    assert "MAX_SESSION_MESSAGE_LENGTH = 20_000" in panel
    assert "analysisBusy" in panel and "sessionBusy" in panel


def test_session_storage_keeps_only_identifier_and_restores_from_backend() -> None:
    panel = (EXTENSION / "sidepanel.js").read_text(encoding="utf-8")

    assert 'ACTIVE_SESSION_KEY = "jobAgentActiveSessionId"' in panel
    assert "chrome.storage.local.set({ [ACTIVE_SESSION_KEY]: currentSession.session_id })" in panel
    assert "getSession(sessionId)" in panel
    assert "getSessionMessages(currentSession.session_id)" in panel
    assert "error.status === 404" in panel
    assert "chrome.storage.local.remove(ACTIVE_SESSION_KEY)" in panel
    assert "Preserve the ID during temporary backend or network failures" in panel
    assert "sessionState" not in panel
    assert "cachedMessages" not in panel
    assert "listSessions(50)" in panel
    assert "Choose a previous conversation" in panel


def test_session_conflicts_refresh_without_automatic_resend() -> None:
    panel = (EXTENSION / "sidepanel.js").read_text(encoding="utf-8")
    handler = panel[panel.index("async function handleConcurrency"):panel.index("async function sendMessage")]

    assert "error.status !== 409" in handler
    assert "await refreshSession()" in handler
    assert "your message was not resent" in handler
    assert "sendSessionMessage" not in handler


def test_restored_running_session_polling_is_bounded() -> None:
    panel = (EXTENSION / "sidepanel.js").read_text(encoding="utf-8")
    polling = panel[panel.index("async function pollRestoredSession"):panel.index("async function restoreSession")]

    assert "SESSION_POLL_TIMEOUT_MS" in polling
    assert 'currentSession?.status === "running"' in polling
    assert "await refreshSession()" in polling
    assert "startSession" not in polling
    assert "createSession" not in polling


def test_session_rendering_uses_only_public_roles_and_safe_dom() -> None:
    panel = (EXTENSION / "sidepanel.js").read_text(encoding="utf-8")

    assert '["user", "assistant"].includes(item?.role)' in panel
    assert 'JSON.stringify(approval.arguments || {}, null, 2)' in panel
    assert "Arguments (redacted by server)" in panel
    assert "approval-arguments" in panel
    assert "innerHTML" not in panel


def test_session_history_ui_is_backend_backed_and_safe() -> None:
    panel = (EXTENSION / "sidepanel.js").read_text(encoding="utf-8")
    page = (EXTENSION / "sidepanel.html").read_text(encoding="utf-8")

    assert 'id="session-history"' in page
    assert 'id="refresh-sessions-button"' in page
    assert "async function loadSessionHistory()" in panel
    assert "async function openSession(sessionId)" in panel
    assert "historyLabel(session)" in panel
    assert "option.value = session.session_id" in panel
    assert "chrome.storage.local.set({ [ACTIVE_SESSION_KEY]: sessionId })" in panel
    storage_write = panel.index("chrome.storage.local.set({ [ACTIVE_SESSION_KEY]: sessionId })")
    assert "messages:" not in panel[storage_write:storage_write + 200]


def test_context_management_ui_is_safe_and_backend_backed() -> None:
    panel = (EXTENSION / "sidepanel.js").read_text(encoding="utf-8")
    page = (EXTENSION / "sidepanel.html").read_text(encoding="utf-8")

    for element_id in {
        "context-tab", "context-panel", "used-context", "memory-list",
        "skill-list", "create-memory-button", "refresh-context-button",
    }:
        assert f'id="{element_id}"' in page
    assert "Used This Turn" in page
    assert "Memory Manager" in page
    assert "Skill Manager" in page
    assert "async function refreshContextSummary" in panel
    assert "async function refreshMemories" in panel
    assert "async function refreshSkills" in panel
    assert "Replace confirmed" in panel
    assert "Inspect instructions" in panel
    assert "refreshContextPanel()" in panel
    assert "innerHTML" not in panel
    assert "chrome.storage.local" not in panel[
        panel.index("function renderUsedContext"):panel.index("elements.healthButton")
    ]


def test_context_conflicts_refresh_without_automatic_retry() -> None:
    panel = (EXTENSION / "sidepanel.js").read_text(encoding="utf-8")
    memory_handler = panel[
        panel.index("async function mutateMemory"):panel.index("function renderMemories")
    ]
    skill_handler = panel[
        panel.index("async function mutateSkill"):panel.index("function renderSkillVersions")
    ]
    assert "error.status === 409" in memory_handler
    assert "await refreshMemories()" in memory_handler
    assert "await operation()" in memory_handler
    assert memory_handler.count("await operation()") == 1
    assert "error.status === 409" in skill_handler
    assert "await refreshSkills()" in skill_handler
    assert "await action(version.version_id, version.version)" in skill_handler
