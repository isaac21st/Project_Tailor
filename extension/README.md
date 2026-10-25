# Tailor Assist (Chrome + Edge extension)

Fills job applications from your own profile, drafts free-text answers with a local model, and **never submits**.
One code base (Manifest V3) works in both Chrome and Edge.

## Install (once, in each browser)
1. Chrome: open `chrome://extensions`. Edge: open `edge://extensions`.
2. Turn on **Developer mode**, click **Load unpacked**, and choose this `extension` folder.
3. Start the local server (see below), then open the extension's **Options** and paste the access token.

## Run the local server
```
<venv>\Scripts\python scripts\run_server.py            # stub model (no RAM needed; free-text drafts are placeholders)
<venv>\Scripts\python scripts\run_server.py --model ollama   # real drafts from local Qwen3-8B (needs ~6 GB free RAM)
```
It listens on `127.0.0.1:8765` only. The token is in `<data_dir>\server_token.txt` (or add `--show-token`).

## Use
On a Greenhouse, Lever or Workday application page a small panel appears. Click **Fill this page**.
- Green outline: filled from your profile. Amber: model draft to review and edit. Red: needs you.
- Work authorization, sponsorship, EEO/demographics, salary, legal attestations, IDs, passwords and CAPTCHAs are
  never filled, only flagged. You answer them and click Submit yourself.
- A sound and a notification tell you when something needs you (it repeats each minute until you click on the page).
  Upload your own clips (30 seconds max) in Options.
- On any other careers page, click the toolbar icon, then **Fill this page**.

## What it can and cannot do
It can type into text fields, choose dropdown options, tick checkboxes and radios, and attach your resume PDF.
It never clicks Submit or Apply; `tests/test_extension_static.py` and the end-to-end tests enforce this. If you turn
on "Go to the next step for me", it presses a form's own Next / Continue button between steps and stops at the Review
page (and never on school job boards, whose Continue sends your documents). It also fills custom dropdowns, Workday's
multi-step forms and boxes inside web components (SmartRecruiters), and attaches the tailored resume and cover letter.
