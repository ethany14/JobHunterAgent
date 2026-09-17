# Job Agent Chrome Extension MVP

This Manifest V3 extension opens Job Agent in Chrome's Side Panel. It extracts text only when the user clicks **Extract Job Description**, leaves the result editable, and sends it to the local FastAPI service only after the user clicks **Analyze and Tailor Resume**.

## Local installation

1. From the project root, start the FastAPI service:

   ```powershell
   python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
   ```

2. Open `chrome://extensions` in Chrome.
3. Enable **Developer mode**.
4. Click **Load unpacked**.
5. Select this project's `chrome_extension` folder.
6. Open a test job listing in a normal web page.
7. Click the **Job Agent** extension icon while the job page is active. The service worker records that tab and opens its Side Panel.

Chrome blocks script injection on internal pages such as `chrome://extensions` and on some protected pages. Open a regular HTTP or HTTPS job page, or paste the job description manually.

### Tab-selection troubleshooting

The extension records the tab from the toolbar action that opened the Side Panel. If extraction reports that the tab changed or page access is no longer active:

1. Keep the job page selected.
2. Click the **Job Agent** toolbar icon again.
3. Click **Extract Job Description** in the newly opened Side Panel.

The recorded tab identity is stored in `chrome.storage.session`; it is used only to associate the panel with the user-selected page and is not sent to the backend.

The service worker starts `sidePanel.open()` directly inside the toolbar click handler, before awaiting any asynchronous work. Chrome requires that call to remain within the original user gesture.

## How it works

Extraction uses this order:

1. Text currently selected by the user.
2. A small set of common job-description containers.
3. The page's `main` text.
4. The document body text.

The extension never sends extracted text automatically. It places the text in an editable field first. Inputs over 50,000 characters are shown but rejected before submission so the user can select or edit down to the relevant content.

The API base URL is fixed at `http://localhost:8000`. The extension calls:

- `GET /health`
- `POST /runs` with `resume_text` and `job_description`
- `GET /runs/{run_id}`
- `POST /runs/{run_id}/review` with `approved` and `feedback`

It handles `running`, `awaiting_review`, `revising`, `approved`, and `failed` run states. A rejection requires feedback. The tailored resume can be copied after the run reaches a reviewable result.

## Resume storage and privacy

Saving the resume is optional. When enabled, the resume is stored in `chrome.storage.local` in the current Chrome profile. **Clear saved resume** removes that stored copy and clears the field. The extension does not write resume text, job-description text, API keys, or generated results to the console.

The local FastAPI service and its SQLite database have their own storage behavior. Clearing the browser copy does not delete data already sent to the backend.

## Permissions

- `activeTab`: grants temporary access to the tab where the user clicked the extension action.
- `scripting`: runs the text-only extraction function after the user clicks the extraction button.
- `storage`: stores a resume locally when the user opts in.
- `sidePanel`: displays the extension interface in Chrome's Side Panel.
- `http://localhost:8000/*`: allows requests only to the local Job Agent API.

The extension does not request optional website host patterns, automatic access to every page, cookies, history, downloads, network interception, or the `tabs` permission.

## Current limits

- It supports only the current page opened by the user.
- Extraction is generic and may include unrelated page text; review it before analysis.
- There are no site-specific adapters, batch scraping, automatic applications, login bypasses, or file uploads.
- The backend must be running locally, and the in-progress request must complete within 60 seconds.
- Resume persistence is local to the Chrome profile and is not encrypted by the extension.
