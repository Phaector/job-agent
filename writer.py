"""Work on rows that are already in Notion.

- enrich_manual_rows(): you paste a link, the agent fills in the rest.
- cover_letters(): tick "Cover Letter" under Next Action, get a draft.
- patterns_report(): once a month, what your yes/no choices say.
"""
import datetime as dt
import re

import filters
import scoring
import settings
import sources
from notion_db import bullet, callout, heading, job_blocks, paragraph
from settings import CONFIG

ST = CONFIG["notion"]["statuses"]
FAIL_MARKER = "Job agent could not read this posting"
LETTER_MARKER = "Cover letter draft"


# ─── Rows you added yourself ──────────────────────────────────────────────────
def _recent(row, days):
    try:
        created = dt.datetime.fromisoformat(row["created"].replace("Z", "+00:00")).date()
    except (KeyError, ValueError):
        return False
    return (dt.date.today() - created).days <= days


def enrich_manual_rows(db, rows, profile, taste):
    lim = CONFIG["limits"]
    todo = [r for r in rows if r.get("url") and r.get("score") is None
            and r.get("status") not in (ST["suggested"], ST["queue"])
            and _recent(r, lim["enrich_rows_created_within_days"])][:lim["max_enrich_per_run"]]
    if not todo:
        return 0
    print(f"[Your links] Filling in {len(todo)} row(s) you added")
    done = 0
    for row in todo:
        if FAIL_MARKER in db.page_text(row["id"]):
            continue
        job = sources.fetch_posting(row["url"])
        if not job:
            db.append_blocks(row["id"], [callout(f"{FAIL_MARKER} automatically (the site may need a login or "
                                                 "JavaScript). Paste the job description into this page "
                                                 "and remove this note to retry.")])
            continue
        job["url"] = row["url"]
        job["loc_option"], job["loc_tier"] = filters.location_bucket(job.get("location", ""))
        job["is_internship"] = filters.is_internship(job["title"])
        job["is_trainee"] = filters.is_trainee(job["title"])
        result = scoring.deep_score(job, profile, taste)
        if not result:
            print(f"  could not score {row['url']}")
            continue
        scoring.apply_result(job, result)
        props = db.build_properties(job, status=None, only_fill_empty_row=row)
        db.update_properties(row["id"], props)
        db.append_blocks(row["id"], [heading("Job agent notes", 2)] + job_blocks(job))
        done += 1
        print(f"  {job['total']} · {job['title']} @ {job['company']}")
    return done


# ─── Cover letters ────────────────────────────────────────────────────────────
LETTER_SYSTEM = """You write cover letter drafts for Phaedra, using ONLY facts from her profile.

CANDIDATE PROFILE:
{profile}

RULES
- Write in the language of the posting (English, Dutch or French).
- 250 to 330 words, 3 or 4 short paragraphs, addressed to the hiring team unless a name is given.
- Open with the specific reason this role and company fit her, not with "I am writing to apply".
- Pick the 2 or 3 strongest pieces of evidence from her profile for what THIS role needs and say what she did, concretely.
- Never invent experience, numbers, tools or motivations. If the letter needs something only she can answer (why this company, a personal link to the brand), write a placeholder in [square brackets].
- Mention US work authorization only if the posting raises sponsorship.
- Style: plain, direct, human. Active voice. No adverbs. No em dashes. No "not X, but Y" constructions. No throat-clearing ("I'm excited to", "I believe that"). No grand closing one-liners. Vary sentence length. Name specific things instead of vague claims.

Return ONLY JSON:
{{"letter": "<paragraphs separated by blank lines>",
  "cv_emphasis": ["<3 CV points to move to the top for this role>"],
  "fill_in": ["<questions she must answer herself before sending>"]}}"""


def cover_letters(db, rows, profile):
    trigger = CONFIG["notion"]["cover_letter_trigger"]
    todo = [r for r in rows if trigger in (r.get("next_action") or []) and r.get("url")]
    count = 0
    for row in todo:
        body = db.page_text(row["id"])
        if LETTER_MARKER in body:
            continue
        job = sources.fetch_posting(row["url"]) or {}
        posting = job.get("description") or body
        if len(posting) < 200:
            db.append_blocks(row["id"], [callout(f"{LETTER_MARKER} not possible yet: the agent couldn't read the "
                                                 "posting. Paste the job description into this page.")])
            continue
        user = (f"ROLE: {row.get('title') or job.get('title', '')}\nCOMPANY: {row.get('company') or job.get('company', '')}\n\n"
                f"POSTING:\n{posting[:7000]}")
        out = scoring.parse_json(scoring.ask(settings.MODEL_DEEP, LETTER_SYSTEM.format(profile=profile), user, 2000))
        if not isinstance(out, dict) or not out.get("letter"):
            print(f"  could not draft a letter for {row.get('company')}")
            continue
        blocks = [heading(f"✍️ {LETTER_MARKER} (Claude)", 2)]
        blocks += [paragraph(p.strip()) for p in re.split(r"\n\s*\n", out["letter"]) if p.strip()]
        if out.get("cv_emphasis"):
            blocks += [heading("Put these CV points first", 3)] + [bullet(x) for x in out["cv_emphasis"][:4]]
        if out.get("fill_in"):
            blocks += [heading("Answer these yourself before sending", 3)] + [bullet(x) for x in out["fill_in"][:4]]
        db.append_blocks(row["id"], blocks)
        count += 1
        print(f"  [Cover letter] drafted for {row.get('company')}")
    return count


# ─── Monthly patterns report ──────────────────────────────────────────────────
PATTERN_SYSTEM = """You review a job seeker's choices to improve her automated job search.

CURRENT PROFILE:
{profile}

Look at the roles she said yes to and no to (with her reasons where given). Write a short report in plain text with these sections, using "## " for headings and "- " for bullets:
## Patterns you see (3 to 5 bullets, specific)
## Suggested profile changes (directions or filters to add, drop or reword; quote the exact line to change)
## New search terms to try (5 to 8)
## Companies worth adding to the watchlist (only ones clearly suggested by her choices)
Be concrete and honest. No filler."""


def patterns_report(db, rows, profile):
    parent = settings.NOTION_PROFILE_PAGE_ID
    if not parent:
        print("[Patterns] Set NOTION_PROFILE_PAGE_ID to get a monthly report page.")
        return
    cutoff = (dt.date.today() - dt.timedelta(days=60)).isoformat()
    recent = [r for r in rows if (r.get("created") or "") >= cutoff and r.get("title")]
    yes = [f"- {r['company']} | {r['title']} | {r.get('track') or ''}" for r in recent if r.get("status") in ST["counts_as_yes"]]
    no = [f"- {r['company']} | {r['title']}" + (f" | why not: {r['why_not']}" if r.get("why_not") else "")
          for r in recent if r.get("status") == ST["not_interested"]]
    if len(yes) + len(no) < 5:
        print("[Patterns] Not enough yes/no choices yet for a useful report.")
        return
    text = scoring.ask(settings.MODEL_DEEP, PATTERN_SYSTEM.format(profile=profile),
                       "YES:\n" + "\n".join(yes) + "\n\nNO:\n" + "\n".join(no), 1500)
    blocks = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("## "):
            blocks.append(heading(line[3:], 2))
        elif line.startswith(("- ", "* ")):
            blocks.append(bullet(line[2:]))
        else:
            blocks.append(paragraph(line))
    title = f"Job agent patterns: {dt.date.today():%B %Y}"
    db.create_child_page(parent, title, blocks)
    print(f"[Patterns] Created '{title}'")
