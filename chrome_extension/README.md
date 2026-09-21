# Job Agent Chrome Extension MVP

The Context tab now includes a Career Evidence manager. Enter a statement in your own words, choose a category, and save it as a candidate. Confirm it separately before it can appear in model context. Existing resume evidence can only be imported by the backend from the original resume; the panel cannot claim resume provenance from typed text. The list shows confirmed and pending items by default, supports search/category filtering, and lets you inspect immutable version history. When a saved Application is open, linked evidence is labeled. Statements and quotes are stored by the local SQLite backend; the extension does not cache them.

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
- `POST /sessions` with the fixed `job_assistant_readonly` capability profile
- `GET /sessions` for safe recent-conversation summaries
- `GET /sessions/{session_id}` and `/messages`
- `POST /sessions/{session_id}/messages`
- `POST /sessions/{session_id}/tool-calls/{tool_call_id}/approve` or `/reject`
- `POST /sessions/{session_id}/cancel` and `/recover`
- Memory management under `/memories`
- Skill discovery and lifecycle management under `/skills` and `/skill-versions`
- `GET /sessions/{session_id}/context` for a privacy-filtered snapshot summary
- `POST /api/workspaces` to save or reopen a Job Workspace
- Workspace list, detail, status, analysis, event, and artifact routes under `/api/applications`

It handles `running`, `awaiting_review`, `revising`, `approved`, and `failed` run states. A rejection requires feedback. The tailored resume can be copied after the run reaches a reviewable result.

The Assistant tab handles `active`, `running`, `awaiting_user`,
`awaiting_tool_approval`, `completed`, `failed`, `cancelled`, and `timed_out`
sessions. **Ask Agent About This Run** starts a session linked to the displayed
run. Tool approval cards display only metadata and arguments already redacted by
the server; arguments cannot be edited in the extension. A restored running
session is polled for a bounded period, and polling never creates a replacement
session. Closing the Side Panel does not cancel backend work.

The **Previous conversations** selector reads recent Session summaries from the
local backend. Selecting one restores its public user/assistant messages, even
after another Session has been created. The browser does not keep a second copy
of the history.

The **Context** tab has three sections. **Used This Turn** shows only snapshot
IDs, lifecycle status, Skill metadata, Memory display metadata, effective tool
names, timestamps, and estimated tokens. **Memory Manager** creates candidates
that require a separate confirmation and uses an explicit **Replace confirmed**
action for same-key changes. Delete is a backend soft delete. **Skill Manager**
shows safe instruction snapshots and separates approval, activation, rejection,
and retirement. Generated Skills also require a server-recorded passing
evaluation before activation.

## Resume storage and privacy

Saving the resume is optional. When enabled, the resume is stored in `chrome.storage.local` in the current Chrome profile. **Clear saved resume** removes that stored copy and clears the field. The extension does not write resume text, job-description text, API keys, or generated results to the console.

For the Assistant, the extension stores only the active session ID in
`chrome.storage.local`. Messages, tool outputs, prompts, approval data, and
session state are restored from the backend and are not cached in browser
storage. A temporary backend failure keeps the saved session ID; an explicit
`404` removes it.

Memory, Skill, and context responses are fetched when the Context tab opens and
after relevant changes. They are not stored in `chrome.storage`. The context
summary never contains system prompts, assembled model context, raw source
evidence, raw tool results, owner IDs, or task-private messages.

The local FastAPI service and its SQLite database have their own storage behavior. Clearing the browser copy does not delete data already sent to the backend.

## Permissions

- `activeTab`: grants temporary access to the tab where the user clicked the extension action.
- `scripting`: runs the text-only extraction function after the user clicks the extraction button.
- `storage`: stores a resume locally when the user opts in.
- `sidePanel`: displays the extension interface in Chrome's Side Panel.
- `http://localhost:8000/*`: allows requests only to the local Job Agent API.

The extension does not request optional website host patterns, automatic access to every page, cookies, history, downloads, network interception, or the `tabs` permission.

## Job Workspace manual test

1. Start FastAPI and reload the unpacked extension.
2. Open a supported HTTP(S) job page and click the extension icon.
3. Extract the JD and edit it in the Analysis tab.
4. Click **Save Job**. Confirm an Application ID appears and no model call starts.
5. Open **Jobs**, refresh, and open the saved Application.
6. Enter or restore a resume and click **Analyze saved job**.
7. Confirm human review is reached and three artifact types appear.
8. Restart FastAPI, reopen the extension, and restore the same Workspace.
9. Click **Open Assistant** and confirm it identifies this Application only.

