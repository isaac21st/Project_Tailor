# Project Tailor

A job-application assistant that runs on your own computer. It tailors a one-page resume to each posting, fills
application forms in your own browser, finds roles that fit you, and helps you find people to ask for a referral.
**It never submits anything:** you review every application and click Submit yourself.

- **Tailor Desk** (`http://127.0.0.1:8765/desk`): save postings (PDFs, links, or straight from a job page), see their
  deadlines and requirements, and get a tailored resume and cover letter for each one.
- **Find roles**: searches companies' public job boards (Greenhouse, Lever, Ashby, Workday, Google Careers, BCG and a
  public new-grad list) every morning and ranks what fits your resume and preferences.
- **Tailor Assist** (Chrome extension, `extension/`): fills application forms from your profile, attaches the tailored
  resume and cover letter, drafts answers to open questions for you to edit, steps through multi-page forms and stops
  at the Review page.
- **Tailor Network** (`http://127.0.0.1:8765/network`): for each company you apply to, searches for alumni, past
  coworkers and people in the same city, your own LinkedIn connections (from LinkedIn's data export), message drafts,
  follow-up reminders and a daily plan. It only gives you links and drafts: you send every message.

## Your data stays yours

The project folder holds only code. Everything about you lives in a separate **data folder** (default
`~/project_tailor_data`): your resume, preferences, saved answers, postings, contacts and generated PDFs. Nothing is
uploaded anywhere; the server listens on `127.0.0.1` only and needs a token. Keep the data folder out of
iCloud / OneDrive / Dropbox and **never commit it** (the `.gitignore` blocks the usual files).

Sensitive questions (work authorization, sponsorship, demographics, disability, veteran status, salary, legal
attestations) are only ever answered from answers you saved yourself in `standing_answers.json`; otherwise they are
left for you.

## Set up (macOS)

1. Python 3.12 or 3.13, and Chrome. Then, from the project folder:
   ```
   python3 -m venv ~/project_tailor_data/venv
   ~/project_tailor_data/venv/bin/python -m pip install -r requirements.txt
   PLAYWRIGHT_BROWSERS_PATH=~/project_tailor_data/browsers ~/project_tailor_data/venv/bin/python -m playwright install chromium
   ```
   (`bash scripts/setup_mac.sh` does these steps.)
2. **Make your profile.** `~/project_tailor_data/venv/bin/python scripts/new_profile.py` copies the example person in
   `examples/profile/` into your data folder. Replace them with yourself:
   - `base_resume.json` is your resume, in the structure shown (sections EDUCATION, PROFESSIONAL EXPERIENCE,
     PROJECT EXPERIENCE, SKILLS; dates like `May 2026 - Present` or `Expected May 2027`; `**bold**` marks tools).
   - `profile_overrides.json`, `standing_answers.json` and `tailoring_rules.json` are your preferences: see
     [PREFERENCES.md](PREFERENCES.md).
3. `~/project_tailor_data/venv/bin/python scripts/check_setup.py` says what, if anything, is not ready (it builds a
   real one-page resume from your file).
4. Optional, for drafted answers and cover letters: install [Ollama](https://ollama.com) and `ollama pull qwen3:8b`.
5. Start the server: `~/project_tailor_data/venv/bin/python scripts/run_server.py --model ollama` (or `--model stub`
   without Ollama). `bash scripts/autostart.sh install` keeps it running at login instead.
6. In Chrome open `chrome://extensions`, turn on Developer mode, **Load unpacked** the `extension` folder, open the
   extension's Options and paste the token from `~/project_tailor_data/server_token.txt`.
7. Open `http://127.0.0.1:8765/desk`. The **My info** tab holds the form answers your resume does not show.

## Tests

```
~/project_tailor_data/venv/bin/python -m unittest discover -s tests
```
The end-to-end tests drive a real headless Chrome with the extension on local test pages. Tests that check tailoring
against the owner's own resume skip themselves when there is none.

## How the pieces fit

- `tailor/resume/`: the structured resume, truth-checked tailoring (`auto.py`, `rewrite.py`), your own tailoring rules
  (`rules.py`), keywords and ATS score, and the PDF renderer (a fixed HTML/CSS template with the bundled Tinos font,
  so every page lays out the same).
- `tailor/fill/`: your profile, the form-field mapper, sensitive-question rules, answer and cover-letter drafting.
- `tailor/jobs/`: posting cleanup and facts, and the role search (`search.py`).
- `tailor/network/`: referral networking. `tailor/server/`: the local API and the Desk and Network pages.
- `extension/`: the Chrome extension (Manifest V3). `scripts/`: command-line tools. `examples/`: the example person.

Tailoring only chooses, orders and trims what your resume already says, plus wordings you approved; every result is
checked against your base resume (no new facts, numbers or skills), or the untailored resume is used instead.

## Sharing a copy

`python scripts/export_public.py <folder>` makes a copy with none of your personal information: it copies only the
code, tests, examples and docs, replaces the personal words you list privately in `<data_dir>/public_export_map.json`,
and scans the result for anything left (your name, contact details, address, resume phrases). It stops with a report
if it finds any.

## Working together

- Change code, tests or docs; never your data folder. Put anything personal in your own data folder instead (see
  [PREFERENCES.md](PREFERENCES.md)): if a feature needs a fact about you, it should read it from there.
- Run the tests before sharing a change. New behavior gets a test that uses a made-up person, like the ones in
  `tests/` and `examples/`.
- Share changes as pull requests on GitHub; `git status` before each commit is a quick check that no resume, PDF,
  `config.toml` or data file is included.

## Fonts

`tailor/resume/fonts/` bundles Tinos (metric-compatible with Times New Roman) under the SIL Open Font License 1.1.
