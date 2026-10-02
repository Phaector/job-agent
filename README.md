# job-agent

Finds jobs every morning, scores them against your profile with Claude, and puts the best ones in your Notion database "DB Applications".

## What it does

**Every morning (7:00 New York summer time):**
- Reads company job boards directly (Greenhouse, Lever, Ashby, Workable, Recruitee, Teamtailor), searches Adzuna (New York, Belgium, Paris, Amsterdam), optionally Google Jobs, and reads job links from your newsletter and LinkedIn alert emails.
- Removes what doesn't fit for free: wrong location, senior titles, retail and store roles, internships in Europe, 3+ years required, old postings, duplicates.
- A cheap Claude model screens the rest. A stronger one scores the survivors on the full posting.
- The top 10, plus up to 3 wildcards, become **Suggested**. Other good matches wait in **Queue**.

**Every 2 hours:**
- Rows you added with only a link get filled in and scored.
- Rows with no link get the same treatment when you paste the posting into the row's page under a **Job Description** heading (handy for Handshake and LinkedIn, which need a login).
- Rows where you ticked **Cover Letter** under Next Action get a draft in the page.

**On the 1st of each month:** a patterns report appears under your profile page.

## Setup

### 1. Notion
1. In DB Applications, open the **Status** property and add three options: `Suggested`, `Queue`, `Not interested`.
2. Change **Location** to a **Select** field. The agent uses these options (and creates any that are missing): New York, Antwerp, Brussels, Ghent, Leuven, Belgium – other, Paris, Amsterdam, Remote.
3. Make sure **Score** is a **Number** field.
4. Optional: add **Why not** (Text) and **Track** (Select).
5. Create two views, both sorted by Score (descending):
   - "Suggested": filter Status is Suggested.
   - "Queue": filter Status is Queue.
6. Optional but recommended: create a page **Job Agent Profile** under "Getting a Job" and paste the contents of `profile.md` into it as plain paragraphs and bullets. From then on you edit your profile there.
7. Go to notion.so/my-integrations, create an internal integration named "Job agent", and copy its secret.
8. Open the "Getting a Job" page, click ••• → Connections → add "Job agent". This gives it access to the database and the profile page underneath.