Chrome stores only the current and last-opened Application IDs. Workspace data
remains in SQLite. Repeated saves reopen the active Application. Rejected,
withdrawn, and archived Applications are not reopened; a later save creates a
new Application for the existing Job.

## Current limits

- It supports only the current page opened by the user.
- Extraction is generic and may include unrelated page text; review it before analysis.
- There are no site-specific adapters, batch scraping, automatic applications, login bypasses, or file uploads.
- The backend must be running locally, and the in-progress request must complete within 60 seconds.
- Resume persistence is local to the Chrome profile and is not encrypted by the extension.
- Session access currently follows the backend's trusted local single-user model;
  there is no account or tenant authorization boundary.
# Manually test Improve Evidence

Start the local FastAPI backend after `alembic upgrade head` and reload the
unpacked extension at `chrome://extensions`. Save a Job in Workspace, run its
analysis so the current Job Snapshot has a match report, then open that
Application's Workspace detail and choose **Start or resume interview** under
**Improve Evidence**. Answer one question with a concrete fact; inspect the
pending candidate and its original answer before clicking **Confirm exactly**
or **Edit and confirm**. Try **I don't have this experience** on a separate
Application: it should create only an Application-specific confirmed gap.

To test recovery manually, start another interview, leave it waiting on a
question, close the Side Panel, restart FastAPI, reopen the same Application,
and verify that the same question and interview progress return. Submit an
answer once. If a model request fails, use **Recover interview**; the saved
answer should not be duplicated. This local-only flow has no user login and
does not grant the interviewer any MCP tools.

## Application Pack manual check

After confirming one Evidence Candidate and analyzing a saved Application,
open that Workspace and select **Generate new Pack**. Generate the resume,
cover letter, and an answer to a question you paste. Inspect each item's
verification, evidence, and immutable version history. Sponsorship and
demographic questions must request a manual answer. Edit a cited block to
create a new verified version. Restart FastAPI, reopen the same Workspace,
and verify that the Pack and its review state return from SQLite. Approve
verified items only. Pack content is fetched from the backend and never
cached in Chrome storage.

## Task Activity diagnostics

When a saved Application has server-created Agent tasks, its Workspace shows
them under **Task Activity**. Expand a task to see its role, status, dependency
IDs, attempt count, elapsed time, safe failure code, and bounded result summary.
The panel offers Cancel and, for a failed task with remaining attempts, Retry.
It does not show child conversations, full context snapshots, prompts, secrets,
or artifact contents. The Job workflow registers one fixed, opt-in business
plan; the browser cannot supply worker code or dependencies. This remains
local single-user infrastructure.

## Multi-Agent Job workflow manual check

After `alembic upgrade head`, start FastAPI and reload the extension. Open an
analyzed saved Application with confirmed Career Evidence. In Workspace choose
**Multi-Agent** and select resume, cover letter, and one ordinary application
question. Start the execution and inspect progress: Candidate and Job analyses
should become ready together; matching waits for both; writing starts only
after evidence freezes. Each generated artifact must be verified before the
Pack appears for review. With the interview option enabled, complete or skip
an evidence question; a paused interview survives a backend restart. A
restricted question, such as sponsorship, stays manual. The same Pack review
and explicit export controls used by Standard mode apply to verified output.
The Multi-Agent option is experimental; Standard remains the default.

## Mock Interview manual check

Open a saved Application with a current approved Pack. In Workspace, choose a
mode, difficulty and 3–12 main questions, then start **Mock Interview**. Give
an incomplete answer, inspect the coaching and any bounded follow-up, and
close the Side Panel. Reopen the same Application to resume the pending
question. **Save and continue later** simply leaves the persisted interview
paused; it does not submit the draft answer. After completion, expand the
report and review any Evidence Candidate. Confirming it is a separate user
action and may make the old Pack stale. To test restart recovery, stop and
restart FastAPI while awaiting an answer, then reopen the Application. The
panel fetches the interview from the server; it does not store the answer or
report in Chrome storage. A failed, planning or evaluating interview offers
**Recover**. All question, feedback and report text is rendered as inert text.
