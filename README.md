# Kings Research Outreach Brain

A Python "thinking brain" that researches each prospect and decides which of the 16 Kings Research offering pillars to pitch. It then writes a 4-email cold sequence that follows the framework, and fills your Google Sheet:

`Signal | E1 Subject | E1 Body | E2 Body | E3 Body | E4 Body | Status` (plus `Dossier` and `Hooks`, if your tab has those columns)

It runs free on Vercel Hobby, the Gemini API free tier, Google News RSS and the Google Sheets API.

## Project structure

```
main.py                 FastAPI app (Vercel entrypoint) and the API routes
vercel.json             function timeout (300 s)
requirements.txt        Python dependencies
.python-version         Python 3.12
.env.example            every environment variable, with defaults
public/index.html       the web UI (connect, pick sheet + tab, generate)
brain/
  config.py             settings from environment variables
  offerings.py          all 16 pillars and their services, triggers, pitches, fit matrices
  persona.py            job title -> function, seniority, buyer role, fit
  research.py           Google News RSS + Gemini/Google Search fallback, 90-day hard filter
  signals.py            news -> trigger type, strength, recency; drops sensitive news
  industry.py           industry, B2B/B2C, company scale
  decision.py           scores all 16 pillars against the evidence, explains the top 3
  prompts.py            strategist, writer, editor prompts and JSON schemas
  quality.py            framework rules, grammar lint, US/UK spelling, fact checks
  pipeline.py           runs the 8 stages for one prospect
  llm.py                Gemini client: pacing, retries, quota fallback
  sheets.py             Google Sheets read/write via a service account
  text.py               name and company-name helpers
tests/test_brain.py     16 offline tests (no network, no keys)
```

## How the brain decides

Each row goes through 8 stages:

1. **Persona.** Reads the job title to work out the person's function, seniority and buyer role, and how likely they are to buy research. Examples:
   - "President" maps to an owner/decider.
   - "VP Procurement" maps to supply chain, as a decider.
   - Student-services staff are skipped as low fit.
2. **Research.**
   - Google News RSS is searched for the exact company name in the country's edition.
   - Anything older than 90 days is dropped in code, and stock-trading spam is removed.
   - If nothing is left, Gemini with Google Search looks for dated events. It uses the email domain to confirm it has the right company. Results that don't show Gemini actually searched are discarded.
3. **Signals.** Each news item is classified as a trigger type, such as acquisition, expansion, funding, new plant, product launch, regulation, tariffs, new executive or partnership. Each gets a strength score that fades with age.
   - Layoffs, lawsuits, data breaches and similar sensitive news are never used as hooks.
   - A new role for the prospect themselves is the strongest hook.
4. **Company read.** Identifies the industry (19 categories), whether the company is B2B or B2C, and a rough scale (small, mid or large).
5. **Decision.** All 16 pillars are scored with this formula:
   - **With news:** 55% signal fit, 30% role fit and 15% industry fit, plus a base weight per pillar.
   - **Without news:** 65% role fit and 35% industry fit.

   The score is then adjusted. Pillars too big for the company are penalised; for example, M&A due diligence isn't pitched to a 40-person printer. Pillars already pitched to a colleague at the same company are also penalised. The top 3 pillars go forward, each with its best 3 services and the reasons.
6. **Strategist** (AI call). Picks one pillar, one service and one signal, then builds the argument: the hook fact, the bottleneck, what's at stake, the deliverable and the Email 2 asset. The code rejects any hook that uses a signal index or number not in the evidence.
7. **Writer** (AI call). Drafts the sequence from the brief, following the framework:
   - Zero fluff, relevance over flattery, and "show me you know me".
   - A soft call to action.
   - Lowercase subjects.
   - Day 1 / 4 / 8 / 14 structure.
8. **Quality gate.**
   - **Framework checks:** word limits, a single-sentence E3, and no pleasantries or requests for calls, meetings or minutes.
   - **Grammar checks:** a/an, repeated words, capitalisation, comma splices, overlong sentences, its/it's.
   - **Spelling and style:** US or UK spelling by country, and no buzzwords.
   - **Fact checks:** no numbers that aren't in the sources, and no client claims unless they come from `KR_Proof`.

   If a hard rule fails, an editor AI call repairs the draft. The better of the two versions is kept, and the result is written to Status.

