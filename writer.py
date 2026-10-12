"""Work on rows that are already in Notion.

- enrich_manual_rows(): you paste a link or a job description, the agent fills in the rest.
- cover_letters(): tick "Cover Letter" under Next Action, get a draft.
- sync_application_replies(): companies' answers in Gmail update the matching row.
- patterns_report(): once a month, what your yes/no choices say.
"""
import datetime as dt
import difflib
import hashlib
import re
import unicodedata

import branding
import filters
import letter_pdf
import scoring
import settings
import sources
from notion_db import bullet, callout, heading, job_blocks, paragraph, rt
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
    still_open = 0
    today = dt.date.today()
    for row in rows:
        if row.get("status") not in watched:
            continue
        try:
            edited = dt.datetime.fromisoformat((row.get("last_edited") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if edited >= cutoff:
            continue
        # A deadline still to come means they have not finished looking yet,
        # so silence says nothing. No deadline at all, and the clock applies.
        try:
            closes = dt.date.fromisoformat((row.get("deadline") or "")[:10])
        except ValueError:
            closes = None
        if closes and closes >= today:
            still_open += 1
            continue
        stale.append(row)
    if still_open:
        print(f"[Quiet] {still_open} left alone: their deadline has not passed")
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


# ─── Replies to her applications ──────────────────────────────────────────────
REPLY_SYSTEM = """You sort emails in the inbox of Phaedra Imschoot, a recent graduate applying \
for jobs in New York and Belgium. Decide whether an email is a company's answer to one of \
her job applications, and if so what kind.

kind, exactly one of:
- "rejection": they will not move forward with her.
- "interview_invite": they want to talk: an invitation to book, confirm or pick a time for a \
call or interview, or a request to call them back.
- "interview_scheduled": a call or interview at a specific date and time is confirmed.
- "next_step": any other step forward: an assessment, a test, a form, a request for documents.
- "offer": a job offer.
- "confirmation": only confirms her application arrived or was sent (job board receipts, \
"we received your application", "application sent to ..."). Nothing has been decided.
- "other": anything else: job alerts, newsletters, marketing, login codes, account emails.

company: the employer she applied to, not the job board, recruiting platform or agency that \
sent the email (e.g. FashionUnited or Handshake are never the company). "" if unknown.
job_title: the role, if the email names it, else "".
interview_at: for interview_scheduled only, ISO 8601 with the timezone offset the email gives \
(New York or Brussels), else "".
summary: one short sentence in English on what they said or ask her to do. No praise, no \
filler.

Return ONLY JSON: {"kind": "...", "company": "...", "job_title": "...", \
"interview_at": "...", "summary": "..."}"""

REPLY_KINDS = {"rejection", "interview_invite", "interview_scheduled", "next_step", "offer"}
REPLY_LABEL = {"rejection": "Rejection", "interview_invite": "Interview invitation",
               "interview_scheduled": "Interview scheduled", "next_step": "Next step",
               "offer": "Offer"}
REPLY_EMOJI = {"rejection": "📪", "interview_invite": "📞", "interview_scheduled": "📅",
               "next_step": "➡️", "offer": "🎉"}
REPLY_REF = "Gmail ref "
EARLY_STATUSES = {None, "", "Suggested", "Queue", "To Apply", "Remembered", "Archived",
                  "Not interested"}


def _fold(text: str) -> str:
    """Lower case, accents and punctuation off, legal suffixes dropped."""
    raw = unicodedata.normalize("NFKD", text or "")
    raw = "".join(c for c in raw if not unicodedata.combining(c))
    return filters.norm_company(raw.replace("’", "'").replace("'", ""))


def _same_company(a: str, b: str) -> bool:
    fa, fb = _fold(a), _fold(b)
    if not fa or not fb:
        return False
    if fa == fb:
        return True
    ta, tb = fa.split(), fb.split()
    # "Christian Wijnants" vs "Christian Wijnants Antwerp", "Mantu" vs "Amaris Consulting" is not one.
    if len(fa) >= 4 and len(fb) >= 4 and (set(ta) <= set(tb) or set(tb) <= set(ta)):
        return True
    # Typos she makes by hand: WSGN for WGSN, Vivrellles for Vivrelle.
    return len(fa) >= 5 and difflib.SequenceMatcher(None, fa, fb).ratio() >= 0.85


def _sender_domain(address: str) -> str:
    host = (address or "").rpartition("@")[2].lower()
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def match_reply(rows, company: str, title: str, sender: str = ""):
    """The row a reply belongs to, or (None, candidates) when it cannot be told.

    Company first (by name, or by the sender's domain against the row's own
    website), then the role. With several rows at one company and no role to
    go on, the reply is not guessed onto one of them.
    """
    domain = _sender_domain(sender)
    cands = [r for r in rows
             if _same_company(company, r.get("company") or "")
             or (domain and _domain_of(r) and _sender_domain("x@" + _domain_of(r)) == domain)]
    if not cands:
        return None, []
    if len(cands) == 1:
        return cands[0], cands
    if title:
        ranked = sorted(cands, key=lambda r: -filters.title_similarity(title, r.get("title") or ""))
        best = filters.title_similarity(title, ranked[0].get("title") or "")
        second = filters.title_similarity(title, ranked[1].get("title") or "")
        if best >= 0.3 and best > second:
            return ranked[0], cands
    # Several rows, one of which she actually applied to: that is the one.
    applied = [r for r in cands if r.get("status") not in EARLY_STATUSES]
    if len(applied) == 1:
        return applied[0], cands
    return None, cands


def _ref(mail: dict) -> str:
    """A short, stable tag for an email, written into the note so it is never added twice."""
    return REPLY_REF + hashlib.sha1(mail["key"].encode()).hexdigest()[:10]


def classify_reply(mail: dict):
    text = (f"FROM: {mail.get('sender_name')} <{mail.get('sender')}>\n"
            f"DATE: {mail.get('date')}\nSUBJECT: {mail.get('subject')}\n\n"
            f"{(mail.get('body') or '')[:4000]}")
    return scoring.parse_json(scoring.ask(settings.MODEL_SCREEN, REPLY_SYSTEM, text, 400))


def _reply_block(mail: dict, info: dict):
    kind = info["kind"]
    who = mail.get("sender_name") or mail.get("sender") or "them"
    parts = [rt(f"{REPLY_LABEL[kind]} · {mail['date']} · from {who}", bold=True),
             rt(f"\n{info.get('summary') or mail.get('subject') or ''}")]
    if mail.get("link"):
        parts += [rt("\n"), rt("Open the email", link=mail["link"])]
    parts.append(rt(f"   ({_ref(mail)})"))
    return {"object": "block", "type": "callout",
            "callout": {"rich_text": parts,
                        "icon": {"type": "emoji", "emoji": REPLY_EMOJI[kind]}}}


def _reply_changes(row: dict, info: dict):
    """New status and Next Action for a row, given a reply. Never moves a row back."""
    kind, status = info["kind"], row.get("status")
    actions = list(row.get("next_action") or [])
    new_status = None
    if status == "Accepted":
        return None, actions
    if kind == "rejection":
        new_status = "Rejected" if status != "Rejected" else None
        actions = [a for a in actions if a not in ("Waiting", "First Call", "Prepare Interview")]
    elif kind == "offer":
        new_status = "Offer" if status != "Offer" else None
        actions = actions + ["Decide"]
    else:
        if status in EARLY_STATUSES:
            new_status = "Applied"
        actions = [a for a in actions if a != "Waiting"]
        actions.append({"interview_invite": "First Call", "interview_scheduled": "Prepare Interview",
                        "next_step": "Follow up"}[kind])
    seen, ordered = set(), []
    for a in actions:
        if a not in seen:
            seen.add(a)
            ordered.append(a)
    return new_status, ordered


def _is_person(address: str) -> bool:
    local = (address or "").partition("@")[0].lower()
    return bool(local) and not re.search(r"no-?reply|do-?not-?reply|notification|mailer|"
                                         r"careers?@|jobs?@|recruit|talent|hr@|hiring@|"
                                         r"^job\.|^e\+", local + "@")


def _reply_props(db, row, info, mail, new_status, actions):
    props = {}
    if new_status:
        db._set(props, "status", ("select", "status"), db._choice("status", new_status))
    if actions != list(row.get("next_action") or []):
        db._set(props, "next_action", ("multi_select",),
                {"multi_select": [{"name": a} for a in actions]})
    when = (info.get("interview_at") or "").strip()
    if info["kind"] == "interview_scheduled" and re.match(r"\d{4}-\d{2}-\d{2}", when):
        is_time = "T" in when
        db._set(props, "interview_date", ("date",), {"date": {"start": when if is_time else when[:10]}})
    if not row.get("contact") and _is_person(mail.get("sender")):
        db._set(props, "contact", ("email",), {"email": mail["sender"]})
    return props


def sync_application_replies(db, rows, dry_run=False):
    """Read companies' answers to her applications in Gmail and record them in Notion.

    Only real answers count: a rejection, an invitation to talk, a scheduled
    interview, another step forward or an offer. Receipts ("we received your
    application", "application sent") are left alone. The matching row gets
    its Status and Next Action updated and a note with a link to the email;
    an answer to an application that has no row yet gets a new one.
    """
    cfg = CONFIG.get("replies") or {}
    if not cfg.get("enabled") and not dry_run:
        return 0
    if not settings.ANTHROPIC_API_KEY:
        print("  [Replies] No ANTHROPIC_API_KEY, skipping")
        return 0
    state = sources.read_reply_state()
    mails = sources.application_emails(cfg.get("gmail_query", ""), cfg.get("lookback_days", 3),
                                       skip_keys=state, max_emails=cfg.get("max_emails", 30))
    if mails is None:
        sources.annotate("warning", "Could not read Gmail, so no replies to applications "
                                    "were checked this run.")
        return 0
    if not mails:
        print("[Replies] No new emails to check")
        return 0
    print(f"[Replies] Checking {len(mails)} new email(s)")
    today = dt.date.today().isoformat()
    done, unclear = [], []
    for mail in mails:
        info = classify_reply(mail)
        if not isinstance(info, dict) or not info.get("kind"):
            print(f"    could not sort: {mail['subject'][:70]} (tried again next run)")
            continue
        state[mail["key"]] = today
        kind = info["kind"]
        if kind not in REPLY_KINDS:
            continue
        company = (info.get("company") or "").strip()
        title = (info.get("job_title") or "").strip()
        row, cands = match_reply(rows, company, title, mail.get("sender"))
        label = f"{company or '?'}{f' · {title}' if title else ''}"

        if row is None and cands:
            # Several rows at this company and nothing to tell them apart: a note
            # on each beats a status change on the wrong one.
            for c in cands:
                if _ref(mail) not in db.page_text(c["id"]):
                    db.append_blocks(c["id"], [_reply_block(mail, info), callout(
                        "This reply could be about another application at this company too, "
                        "so the status was not changed. Update it if it is about this one.", "❔")])
            unclear.append(f"{REPLY_LABEL[kind]} from {label}")
            print(f"    {REPLY_LABEL[kind]}: {label} -> {len(cands)} possible rows, noted on each")
            continue

        if row is None:
            if not cfg.get("create_missing_rows", True) or not company:
                unclear.append(f"{REPLY_LABEL[kind]} from {label} (no row)")
                continue
            new_status, actions = _reply_changes({"status": "Applied"}, info)
            props = {db.title_prop: {"title": [rt(company)]}}
            db._set(props, "job_title", ("rich_text",), {"rich_text": [rt(title)]})
            db._set(props, "status", ("select", "status"),
                    db._choice("status", new_status or "Applied"))
            db._set(props, "type", ("select", "status"), db._choice("type", "Job"))
            db._set(props, "date_found", ("date",), {"date": {"start": mail["date"]}})
            props.update(_reply_props(db, {"next_action": ["Waiting"]}, info, mail, None, actions))
            page = db.create_row(props, [_reply_block(mail, info)], label=label)
            if page:
                rows.append({"id": page, "company": company, "title": title,
                             "status": new_status or "Applied", "next_action": actions,
                             "last_edited": dt.datetime.now(dt.timezone.utc).isoformat()})
                done.append(f"{REPLY_LABEL[kind]} · {label} (new row)")
                print(f"    {REPLY_LABEL[kind]}: {label} -> new row")
            continue

        if _ref(mail) in db.page_text(row["id"]):
            continue                    # recorded on an earlier run
        new_status, actions = _reply_changes(row, info)
        props = _reply_props(db, row, info, mail, new_status, actions)
        if props:
            db.update_properties(row["id"], props)
        db.append_blocks(row["id"], [_reply_block(mail, info)])
        # Keep this run's picture current, so the quiet-application check that
        # follows sees the new status rather than the old one.
        row["status"] = new_status or row.get("status")
        row["next_action"] = actions
        row["last_edited"] = dt.datetime.now(dt.timezone.utc).isoformat()
        moved = f" ({row.get('status')})" if new_status else ""
        done.append(f"{REPLY_LABEL[kind]} · {row.get('company')}{moved}")
        print(f"    {REPLY_LABEL[kind]}: {row.get('company')} · {row.get('title') or ''}{moved}")

    if not dry_run:
        sources.write_reply_state(state)
    if done:
        sources.annotate("notice", f"Recorded {len(done)} repl(ies) to your applications: "
                                   + "; ".join(done[:12]))
    if unclear:
        sources.annotate("warning", "Replies that could not be matched to one row: "
                                    + "; ".join(unclear[:8]))
    return len(done)


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
    ticked = [r for r in rows if trigger in (r.get("next_action") or [])]
    todo = ticked
    if only_page:
        want = only_page.replace("-", "").strip()
        todo = [r for r in todo if r["id"].replace("-", "") == want]
        if not todo:
            named = [r for r in rows if r["id"].replace("-", "") == want]
            why = ("that row is not ticked for a letter" if named
                   else "no row on your database has that id")
            sources.annotate("warning", f"No letter written: {why}. {len(ticked)} row(s) are "
                                        f"ticked for a letter in total.")
            print(f"  [Cover letter] {only_page}: {why}")
    elif not ticked:
        print("  [Cover letter] nothing ticked")
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
            sources.annotate("warning", f"No letter for {company}: the posting could not be "
                                        f"read ({len(posting)} characters) and the row has no "
                                        "notes of yours.")
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
        # A letter plus its three lists is long, and a reply cut off mid-JSON
        # parses as nothing. One retry costs a few seconds and saves her
        # having to tick the row again.
        out, raw = None, ""
        for attempt in (1, 2):
            raw = scoring.ask(settings.MODEL_DEEP, system, user, 3000)
            out = scoring.parse_json(raw)
            if isinstance(out, dict) and out.get("letter"):
                break
            print(f"  [Letter] attempt {attempt} for {company} came back unusable "
                  f"({len(raw)} characters)")
        if not isinstance(out, dict) or not out.get("letter"):
            opening = " ".join((raw or "").split())[:240]
            sources.annotate("warning", f"No letter for {company}: the model did not return one "
                                        f"after two tries (last reply {len(raw)} characters). "
                                        f"Posting {len(posting)} characters, "
                                        f"{'notes found' if notes else 'no notes'}. "
                                        f"It began: {opening or '(nothing at all)'} "
                                        f"— the row is still ticked, so the next run retries it.")
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
        fonts = letter_pdf.fonts_for(brand)
        got, theirs = fonts.get("named", ""), letter_pdf.tidy_family(brand.get("font_name", ""))
        if not got:
            set_in = "set in the default face"
        elif theirs and got.lower() == theirs.lower():
            set_in = f"set in their own {got}"
        elif theirs:
            set_in = f"set in {got}, the closest to their {theirs}"
        else:
            set_in = f"set in {got}"
        print(f"  [Letter] {company}: accent {brand['accent']}, "
              f"{'serif' if brand['serif'] else 'sans'} ({brand.get('source')}); "
              f"{set_in}; {sender['region'].upper()} contact details")
        sources.annotate("notice", f"Letter for {company}: {brand.get('source')}, "
                                   f"accent {brand['accent']}, {set_in}, "
                                   f"{'used your notes' if notes else 'no notes of yours on the row'}")
        pdf_path = attached = None
        try:
            pdf_path = letter_pdf.build_letter_pdf(
                settings.LETTER_DIR / _letter_filename(company, role, version),
                out["letter"], company, role, brand, sender=sender, fonts=fonts)
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
