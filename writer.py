"""Work on rows that are already in Notion.

- enrich_manual_rows(): you paste a link, the agent fills in the rest.
- cover_letters(): tick "Cover Letter" under Next Action, get a draft.
- patterns_report(): once a month, what your yes/no choices say.
"""
import datetime as dt
import re

import branding
import filters
import letter_pdf
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
- 250 to 330 words, 3 or 4 short paragraphs.
- Write the BODY ONLY. No date, no address block, no "Dear ...", no "Kind regards" and no name: those are set by the template.
- Open with the specific reason this role and company fit her, not with "I am writing to apply".
- Pick the 2 or 3 strongest pieces of evidence from her profile for what THIS role needs and say what she did, concretely.
- Never invent experience, numbers, tools or motivations. If the letter needs something only she can answer (why this company, a personal link to the brand), write a placeholder in [square brackets].
- Mention US work authorization only if the posting raises sponsorship.
- Style: plain, direct, human. Active voice. No adverbs. No em dashes. No "not X, but Y" constructions. No throat-clearing ("I'm excited to", "I believe that"). No grand closing one-liners. Vary sentence length. Name specific things instead of vague claims.

HER OWN NOTES
{her_notes}

How to use those notes, when there are any: they are the most important input you have, above the profile. They hold two things at once and you must honour both.
- Content and voice: the reasons she actually gives for wanting this role, in her own words. Keep her reasons and her phrasing. Do not replace them with smoother wording of your own, and do not add motivations she did not express.
- Instructions to you: she often writes to herself about what the letter should do ("maybe I should link my assignments", "put in that I am open to other entry-level positions"). Treat each of those as an instruction and carry it out in the letter. Do not quote the instruction itself.
Her notes may be dictated and rambling, with repetition and false starts. Read through that for the intent. If a note contradicts the profile, the note wins. If she asks for something that needs a fact she has not given you, leave a [square bracket] placeholder.

Return ONLY JSON:
{{"letter": "<body paragraphs separated by blank lines>",
  "used_from_her_notes": ["<each instruction of hers you carried out, a few words each>"],
  "cv_emphasis": ["<3 CV points to move to the top for this role>"],
  "fill_in": ["<questions she must answer herself before sending>"]}}"""


def her_draft(body: str) -> str:
    """The text she wrote herself under one of her own headings.

    Reads from a heading whose text matches config (e.g. "## My Cover Letter
    draft") down to the next heading, so the agent's own sections are skipped.
    """
    wanted = [h.lower() for h in CONFIG["cover_letter"].get("draft_headings", [])]
    if not wanted:
        return ""
    chunks, keeping = [], False
    for line in (body or "").splitlines():
        heading = re.match(r"^#{1,3}\s+(.*)$", line)
        if heading:
            label = heading.group(1).strip().lower()
            keeping = any(w in label for w in wanted)
            continue
        if keeping and line.strip():
            chunks.append(line.strip())
    return "\n".join(chunks).strip()


def _letter_filename(company: str, role: str, version: int) -> str:
    stem = re.sub(r"[^\w]+", "-", f"{company} {role}".strip()).strip("-")[:70] or "cover-letter"
    suffix = f"-v{version}" if version > 1 else ""
    return f"Cover letter - {stem}{suffix}.pdf"


def cover_letters(db, rows, profile, only_page=None):
    """Write, render and attach a letter for every row ticked "Cover Letter".

    With only_page set, just that row is handled: that is what the instant
    trigger passes in. The tick is removed once a letter lands, so ticking it
    again asks for a new version rather than being ignored.
    """
    cfg = CONFIG["cover_letter"]
    trigger = CONFIG["notion"]["cover_letter_trigger"]
    todo = [r for r in rows if trigger in (r.get("next_action") or [])]
    if only_page:
        todo = [r for r in todo if r["id"].replace("-", "") == only_page.replace("-", "")]
        if not todo:
            print(f"  [Cover letter] {only_page} is not ticked for a letter; nothing to do")
    count = 0
    for row in todo:
        company = row.get("company") or ""
        body = db.page_text(row["id"])
        version = len(re.findall(re.escape(LETTER_MARKER), body)) + 1
        job = sources.fetch_posting(row["url"]) if row.get("url") else {}
        job = job or {}
        posting = job.get("description") or body
        notes = her_draft(body)
        if len(posting) < 200 and not notes:
            db.append_blocks(row["id"], [callout(
                f"{LETTER_MARKER} not possible yet: the agent couldn't read the posting and "
                f"found no notes of yours. Paste the job description into this page, or write "
                f"your own thoughts under a '## My cover letter' heading, then tick Cover Letter again.")])
            continue
        role = row.get("title") or job.get("title", "")
        user = (f"ROLE: {role}\nCOMPANY: {company or job.get('company', '')}\n\n"
                f"POSTING:\n{posting[:7000]}")
        system = LETTER_SYSTEM.format(
            profile=profile,
            her_notes=notes or "(she has not written anything herself for this role)")
        out = scoring.parse_json(scoring.ask(settings.MODEL_DEEP, system, user, 2000))
        if not isinstance(out, dict) or not out.get("letter"):
            print(f"  could not draft a letter for {company}")
            continue

        brand = branding.brand_for(row.get("website") or job.get("website") or "", company)
        sender = letter_pdf.sender_for(row.get("location") or "", profile)
        print(f"  [Letter] {company}: accent {brand['accent']}, "
              f"{'serif' if brand['serif'] else 'sans'} ({brand.get('source')}); "
              f"{sender['region'].upper()} contact details")
        pdf_path = attached = None
        try:
            pdf_path = letter_pdf.build_letter_pdf(
                settings.LETTER_DIR / _letter_filename(company, role, version),
                out["letter"], company, role, brand, sender=sender)
        except Exception as e:                      # never lose the text over a layout error
            print(f"  [Letter] could not render the PDF for {company}: {e}")
        if pdf_path:
            upload_id = db.upload_file(pdf_path)
            if upload_id:
                attached = db.attach_file(row["id"], cfg["files_property"],
                                          upload_id, pdf_path.name)

        blocks = [heading(f"✍️ {LETTER_MARKER} {version} · {dt.date.today():%d %b %Y}", 2)]
        if not attached and pdf_path:
            blocks.append(callout("The PDF could not be attached to this row, so the text is "
                                  "below. Check that a Files & media property named "
                                  f"'{cfg['files_property']}' exists."))
        blocks += [paragraph(p.strip()) for p in re.split(r"\n\s*\n", out["letter"]) if p.strip()]
        if out.get("used_from_her_notes"):
            blocks += [heading("From your own notes", 3)] + [bullet(x) for x in out["used_from_her_notes"][:6]]
        if out.get("cv_emphasis"):
            blocks += [heading("Put these CV points first", 3)] + [bullet(x) for x in out["cv_emphasis"][:4]]
        if out.get("fill_in"):
            blocks += [heading("Answer these yourself before sending", 3)] + [bullet(x) for x in out["fill_in"][:4]]
        db.append_blocks(row["id"], blocks)

        if attached:
            remaining = [o for o in (row.get("next_action") or []) if o != trigger]
            db.set_next_action(row["id"], remaining)
        count += 1
        print(f"  [Cover letter] v{version} for {company}"
              f"{' (attached)' if attached else ' (text only)'}"
              f"{' · used your notes' if notes else ''}")
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