**Brain modes.** Deep mode (the default) uses 2–3 AI calls per prospect, plus 1 research call when a company has no news; research is cached for 6 hours. Fast mode merges the strategist and writer steps into one call, which roughly halves quota use.

## Deploy (about 20 minutes, once)

### 1. Put the code on GitHub

Create a new **private** repository and upload every file in this folder, keeping the folder structure. Files starting with a dot (`.python-version`, `.gitignore`, `.vercelignore`, `.env.example`) are included. Never commit a real `.env` or service-account JSON file.

### 2. Get a free Gemini API key

Go to https://aistudio.google.com/apikey and click **Create API key**. No card is needed.

### 3. Create a Google service account for the Sheet

1. Go to https://console.cloud.google.com and create a project, or pick an existing one.
2. Open **APIs & Services → Library**, search for **Google Sheets API**, and click **Enable**.
3. Open **IAM & Admin → Service Accounts → Create service account**. Name it something like `kr-outreach`, click **Done**, and skip the role assignment.
4. Open the new service account and go to **Keys → Add key → Create new key → JSON**. A JSON file downloads.
5. Copy the `client_email` from that file (it looks like `kr-outreach@your-project.iam.gserviceaccount.com`).
6. Open your prospect Google Sheet, click **Share**, paste that email, and give it **Editor** access.

If your Google Workspace blocks service-account key creation, an admin has to allow it. The org policy is `iam.disableServiceAccountKeyCreation`.

### 4. Import to Vercel

1. Go to https://vercel.com/new and import the GitHub repository. Vercel detects FastAPI from `main.py`; leave the build settings at their defaults.
2. Before clicking Deploy, open **Environment Variables** and add:

| Name | Value |
|---|---|
| `APP_PASSWORD` | A long random password. Everyone who uses the UI needs it. |
| `GEMINI_API_KEY` | Your key from step 2. |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | The entire contents of the JSON file from step 3, pasted as-is. |

   If pasting the JSON causes problems, use `GOOGLE_SERVICE_ACCOUNT_B64` instead, containing the base64 of the file:
   - macOS: `base64 -i key.json | pbcopy`
   - Linux: `base64 -w0 key.json`
   - Windows PowerShell: `[Convert]::ToBase64String([IO.File]::ReadAllBytes("key.json"))`

3. Click **Deploy**. Every later push to GitHub redeploys automatically. If you change an environment variable, redeploy for the change to take effect.

The optional variables are listed in `.env.example`:

| Name | Default | What it does |
|---|---|---|
| `BRAIN_MODE` | `deep` | Set to `fast` to use one AI call for strategy and writing. |
| `WEB_RESEARCH` | `fallback` | `fallback` searches the web only when Google News finds nothing; `always` or `off` are the other options. |
| `GEMINI_MODEL` | `gemini-2.5-flash` | The writing model. |
| `GEMINI_FALLBACK_MODEL` | `gemini-2.5-flash-lite` | Used when the main model's daily quota is used up. |
| `GEMINI_SEARCH_MODEL` | `gemini-2.5-flash` | Must support free Google Search grounding (a 2.5 Flash or Flash-Lite model). |
| `SIGNOFF` | empty | Added to E1, E2 and E4. Use `\n` for line breaks. Leave empty if your sending tool adds a signature. |
| `MIN_FIT` | `0.30` | Rows whose role fit falls below this are skipped without using AI calls. |
| `MAX_REPAIRS` | `1` | Editor passes allowed per row (0–2). |
| `GEMINI_MIN_INTERVAL_SECONDS` | `4` | Minimum gap between AI calls. |
| `ROW_DELAY_SECONDS` | `4` | Pause the UI waits between rows. |
| `ROW_TIME_BUDGET_SECONDS` | `240` | Per-row time budget; stays under Vercel's 300-second limit. |
| `MAX_NEWS_AGE_DAYS` | `90` | Can be lowered, but never goes above 90. |

## Use it

