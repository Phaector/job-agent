"""Work on rows that are already in Notion.

- enrich_manual_rows(): you paste a link or a job description, the agent fills in the rest.
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


POSTING_MIN_CHARS = 400
AGENT_HEADINGS = (LETTER_MARKER, "Job agent notes")
POSTING_HEADINGS = [h.lower() for h in CONFIG["notion"].get(
    "posting_headings", ["job description", "job posting", "vacancy", "vacature"])]


def pasted_posting(body: str, needs_heading: bool) -> str:
    """The job description she pasted into a row's page.

    The agent's own sections (notes, letter drafts, warnings) are left out.
    With needs_heading, only text under one of her "Job Description" headings
    counts, so a row holding nothing but interview notes is not mistaken for a
    posting. Returns "" when what is there is too short to be one.
    """
    lines, keeping = [], not needs_heading
    for line in (body or "").splitlines():
        heading = re.match(r"^#{1,3}\s+(.*)$", line)
        if heading:
            label = heading.group(1).strip().lower()
            if any(m.lower() in label for m in AGENT_HEADINGS):
                break
            if not keeping and any(h in label for h in POSTING_HEADINGS):
                keeping = True
                continue
        if FAIL_MARKER in line or f"{LETTER_MARKER} not possible" in line:
            continue
        if keeping and line.strip():
            lines.append(line.strip())
    text = "\n".join(lines).strip()
    return text if len(text) >= POSTING_MIN_CHARS else ""


def enrich_manual_rows(db, rows, profile, taste):
    """Fill in rows she added herself: from the link when it can be read,
    otherwise from the job description pasted into the row's page."""
    lim = CONFIG["limits"]
    todo = [r for r in rows if r.get("score") is None
            and r.get("status") not in (ST["suggested"], ST["queue"])
            and _recent(r, lim["enrich_rows_created_within_days"])]
    done = tried = 0
    for row in todo:
        if tried >= lim["max_enrich_per_run"]:
            break
        url = row.get("url") or ""
        body = db.page_text(row["id"])
        pasted = pasted_posting(body, needs_heading=not url)
        job = None
        if url and FAIL_MARKER not in body:
            job = sources.fetch_posting(url)
            if not job and not pasted:
                db.append_blocks(row["id"], [callout(
                    f"{FAIL_MARKER} automatically (the site may need a login or JavaScript). "
                    "Paste the job description into this page and the agent fills in the "
                    "rest on its next run.")])
        if not job and pasted:
            job = sources.make_job(url=url, title=row.get("title") or "", company=row.get("company") or "",
                                   description=pasted, source="Pasted into the page")
        if not job:
            continue
        tried += 1
        job["url"] = url
        result = scoring.deep_score(job, profile, taste)
        if not result:
            print(f"  could not score {url or row.get('company')}")
            continue
        scoring.apply_result(job, result)
        job["loc_option"], job["loc_tier"] = filters.location_bucket(job.get("location", ""))
        job["is_internship"] = job["is_internship"] or filters.is_internship(job["title"])
        job["is_trainee"] = job["is_trainee"] or filters.is_trainee(job["title"])
        props = db.build_properties(job, status=None, only_fill_empty_row=row)
        db.update_properties(row["id"], props)
        db.append_blocks(row["id"], [heading("Job agent notes", 2)] + job_blocks(job))
        done += 1
        print(f"  [Your rows] {job['total']} · {job['title']} @ {job['company']} ({job['source']})")
    return done


# ─── Filling in who the company is ───────────────────────────────────────────
ABOUT_MARKER = "About this company"
ABOUT_SYSTEM = """You write one short paragraph saying what a company does, for someone \
deciding whether to apply there.

From the text of their own website, write 2 or 3 sentences covering: what they make or do, \
who their customers are, and anything about their size, age or market that a candidate would \
want to know. Then, on a new line starting with "Signals: ", give at most three short notes \
that bear on working there, such as a house that is part of a larger group, a very small team, \
a chain of shops, or a recent funding round. Leave the Signals line out entirely if the text \
does not support any.

Use only what the text says. No marketing language, no adjectives the company uses about \
itself, nothing invented. If the text is too thin to tell, reply exactly: UNCLEAR"""