### 2. API keys
1. **Anthropic:** console.anthropic.com → API Keys → create a key. Add credit and set a monthly spend limit under Billing.
2. **Adzuna:** developer.adzuna.com → register → copy the App ID and App Key.
3. **Optional, SerpApi (Google Jobs):** serpapi.com → free plan → copy the API key. The agent uses 3 searches a day.
4. **Optional, newsletter and alert emails:**
   1. Turn on 2-Step Verification on your Google account.
   2. Create an app password at myaccount.google.com/apppasswords.
   3. Note: this password can read your mail. It only lives in your private repo's secrets.
   4. No Gmail label or filter needed. The agent searches your mail by sender, listed under `newsletter.sources` in `config.yaml` (All Things Fashion Tech's job board and LinkedIn job alerts). To add another newsletter, open one of its emails and copy the address it came from into that list.
   5. Each email is read once and remembered in `state/seen_emails.json`, so a job board that only arrives every few weeks is still caught.

### 3. GitHub

**Quick route (recommended): the setup script**
1. Install the GitHub CLI from cli.github.com (Mac: `brew install gh`). On Windows, also install Git for Windows and use "Git Bash".
2. In this folder, copy `.env.example` to `.env` and paste in your keys. `.env` never gets uploaded.
3. Open a terminal in this folder and run `bash setup_github.sh`. It will:
   1. Log you in to GitHub in your browser.
   2. Create a private repo named `job-agent` and upload the files.
   3. Store your keys as GitHub Secrets.
   4. Start a test run and show whether it passed.
4. If the test passes, start the first real search: `gh workflow run job-agent.yml -f mode=daily`

**Manual route (in the browser)**
1. Create a **private** repository, e.g. `job-agent`.
2. Upload all files from this folder: Add file → Upload files.
   - The `.github` folder is hidden on Mac (press Cmd+Shift+. in Finder to show it).
   - If it doesn't upload, use Add file → Create new file, name it `.github/workflows/job-agent.yml`, and paste the file's contents.
3. Go to Settings → Secrets and variables → Actions → New repository secret. Add:

| Secret | Value |
|---|---|
| ANTHROPIC_API_KEY | your Anthropic key |
| NOTION_API_KEY | the integration secret |
| NOTION_DATABASE_ID | `2ebd1ac0402e8101b6beeaee550c7d18` |
| NOTION_PROFILE_PAGE_ID | the ID at the end of your profile page's link (optional) |
| ADZUNA_APP_ID / ADZUNA_APP_KEY | from Adzuna |
| SERPAPI_KEY | optional |
| GMAIL_ADDRESS / GMAIL_APP_PASSWORD | optional |

4. Open the Actions tab → **Job agent** → Run workflow:
   1. Mode `light` first: checks the Notion connection.
   2. Then mode `daily`: the first real search.
5. If the "Remember screened postings" step fails with a permission error, go to Settings → Actions → General → Workflow permissions and select "Read and write permissions".

### Optional: test on your laptop first
```
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # then fill in your keys
python -m tests.test_offline    # checks the logic, no keys needed
python main.py --dry-run        # full run that prints instead of writing to Notion
```

## Daily use

**Each morning, open the Suggested view:**
- Want it → set Status to **To Apply**. This counts as "yes".
- Don't want it → set Status to **Not interested**, ideally with a few words in "Why not". This counts as "no".
- Neither → leave it. "Archived" is neutral and teaches the agent nothing.

**Found a job yourself?** Add a row with only the Reference Link and Status **To Apply**. Within 2 hours it's filled in and scored, and if the company posts through a supported job board, that company joins the watchlist.

**Want a cover letter?** Tick **Cover Letter** under Next Action. The draft appears in the page within 2 hours, plus which CV points to put first and what you need to fill in yourself.

**Want more today?**
- Open the Queue view; it's already ranked.
- Or: Actions → Run workflow → mode `next` (moves the next 10 to Suggested).
- Or: mode `daily` for a fresh search.

**Repeats:** a job that has been in Notion once is never added again. Queue rows older than 14 days go to Notion's trash (recoverable for 30 days).

## Tuning (config.yaml)

| What | Section |
|---|---|
| How many suggestions per day, score thresholds | `limits` |
| Weights of the sub-scores | `weights` |
| Cities and their spellings | `locations` |
| Excluded titles, seniority words | `filters` |
| Search terms per country | `search` |
| Company boards to watch | `watchlist` |
| Newsletters and alert emails to read | `newsletter.sources` |

Your directions and background live in the Notion profile page (or `profile.md`).

## Limits to know
- Some career sites only show their jobs after running JavaScript. The agent can't read those. It adds a note to the row; paste the description into the page and the next run fills the row in from that.
- LinkedIn, Indeed and Glassdoor are not scraped; Adzuna and Google Jobs cover much of the same market, and LinkedIn's own alert emails are read for their links.
- Fashion PR agencies mostly run their careers pages by hand rather than on a job board the agent can read. The Independents feed covers Karla Otto, Lucien Pagès, CTZAR, K2, Lefty and WE ARE ONA; for BPCM, Purple, KCD, The Lede Company and Factory PR, set up LinkedIn job alerts and let the email channel catch them.
- Applicant counts are almost never available through these sources, so freshness relies on posting dates.
- SpreeAI (Rippling) and Alvanon don't use a supported job board, so they can't be watched automatically yet.
- Each morning run uses Claude for up to 40 full scores, so keep a spend limit set in the Anthropic console. GitHub's free plan includes 2,000 Actions minutes a month for private repos; this agent should need well under that.
