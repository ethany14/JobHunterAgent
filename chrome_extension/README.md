# Northstar Job Lens

The Chrome extension is intentionally small. It reads the job description from the page the user explicitly selected, leaves the extracted text editable, and asks the local JobHunterAgent API for a quick comparison with the default PDF resume stored by the web app.

It shows a match score, matched/partial/missing counts, focused suggestions, and optional requirement details. Resume upload, saved jobs, conversations, materials, evidence, interviews, Memory, Skills, and workflow diagnostics live in the full web workspace at `http://localhost:8000/app`.

## Run locally

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m uvicorn api.main:app --host 127.0.0.1 --port 8000
```

Open `http://localhost:8000/app`, go to **Settings**, and upload a text-based PDF resume. Then:

1. Open `chrome://extensions`.
2. Enable **Developer mode**.
3. Choose **Load unpacked** and select `chrome_extension`.
4. Open a normal HTTP(S) job page.
5. Click the Northstar Job Lens toolbar icon.
6. Select **Extract page**, review the title, company, and description, then select **Analyze fit**.

The API URL is fixed to `http://localhost:8000`. The extension calls only `GET /health` and `POST /api/quick-analysis`.

## Privacy and permissions

- `activeTab` grants temporary access to the page where the toolbar icon was clicked.
- `scripting` runs the text-only extractor after **Extract page** is clicked.
- `storage` remembers only the selected tab in session storage.
- `sidePanel` displays the UI.
- `http://localhost:8000/*` allows the two local API requests.

The extension does not store resume text, job descriptions, analysis results, messages, tool output, or prompts. It does not request broad website access, cookies, history, downloads, `webRequest`, or the `tabs` permission. It never sends extracted page text automatically.

PDFs containing only scanned images are rejected because OCR is outside this version.