def company_blurb(company: str, site: str) -> str:
    """Two or three sentences on what a company does, from their own site."""
    if not settings.ANTHROPIC_API_KEY or not site:
        return ""
    r = sources.http_get(site, retries=1, timeout=15)
    if r is None or not getattr(r, "text", ""):
        return ""
    text = sources.html_to_text(r.text)[:6000]
    if len(text) < 200:
        return ""
    out = scoring.ask(settings.MODEL_SCREEN, ABOUT_SYSTEM,
                      f"COMPANY: {company}\nWEBSITE: {site}\n\nTHEIR SITE:\n{text}", 400)
    out = (out or "").strip()
    return "" if out.upper().startswith("UNCLEAR") else out


def fill_company_websites(db, rows, limit=None):
    """Find each company's site, note what they do, and save both to the row.

    Without a website the letter has no brand to read, and a row says nothing
    about who the company actually is. Both are filled here, once per company,
    in the light run.
    """
    cfg = CONFIG.get("company_lookup") or {}
    if not cfg.get("enabled", True):
        return 0
    limit = limit or int(cfg.get("max_per_run", 8))
    skip_status = set(cfg.get("skip_statuses") or [])
    todo = [r for r in rows
            if not (r.get("website") or "").strip()
            and (r.get("company") or "").strip()
            and r.get("status") not in skip_status][:limit]
    if not todo:
        return 0

    print(f"[Company] Looking up {len(todo)} website(s)")
    done = 0
    for row in todo:
        company = row["company"]
        site = branding.find_website(company, row.get("url") or "",
                                     location=row.get("location") or "")
        if not site:
            print(f"    {company}: no site found")
            continue
        db.update_properties(row["id"], {"Website": {"url": site}})
        done += 1
        blurb = company_blurb(company, site) if cfg.get("describe", True) else ""
        if blurb and ABOUT_MARKER not in db.page_text(row["id"]):
            blocks = [heading(f"🏷 {ABOUT_MARKER}", 3)]
            for part in blurb.split("\n"):
                part = part.strip()
                if part.lower().startswith("signals:"):
                    blocks += [bullet(s.strip()) for s in part[8:].split(";") if s.strip()]
                elif part:
                    blocks.append(paragraph(part))
            db.append_blocks(row["id"], blocks)
        print(f"    {company}: {site}{' (described)' if blurb else ''}")
    if done:
        sources.annotate("notice", f"Filled in {done} company website(s) of {len(todo)} tried")
    return done


# ─── Applications that went quiet ────────────────────────────────────────────
def _domain_of(row) -> str:
    for field in ("website", "url"):
        raw = row.get(field) or ""
        host = re.sub(r"^https?://(www\.)?", "", raw).split("/")[0].lower()
        # A link to an applicant tracking system says nothing about who would reply.
        if host and not any(ats in host for ats in (
                "recruitee", "greenhouse", "lever.co", "workable", "ashbyhq", "teamtailor",
                "myworkday", "adzuna", "linkedin", "indeed", "fashionunited", "smartrecruiters",
                "icims", "jobvite", "rippling", "personio", "jobtoolz", "bamboohr")):
            return host
    return ""


