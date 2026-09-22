import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "chrome_extension"


def test_manifest_is_minimal_and_valid() -> None:
    manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 3
    assert manifest["minimum_chrome_version"] == "116"
    assert set(manifest["permissions"]) == {"activeTab", "scripting", "storage", "sidePanel"}
    assert manifest["host_permissions"] == ["http://localhost:8000/*"]
    assert manifest["background"]["type"] == "module"
    assert "<all_urls>" not in json.dumps(manifest)
    assert not ({"cookies", "history", "webRequest", "downloads", "tabs"} & set(manifest["permissions"]))


def test_required_extension_files_exist() -> None:
    for name in {"manifest.json", "service-worker.js", "sidepanel.html", "sidepanel.css",
                 "sidepanel.js", "api-client.js", "extractor.js", "README.md"}:
        assert (EXTENSION / name).is_file(), name


def test_extension_is_a_focused_job_lens() -> None:
    page = (EXTENSION / "sidepanel.html").read_text(encoding="utf-8")
    script = (EXTENSION / "sidepanel.js").read_text(encoding="utf-8")
    client = (EXTENSION / "api-client.js").read_text(encoding="utf-8")
    for element in {"extract-button", "job-title", "job-company", "job-description",
                    "analyze-button", "match-score", "suggestion-list", "open-web-button",
                    "save-job-button", "open-application-button"}:
        assert f'id="{element}"' in page
    assert 'request("/api/workspaces"' in client
    assert '/analyze-with-resume' in client
    assert 'request("/health"' in client
    assert 'http://localhost:8000/app' in script
    assert "source_url: currentExtraction?.source_url" in script
    assert "?application=" in script
    for removed in {"resume-text", "assistant-panel", "memory-list", "skill-list",
                    "application-pack", "mock-interview"}:
        assert removed not in page


def test_extraction_remains_user_initiated_and_editable() -> None:
    page = (EXTENSION / "sidepanel.html").read_text(encoding="utf-8")
    script = (EXTENSION / "sidepanel.js").read_text(encoding="utf-8")
    extractor = (EXTENSION / "extractor.js").read_text(encoding="utf-8")
    assert "chrome.scripting.executeScript" in script
    assert 'elements.extract.addEventListener("click", extract)' in script
    assert "Nothing is sent until you select Analyze" in page
    assert "<textarea" in page and 'id="job-description"' in page
    assert "MAX_TEXT_LENGTH = 50_000" in script
    assert extractor.index("window.getSelection") < extractor.index("const selectors")
    assert "await extract();" in script
    assert "chrome.storage.onChanged.addListener" in script
    assert '"#job-details"' in extractor
    assert "bestContainerText" in extractor
    assert "isVisible" in extractor
    assert "linkedin\\.com" in extractor
    assert "jobDescriptionSlice" in extractor
    assert "关于职位" in extractor
    assert "职位发布中说明的福利" in extractor
    assert '".jobs-search__job-details--container #job-details"' in extractor


def test_extension_renders_untrusted_content_with_safe_dom_only() -> None:
    for path in EXTENSION.glob("*.js"):
        source = path.read_text(encoding="utf-8")
        assert "innerHTML" not in source, path.name
        assert "document.write" not in source, path.name
    panel = (EXTENSION / "sidepanel.js").read_text(encoding="utf-8")
    assert "textContent" in panel
    assert "replaceChildren" in panel


def test_service_worker_preserves_action_gesture_for_side_panel() -> None:
    worker = (EXTENSION / "service-worker.js").read_text(encoding="utf-8")
    assert 'ACTIVE_JOB_TAB_KEY = "jobAgentActiveTab"' in worker
    assert "openPanelOnActionClick: false" in worker
    assert "chrome.action.onClicked.addListener" in worker
    assert "chrome.sidePanel.open" in worker
    assert "chrome.scripting.executeScript" in worker
    assert 'import { extractJobDescriptionFromPage } from "./extractor.js"' in worker
    assert 'ACTIVE_JOB_EXTRACTION_KEY = "jobAgentActiveExtraction"' in worker
    handler = worker[worker.index("chrome.action.onClicked.addListener"):]
    assert "await " not in handler[:handler.index("chrome.sidePanel.open")]


def test_interactions_have_hover_and_press_feedback() -> None:
    styles = (EXTENSION / "sidepanel.css").read_text(encoding="utf-8")
    assert ":hover" in styles
    assert ":active" in styles
    assert "transition:" in styles
    assert "transform:" in styles
    assert "Monochrome Side Panel theme" in styles
    assert "--green:#111" in styles
    assert "conic-gradient(#111 var(--score)" in styles