1. Open your Vercel URL, enter the `APP_PASSWORD` and click **Connect**. The page checks the Gemini key and the service account, and shows the email you need to share sheets with.
2. Paste the Google Sheet link and click **Load tabs**. Choose the tab from the dropdown. Optionally set a row range.
   - By default, rows that already have an E1 Body, or whose Status says "Sent", are skipped.
   - Columns are matched by name, so `timezone` and `time_zone` both work.
   - Required columns: `fname`, `job_title`, `company`.
   - Missing output columns are added automatically.
3. Click **Preview first row** to see the brain's decision (pillar, service, confidence, hook, bottleneck, shortlist) and all 4 emails. Preview writes nothing to the sheet.
4. Click **Generate emails**. Rows are written one at a time. Keep the tab open while it runs. Stop it any time; the next run continues where it left off.

**Approved proof.** To let emails mention real results, add a tab named `KR_Proof` to the sheet with three columns: `Type` (`proof` or `asset`), `Text`, and `Applies to`. Only add results that are true, anonymised and approved for use. While the tab is missing or empty, the emails never claim past clients or results.

**Editing offerings.** Change `brain/offerings.py`: service names, pitches, triggers and fit weights. Push to GitHub and Vercel redeploys.

## What Status means

| Status | Action |
|---|---|
| `Ready` | Passed every check. |
| `Ready (minor: …)` | Only style notes, such as a long sentence. Skim before sending. |
| `Needs review: …` | A hard rule still failed after repair. Fix the email by hand, or clear E1 Body and run again. |
| `Skipped: low-fit role …` | Not a research buyer (for example, a student-services role). No AI quota used. |
| `Error: …` | That row failed. Clear Status and run again. |

In the **Signal** column you'll see the trigger's date, headline, source and link, followed by the pillar and service chosen. If it says **verify before sending**, the trigger came from web search rather than Google News, so open the link and confirm it. If it says **role-based**, no usable news from the last 90 days was found and the email makes no claims about an event.

## API

Every route except `/` requires the header `x-app-password`.

| Route | Purpose |
|---|---|
| `GET /api/health` | Configuration check. |
| `POST /api/sheet/tabs` | `{"sheet": url}` returns the tabs. |
| `POST /api/sheet/inspect` | Returns the column map and eligible rows. |
| `POST /api/row` | Researches, decides, writes and saves one row (`"write": false` previews it). |
| `POST /api/generate` | Sheet-free: send `{"prospect": {...}}`, get the decision and emails back as JSON. |

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt pytest httpx
cp .env.example .env    # fill it in, then export the variables
pytest -q               # offline tests
uvicorn main:app --reload
```

## Free-tier notes

- **Quota.** Gemini free quotas are per Google Cloud project, vary by model and reset at midnight Pacific time. Check yours in AI Studio. When the main model is out, the brain switches to the fallback model. When both are out, the run stops cleanly without marking rows as errors, so you can run again the next day. Fast mode and `WEB_RESEARCH=fallback` stretch the quota furthest.
- **Web research.** Google's pricing page lists free Google Search grounding, up to 500 requests a day, only for Gemini 2.5 Flash and Flash-Lite on the free tier. Keep `GEMINI_SEARCH_MODEL` on one of those.
- **Why not Google's Custom Search API?** It's closed to new customers and shuts down on January 1, 2027, so it isn't used.
- **Vercel limits.** Vercel Hobby functions stop at 300 seconds. The brain gives each row a 240-second budget and processes one row per request.
- **Privacy.** On the Gemini free tier, Google may use prompts to improve its products. The brain sends only first name, job title, company, email domain and country. Email addresses and last names are never sent. Enabling billing on the Gemini key stops Google using prompts for training.

## Troubleshooting

- **"Can't open the sheet"**: share the sheet with the service-account email as **Editor**.
- **"Google Sheets API has not been used in project…"**: enable the Sheets API in the same Google Cloud project as the service account.
- **"Model not found"**: Google retired or renamed the model. Set `GEMINI_MODEL` to a current Flash model and redeploy.
- **Timeouts on Vercel**: switch to `BRAIN_MODE=fast`, or lower `MAX_REPAIRS` to `0`.
- **Many role-based rows**: small private companies rarely make the news. Set `WEB_RESEARCH=always`, or accept role-based emails for them; those still follow the framework.