def archive_quiet_applications(db, rows, dry_run=False):
    """Archive applications that have sat untouched with no reply.

    Silence is only taken as silence when the mail was actually read: if
    Gmail cannot be reached, mail_replies_since reports every company as
    having replied, and nothing is archived.
    """
    cfg = CONFIG.get("archive_quiet") or {}
    if not cfg.get("enabled") and not dry_run:
        return 0
    days = int(cfg.get("after_days", 14))
    watched = cfg.get("statuses") or ["Applied"]
    target = cfg.get("archive_status", "Archived")
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)

    stale = []
    for row in rows:
        if row.get("status") not in watched:
            continue
        try:
            edited = dt.datetime.fromisoformat((row.get("last_edited") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if edited < cutoff:
            stale.append(row)
    if not stale:
        if dry_run:
            sources.annotate("notice", "Preview only: no application has been untouched "
                                       f"for {days}+ days, so nothing would be archived.")
        return 0

    since_day = (cutoff - dt.timedelta(days=2)).date()      # a little slack around the window
    queries = [(r["id"], _domain_of(r), r.get("company") or "") for r in stale]
    replied = sources.mail_replies_since(queries, since_day)

    quiet = [r for r in stale if r["id"] not in replied]
    print(f"[Quiet] {len(stale)} application(s) untouched for {days}+ days, "
          f"{len(stale) - len(quiet)} had a reply, {len(quiet)} to archive")
    listed = "; ".join(f"{r.get('company') or '?'} ({r.get('title') or 'no title'})"
                       for r in quiet[:12]) or "none"
    if dry_run:
        sources.annotate("notice", f"Preview only, nothing changed. {len(stale)} application(s) "
                                   f"untouched for {days}+ days, {len(stale) - len(quiet)} had a "
                                   f"reply. Would archive {len(quiet)}: {listed}")
    for row in quiet:
        label = f"{row.get('company')} · {row.get('title') or ''}".strip(" ·")
        if dry_run:
            print(f"    [preview] would archive: {label}")
            continue
        if db.set_status(row["id"], target):
            db.append_blocks(row["id"], [callout(
                f"Archived automatically on {dt.date.today():%d %b %Y}: no change to this row "
                f"for {days} days and no email from them in that time. Set the status back "
                f"if that is wrong.")])
            print(f"    archived: {label}")
    if quiet and not dry_run:
        sources.annotate("notice", f"Archived {len(quiet)} quiet application(s): {listed}")
    return len(quiet)


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
- Do not state where she lives. She is a Belgian-American dual national who moves between Belgium and New York, and a letter that plants her in the wrong one reads as careless. Say only that she needs no visa for this country, and only where the posting raises it.
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
    """The text she wrote herself, from a heading down to the next one.

    Her headings vary ("My cover letter draft", "Cover Letter Draft",
    "Motivatie"), and requiring them to start with "my" meant her notes were
    silently skipped on most rows and the letter was written from the profile
    alone.

    The risk in widening it is the agent reading its own previous letters
    back in as her voice, so anything the agent writes itself is excluded by
    name regardless of what the include list says.
    """
    cfg = CONFIG["cover_letter"]
    wanted = [h.lower() for h in cfg.get("draft_headings", [])]
    skip = [h.lower() for h in cfg.get("agent_headings", [])]
    if not wanted:
        return ""
    chunks, keeping = [], False
    for line in (body or "").splitlines():
        heading = re.match(r"^#{1,6}\s+(.*)$", line)
        if heading:
            label = heading.group(1).strip().lower()
            mine = "✍" in label or any(s in label for s in skip)
            keeping = (not mine) and any(w in label for w in wanted)
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

        site = row.get("website") or job.get("website") or ""
        if not site:
            site = branding.find_website(company, row.get("url") or "", posting)
            if site:
                print(f"  [Letter] found {company}'s site: {site}")
                db.update_properties(row["id"], {"Website": {"url": site}})
        brand = branding.brand_for(site, company)
        sender = letter_pdf.sender_for(row.get("location") or "", profile)
        print(f"  [Letter] {company}: accent {brand['accent']}, "
              f"{'serif' if brand['serif'] else 'sans'} ({brand.get('source')}); "
              f"{sender['region'].upper()} contact details")
        sources.annotate("notice", f"Letter for {company}: {brand.get('source')}, "
                                   f"accent {brand['accent']}, "
                                   f"{'used your notes' if notes else 'no notes of yours on the row'}")
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
