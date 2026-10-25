# Your preferences

Everything personal lives in your data folder (default `~/project_tailor_data`), never in the project. These files in
`<data_dir>/profile/` set how Project Tailor works for you. `python scripts/new_profile.py` starts you with the example
person's copies; edit them, or use the Desk and Network pages, which write to the same files.

## `base_resume.json`: your resume (required)

The one source of truth. Forms are filled from it, tailoring only chooses, orders and trims what it says, and every
tailored resume is checked against it. See `examples/profile/base_resume.json` for the structure:

- `header`: your name and contact items (an email item has a `mailto:` url; a LinkedIn or portfolio link has its url).
- `sections`: `EDUCATION`, `PROFESSIONAL EXPERIENCE`, a `... PROJECT EXPERIENCE` section, and `SKILLS` (labelled lines).
- Each entry has a `title` (school, employer or project), `location`, and `roles` with `title`, `dates`, `bullets`.
  Dates read like `May 2026 - Present`, `June 2025 - September 2025` or `Expected May 2027`.
- Write tools in `**bold**`: the tailoring treats them as names that must stay exactly as written.

## `profile_overrides.json`: facts and choices your resume does not show

| Key | What it does |
|---|---|
| `extras` | Answers forms ask for: `city`, `state`, `country`, `address`, `postal_code`, `phone_type`, `how_heard`, `location_preferences` (your cities in order; the search and the networking use them), `available_start` (`YYYY-MM-DD`), `preferred_name`, `github_url`, `gpa`, `education_gpas` ({"words of a school name": "3.4"}), `languages`, `high_school_year`, `interviewed_at`, `relatives_at`, `government_official`, `conflict_of_interest`. Blank means the form question is left to you. The Desk's **My info** tab edits these. |
| `education_order` | Words of school names in the order forms should list your schools. |
| `education_starts` | `{"school as on your resume": "YYYY-MM"}`: start months, which resumes leave out. |
| `fields_of_study` | `{"words of a school name": "Field"}`: the field a form's list should get for that school. |
| `name_overrides` | How an ALL-CAPS resume heading should read on forms (`{"ACME RENTAL GROUP": "Acme Rental Group"}`). |
| `confirmed_skills` | Skills you confirmed you really have; tailoring may add them where a posting asks. Set from the Desk. |
| `resume_summary` | `false` turns off the one-line summary at the top of tailored resumes. |
| `network_ties` | Extra ties for networking (clubs, programs, how a school should be named): `[{"label", "kind" (school, program, club, employer), "search", "weight" (0-3), "words" (how it shows on a profile), "short", "within" (a broader tie it already covers), "alumni_page" (the school's LinkedIn page name, for its alumni tool)}]`. The Network page's **Your ties** tab edits these. |
| `network_profile_ties` | `false` uses only `network_ties` instead of also making ties from your resume's schools and employers. |
| `network_weights`, `network_off` | Your weight for a tie, and ties switched off. Set from the Network page. |

## `standing_answers.json`: questions you answer the same way everywhere

Tailor answers sensitive questions **only** from here; an empty value means it always leaves that question to you.

| Key | Example |
|---|---|
| `work_authorization_us`, `sponsorship_us`, `us_citizen` | `"Yes"` / `"No"` |
| `us_answers_for_us_roles` | `true`: use the US answers on US roles |
| `background_check_ok` | `"Yes"` |
| `relocation_ok_cities` | `["San Francisco", "New York"]` |
| `in_person_max_percent` | `100` |
| `gender`, `pronouns`, `transgender`, `hispanic_latino`, `race`, `veteran`, `disability`, `disability_detail` | Your own wording, or a form's "decline to answer" option. Leave empty to answer each form yourself. |

## `answer_sheet.json`: your answers to specific questions (optional)

`{"question as the form words it": {...}}`. The Desk and the extension add to it when you save an answer; the
answer library (`answer_library.json`) keeps your written answers so a similar question starts from your own words.

## `tailoring_rules.json`: your own tailoring rules (optional)

All about your resume, so none of it is built in. See the top of `tailor/resume/rules.py` and the example file.

| Key | What it does |
|---|---|
| `protect` | Words of bullets that must stay on every tailored resume. |
| `approved_rewrites` | `[["old text", "new text"], ...]`: re-wordings you reviewed, applied wherever the old text appears. |
| `reframes` | `[{"when": "regex on the posting", "old": "...", "new": "..."}]`: use the posting's own word for something you did. |
| `shortenings` | Extra ways the one-page fitter may save space: replace `find` with `replace` in your bullets or skills lines. |
| `education_first` | `{"degree": "Master of Science", "label": "MS first", "reason": "my order"}`: the degree you list first. |

## Elsewhere in the data folder

- `search/prefs.json`: the role search (role types, cities, experience limit, companies, daily time, auto-prepare).
  Edit it on the Desk's **Find roles** tab.
- `network/connections.json`: your LinkedIn connections, if you import LinkedIn's `Connections.csv` on the Network page.
- `network/autopilot.json`: the daily referral plan (time, how many people to line up, Mac notification).
- `public_export_map.json`: only for `scripts/export_public.py`: personal words to replace when you share a copy.
