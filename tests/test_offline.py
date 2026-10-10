"""Offline checks: run with  python -m tests.test_offline  (no API keys needed)."""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bs4 import BeautifulSoup  # noqa: E402

import filters  # noqa: E402
import main  # noqa: E402
import notion_db  # noqa: E402
import scoring  # noqa: E402
import sources  # noqa: E402

TODAY = dt.date.today().isoformat()
passed = 0


def check(cond, label):
    global passed
    assert cond, f"FAILED: {label}"
    passed += 1


# ── ATS detection on your saved links ──
cases = {
    "https://apply.workable.com/wolfandbadger/j/60543C348E/": ("workable", "wolfandbadger", "60543C348E"),
    "https://jobs.lever.co/wgsn/4fc33f6f-a386-4505-a091-47cbafd164d1": ("lever", "wgsn", "4fc33f6f-a386-4505-a091-47cbafd164d1"),
    "https://job-boards.greenhouse.io/stockx?error=true": ("greenhouse", "stockx", None),
    "https://job-boards.greenhouse.io/boldmetrics/jobs/4696097005?gh_jid=4696097005": ("greenhouse", "boldmetrics", "4696097005"),
    "https://jobs.ashbyhq.com/linear/abc-123": ("ashby", "linear", "abc-123"),
    "https://acme.recruitee.com/o/junior-analyst": ("recruitee", "acme", "junior-analyst"),
}
for url, (ats, slug, jid) in cases.items():
    info = sources.detect_ats(url)
    check(info and info["ats"] == ats and info["slug"] == slug and info["job_id"] == jid, f"detect {url}")
check(sources.detect_ats("https://www.tryprofound.com/") is None, "non-ATS link")

# ── URL normalisation ──
check(filters.normalize_url("https://www.Example.com/jobs/1/?utm_source=x") == "example.com/jobs/1", "strip tracking")
check("jk=abc" in filters.normalize_url("https://be.indeed.com/viewjob?jk=abc&from=serp"), "keep indeed id")

# ── Locations ──
loc = {
    "New York, NY": ("New York", "main"), "Brooklyn, New York": ("New York", "main"),
    "Antwerpen, Antwerpen": ("Antwerp", "main"), "Bruxelles, Bruxelles-Capitale": ("Brussels", "main"),
    "Gent, Oost-Vlaanderen": ("Ghent", "main"), "Mechelen, Belgium": ("Belgium – other", "main"),
    "Paris, Ile-de-France": ("Paris", "stretch"), "Amsterdam, Noord-Holland": ("Amsterdam", "stretch"),
    "Remote - US": ("Remote", "main"), "Remote": ("Remote", "main"),
    "London, UK": (None, None), "Remote - India": (None, None), "Carlsbad, CA": (None, None),
    "Simmons, TX": (None, None),
}
for text, expected in loc.items():
    check(filters.location_bucket(text) == expected, f"location {text} -> {filters.location_bucket(text)}")

# ── Years required ──
check(filters.min_years_required("Requires 3+ years of experience in retail") == 3, "3+ years")
check(filters.min_years_required("1-3 years of relevant experience") == 1, "range min")
check(filters.min_years_required("minimum 5 jaar werkervaring") == 5, "dutch")
check(filters.min_years_required("au moins 2 ans d'expérience") == 2, "french")
check(filters.min_years_required("We are a fast-growing startup") is None, "none")


def job(title, location="New York, NY", desc="", posted=TODAY, company="Acme", url=None):
    return sources.make_job(url or f"https://x.com/{title.replace(' ', '-')}-{company}", title, company,
                            location, desc, posted, "test")


# ── Hard filter ──
check(filters.hard_filter(job("Client Success Associate"))[0], "keep CS associate")
check(filters.hard_filter(job("3D Design & Implementation Specialist"))[0], "keep CLO-style role")
check(not filters.hard_filter(job("Senior Account Manager"))[0], "drop senior")
check(not filters.hard_filter(job("Sales Associate - Part Time"))[0], "drop store sales")
check(not filters.hard_filter(job("Fashion Designer"))[0], "drop designer")
check(not filters.hard_filter(job("Marketing Intern", "Brussels"))[0], "drop EU internship")
j = job("Merchandising Intern", "New York, NY")
check(filters.hard_filter(j)[0] and j["is_internship"], "keep NY internship, flagged")
j = job("Graduate Programme Supply Chain", "Antwerpen")
check(filters.hard_filter(j)[0] and j["is_trainee"], "keep EU graduate programme")
check(not filters.hard_filter(job("Analyst", desc="You bring 4+ years of experience."))[0], "drop 4+ years")
old = (dt.date.today() - dt.timedelta(days=40)).isoformat()
check(not filters.hard_filter(job("Analyst", posted=old))[0], "drop old posting")
check(not filters.hard_filter(job("Analyst", "London"))[0], "drop London")

# ── Duplicates ──
a = job("Partnerships Associate (m/f/x)", company="SpreeAI NV", url="https://a.com/1", desc="short")
b = job("Partnerships Associate", company="SpreeAI", url="https://b.com/2", desc="a much longer description" * 5)
c = job("Partnerships Associate", company="SpreeAI", url="https://a.com/1")
merged = filters.dedupe_batch([a, b, c])
check(len(merged) == 1 and merged[0]["url"] == "https://b.com/2" and merged[0]["also_posted_on"], "merge crossposts")
rows = [{"company": "WGSN", "title": "Client Success Coordinator", "url": ""}]
check(filters.matches_existing(job("Client Success Coordinator", company="WGSN Inc"), rows), "match existing row")

# ── Scoring maths ──
sub = {"skills_fit": 8, "direction_fit": 9, "growth_onboarding": 6, "taste_match": 8, "feasibility": 7}
total = scoring.compute_total(sub, {"posted_at": TODAY})
_w = scoring.CONFIG["weights"]          # read the live weights, so tuning them never breaks this
_expected = round(sum(sub[k] * _w[k] for k in sub) + 10 * _w["freshness"], 1)
check(total == _expected, f"total {total} should be {_expected}")
check(round(sum(_w.values()), 3) == 1.0, f"the weights add up to 1.0 ({sum(_w.values())})")
check(_w["feasibility"] > _w["taste_match"], "being hireable now outranks resembling past picks")
check(scoring.parse_json('```json\n{"a": 1}\n```') == {"a": 1}, "parse fenced json")
check(scoring.parse_json('Here: [{"i":1,"keep":true}]', "array")[0]["keep"], "parse array")

# ── Taste block ──
t_rows = [{"company": "Lyst", "title": "Brand Partnerships", "status": "To Apply", "created": "2026-09-01"},
          {"company": "Bank", "title": "Teller", "status": "Not interested", "why_not": "not my field",
           "created": "2026-09-02"}]
taste = scoring.taste_block(t_rows)
check("Lyst | Brand Partnerships" in taste and "not my field" in taste and "WGSN" in taste, "taste block")

# ── JSON-LD parsing ──
html = """<html><script type="application/ld+json">{"@type":"JobPosting","title":"Production Coordinator",
"hiringOrganization":{"name":"Delvaux"},"datePosted":"2026-09-20","validThrough":"2026-10-15",
"jobLocation":{"address":{"streetAddress":"Rue X 1","postalCode":"1000","addressLocality":"Brussels",
"addressCountry":"BE"}},"description":"<p>Coordinate the atelier.</p>",
"baseSalary":{"currency":"EUR","value":{"minValue":38000,"maxValue":42000}}}</script></html>"""
posting = sources._jsonld_posting(BeautifulSoup(html, "html.parser"))
pj = sources._jsonld_to_job(posting, "https://delvaux.com/job", "test")
check(pj["company"] == "Delvaux" and pj["deadline"] == "2026-10-15" and "Rue X 1" in pj["address"]
      and pj["salary"]["min"] == 38000 and "atelier" in pj["description"], "json-ld")

# ── Notion blocks ──
full = job("Client Success Coordinator", company="WGSN")
full.update({"total": 8.4, "track": "Fashion-tech", "freshness": 10, "loc_option": "New York",
             "score_detail": {"one_line": "x", "why_it_fits": ["a" * 3000], "needs": [{"need": "n", "evidence": "e", "met": "yes"}],
                              "gaps": [{"gap": "g", "learnable": True, "how": "h"}], "red_flags": [],
                              "subscores": sub}})
blocks = notion_db.job_blocks(full)
lengths = [len(rt["text"]["content"]) for b in blocks for rt in b[b["type"]].get("rich_text", [])]
check(blocks and max(lengths) <= notion_db.TEXT_LIMIT, "blocks respect Notion text limit")


# ── Selection logic with a fake Notion ──
class FakeDB:
    def __init__(self):
        self.created, self.status = [], []

    def create_job(self, j, status):
        self.created.append((j["title"], status))
        return "id"

    def set_status(self, page_id, status):
        self.status.append((page_id, status))
        return True


scored = []
for i in range(14):
    s = job(f"Role {i}")
    s.update({"total": 9.5 - i * 0.2, "track": "Tech business", "loc_tier": "main"})
    scored.append(s)
for i in range(4):
    w = job(f"Wild {i}")
    w.update({"total": 7.5 + i * 0.1, "track": "Wildcard", "loc_tier": "main"})
    scored.append(w)
p = job("Paris role", "Paris")
p.update({"total": 8.5, "track": "Fashion business", "loc_tier": "stretch"})
scored.append(p)
q_rows = [{"id": "q1", "status": "Queue", "score": 9.9, "track": "Fashion business"},
          {"id": "q2", "status": "Queue", "score": 5.0, "track": None}]
fake = FakeDB()
main.select_and_push(fake, q_rows, scored)
suggested = [t for t, s in fake.created if s == "Suggested"]
check(("q1", "Suggested") in fake.status, "strong queue row promoted")
check(len(suggested) == 9 + 3, f"9 new + 3 wildcards suggested ({len(suggested)})")
check("Paris role" not in [t for t, _ in fake.created], "stretch below 9 dropped")
check(all(s == "Queue" for t, s in fake.created if t not in suggested), "rest queued")

# ── Watchlist is built from seeds + rows ──
boards = main.watchlist_boards([{"url": "https://jobs.ashbyhq.com/phia/123", "company": "Phia"}])
keys = {(a, s) for a, s, _ in boards}
for expected in [("workable", "wolfandbadger"), ("lever", "wgsn"), ("greenhouse", "stockx"),
                 ("ashby", "phia"), ("workable", "lyst"), ("greenhouse", "boldmetrics")]:
    check(expected in keys, f"watchlist has {expected}")

# ── Teamtailor board (fashion PR agencies) ──
TT_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:tt="https://teamtailor.com/locations">
  <channel>
    <title>The Independents</title>
    <link>https://careers.the-independents.com/jobs</link>
    <item>
      <title>Account Coordinator, Fashion Communications</title>
      <description>&lt;p&gt;Join our New York press office team.&lt;/p&gt;</description>
      <pubDate>Mon, 28 Sep 2026 10:29:20 +0200</pubDate>
      <link>https://careers.the-independents.com/jobs/8462906-account-coordinator</link>
      <remoteStatus>none</remoteStatus>
      <tt:locations><tt:location><tt:name>New York - Park Ave S</tt:name><tt:address>Park Ave S</tt:address><tt:zip>10016</tt:zip><tt:city>New York</tt:city><tt:country>United States</tt:country></tt:location></tt:locations>
      <tt:division>Karla Otto</tt:division>
    </item>
    <item>
      <title>Consolidation Assistant</title>
      <description>&lt;p&gt;Support our Group Finance Paris team.&lt;/p&gt;</description>
      <pubDate>Mon, 28 Sep 2026 10:29:20 +0200</pubDate>
      <link>https://careers.the-independents.com/jobs/8462907-consolidation-assistant</link>
      <tt:locations></tt:locations>
      <tt:division/>
    </item>
    <item>
      <title>Producer, Global</title>
      <description>&lt;p&gt;Working across our New York, Paris and Amsterdam offices.&lt;/p&gt;</description>
      <link>https://careers.the-independents.com/jobs/8462908-producer</link>
      <tt:locations/>
    </item>
  </channel>
</rss>"""


class FakeResponse:
    content = TT_FEED


_real_http_get = sources.http_get
sources.http_get = lambda *a, **k: FakeResponse()
try:
    tt = sources.board_teamtailor("careers.the-independents.com", "The Independents")
finally:
    sources.http_get = _real_http_get

check(len(tt) == 3, f"three Teamtailor items parsed ({len(tt)})")
check(tt[0]["location"] == "New York", f"location read from tt:locations ({tt[0]['location']!r})")
check(tt[0]["company"] == "Karla Otto", "tt:division names the agency in a group feed")
check(tt[0]["posted_at"] == "2026-09-28", f"RFC 822 pubDate parsed ({tt[0]['posted_at']})")
check(tt[0]["description"].startswith("Join our New York"), "description unescaped to text")
check(tt[1]["location"] == "Paris", "single city in the text is used when no location field")
check(tt[1]["company"] == "The Independents", "falls back to the feed's own name")
check(tt[2]["location"] == "", "several cities in the text are not guessed at")
check(sources.detect_ats("https://careers.the-independents.com/jobs/8462906-account-coordinator")
      == {"ats": "teamtailor", "slug": "careers.the-independents.com",
          "job_id": "8462906-account-coordinator", "eu": False}, "Teamtailor link detected")
check(sources.detect_ats("https://careers.somebrand.com/jobs/marketing-lead") is None,
      "a non-Teamtailor careers link is not mistaken for one")
check(sources.detect_ats("https://karlaotto.teamtailor.com/jobs")["slug"] == "karlaotto.teamtailor.com",
      "teamtailor.com subdomain detected")
check(main.watchlist_boards([])[("teamtailor", "careers.karlaotto.com", False)] == "Karla Otto",
      "Teamtailor boards come through from config")


# ── Newsletter / alert emails: read by sender, each email parsed once ──
class FakeIMAP:
    """Minimal stand-in for imaplib.IMAP4_SSL holding two fake emails."""
    MAIL = {
        "allthingsfashiontech+job-board@substack.com": (
            "<jobboard-1@substack.com>",
            '<a href="https://job-boards.greenhouse.io/moncler/jobs/123">Assistant Merchandiser</a>'
            '<a href="https://jobs.lever.co/depop/abc-def">Partnerships Coordinator</a>'
            '<a href="https://allthingsfashiontech.substack.com/subscribe">Subscribe</a>'
            '<a href="https://twitter.com/atft">Follow us</a>'),
        "jobalerts-noreply@linkedin.com": (
            "<alert-1@linkedin.com>",
            '<a href="https://www.linkedin.com/comm/jobs/view/4123456789/?trackingId=xY%3D">PR Coordinator</a>'
            '<a href="https://www.linkedin.com/comm/jobs/view/4123456789/?trackingId=other">same job again</a>'
            '<a href="https://www.linkedin.com/psettings/email">Unsubscribe settings</a>'),
    }

    def __init__(self, *a, **k):
        self.current = None

    def login(self, *a):
        return "OK", []

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"',
                      b'(\\All \\HasNoChildren) "/" "[Gmail]/All Mail"']

    def select(self, mailbox, readonly=False):
        return ("OK", []) if "All Mail" in mailbox else ("NO", [])

    def search(self, charset, criteria):
        self.current = next((a for a in self.MAIL if f'FROM "{a}"' in criteria), None)
        return ("OK", [b"1"]) if self.current else ("OK", [b""])

    def fetch(self, num, spec):
        msgid, body = self.MAIL[self.current]
        raw = (f"Message-ID: {msgid}\r\nSubject: Jobs\r\n"
               f"Content-Type: text/html; charset=utf-8\r\n\r\n{body}").encode()
        return "OK", [(b"1 (RFC822 {})", raw)]

    def logout(self):
        return "OK", []


class _ListBox:
    def __init__(self, lines): self.lines = lines

    def list(self):
        return "OK", self.lines


check(sources._all_mail_box(_ListBox([b'(\\HasNoChildren) "/" "INBOX"',
                                      b'(\\All \\HasNoChildren) "/" "[Gmail]/All Mail"']))
      == '"[Gmail]/All Mail"', "All Mail is found in English")
check(sources._all_mail_box(_ListBox([b'(\\All \\HasNoChildren) "/" "[Gmail]/Alle berichten"']))
      == '"[Gmail]/Alle berichten"', "All Mail is found in Dutch, by its flag")
check(sources._all_mail_box(_ListBox([b'(\\HasNoChildren) "/" "INBOX"'])) == "INBOX",
      "an account without All Mail falls back to the inbox")
check(sources.tidy_link("https://www.linkedin.com/comm/jobs/view/4123456789/?trackingId=xY%3D")
      == "https://www.linkedin.com/jobs/view/4123456789/", "LinkedIn tracking link tidied")
check(sources.tidy_link("https://job-boards.greenhouse.io/moncler/jobs/123")
      == "https://job-boards.greenhouse.io/moncler/jobs/123", "other links left alone")

_real_imap = sources.imaplib.IMAP4_SSL
_real_state = sources.settings.MAIL_STATE_FILE
sources.imaplib.IMAP4_SSL = FakeIMAP
sources.settings.GMAIL_ADDRESS = "her@gmail.com"
sources.settings.GMAIL_APP_PASSWORD = "app-password"
sources.settings.MAIL_STATE_FILE = Path("/tmp/job-agent-test-seen-emails.json")
sources.settings.MAIL_STATE_FILE.unlink(missing_ok=True)
try:
    first = sources.newsletter_job_links()
    urls = [u for u, _ in first]
    names = {n for _, n in first}
    check(len(first) == 3, f"3 job links from 2 emails, dupes and social dropped ({len(first)})")
    check("https://www.linkedin.com/jobs/view/4123456789/" in urls, "LinkedIn job kept once")
    check("https://job-boards.greenhouse.io/moncler/jobs/123" in urls, "newsletter job kept")
    check(not any("subscribe" in u or "twitter" in u or "psettings" in u for u in urls),
          "subscribe, social and settings links dropped")
    check(names == {"All Things Fashion Tech", "LinkedIn job alerts"}, "source name per link")
    check(sources.newsletter_job_links() == [], "same emails are not read twice")
finally:
    sources.imaplib.IMAP4_SSL = _real_imap
    sources.settings.MAIL_STATE_FILE.unlink(missing_ok=True)
    sources.settings.MAIL_STATE_FILE = _real_state

# ── Notion Status as a "status" property, not a "select" ──
def fake_db(status_kind):
    db = notion_db.NotionDB.__new__(notion_db.NotionDB)
    db.offline, db.dry_run = True, True
    db.schema = {"Company": "title", "Job Title": "rich_text", "Status": status_kind,
                 "Reference Link": "url", "Score": "number", "Track": "select",
                 "Type": "select", "Location": "select", "Date Found": "date"}
    db.title_prop = "Company"
    return db

scored_job = job("PR Coordinator")
scored_job.update({"total": 8.1, "track": "PR / Comms", "loc_option": "New York"})
for kind in ("status", "select"):
    props = fake_db(kind).build_properties(scored_job, status="Suggested")
    check(props.get("Status") == {kind: {"name": "Suggested"}},
          f"Status written as a {kind} property ({props.get('Status')})")
check(notion_db.NotionDB._plain({"type": "status", "status": {"name": "Queue"}}) == "Queue",
      "a status property is read back")
check(notion_db.NotionDB._plain({"type": "select", "select": {"name": "Queue"}}) == "Queue",
      "a select property is still read back")
check(fake_db("rich_text").build_properties(scored_job, status="Suggested").get("Status") is None,
      "a Status of the wrong type is left alone")


# ── A letter whose JSON a model broke is repaired, not thrown away ──
check(scoring.parse_json('{"letter": "one"}') == {"letter": "one"}, "ordinary JSON still parses")
_loose = '{"letter": "Dear team,\n\nSecond paragraph.", "cv_emphasis": ["a"]}'
_fixed = scoring.parse_json(_loose)
check(isinstance(_fixed, dict) and _fixed["letter"].count("\n") == 2,
      "raw line breaks inside the letter are repaired, keeping the paragraphs")
check(scoring.parse_json('{"a": "he said \\"hi\\" to me"}') == {"a": 'he said "hi" to me'},
      "an escaped quote inside a string is left alone")
check(scoring.parse_json("not json at all") is None, "and real rubbish is still refused")
check(scoring.parse_json('```json\n{"letter": "x"}\n```') == {"letter": "x"},
      "a fenced reply is unwrapped")

# ── Cover letters: her own notes, branding, PDF ──
import branding          # noqa: E402
import letter_pdf        # noqa: E402
import writer            # noqa: E402

page_body = """## Fit
- Why it fits: fashion brand on the business side.
## My Cover Letter draft
I have a strong passion for the fashion industry and branding.
Maybe I should link my assignments in the cover letter.
## Resume changes
- Replace Boston, MA with your Belgian city.
"""
notes = writer.her_draft(page_body)
check("passion for the fashion industry" in notes, "her own section is read")
check("link my assignments" in notes, "her instructions to the writer are kept")
check("Belgian city" not in notes, "the resume section is not swept in")
check("business side" not in notes, "the agent's own Fit section is not swept in")
check(writer.her_draft("## Notes\nnothing of hers here") == "", "no section means no notes")

# The heading she actually used was "Cover Letter Draft", not "My cover letter",
# so her notes were skipped and the letter was written from the profile alone.
_real_page = """## Why it fits
- Ran 15+ events a year, 80-120 attendees.
### What the role really needs
- Press outreach: managed 20-25 corporate partners.
# Cover Letter Draft
I have a passion to help and promote brands through communications.
I am also very open to explore other kind of positions or internships.
## ✍️ Cover letter draft 1 · 02 Oct 2026
Lucien Pages' roster of established luxury houses is exactly the mix I want.
### Put these CV points first
- Emergent Leuven: 15+ events a year
### Answer these yourself before sending
- Any personal connection worth naming?
"""
_hers = writer.her_draft(_real_page)
check("open to explore other kind of positions" in _hers,
      "a heading of hers called 'Cover Letter Draft' is read")
check("roster of established" not in _hers, "the agent's own letter is not read back as her voice")
check("Ran 15+ events a year, 80-120" not in _hers, "nor is the agent's Why it fits")
check("Emergent Leuven" not in _hers, "nor its CV points")
check("Any personal connection" not in _hers, "nor its questions")
check(writer.her_draft("## Motivatie\nIk wil graag bij dit merk werken.") != "",
      "a Dutch heading is read too")

check(branding._plausible_site("https://www.christianwijnants.com", "Christian Wijnants"),
      "a company's own site is recognised")
check(not branding._plausible_site("https://sspcs.recruitee.com/o/x", "Suspicious Antwerp"),
      "an applicant tracking host is not the company")
check(not branding._plausible_site("https://fashionunited.be/x", "Comme ça"),
      "nor is the job board it was listed on")
check(not branding._plausible_site("https://www.linkedin.com/jobs/view/1", "Lacoste"),
      "nor LinkedIn")
check(branding.find_website("", "", "") == "", "no company name means no lookup")
check(branding.find_website("Lucien Pagès Communication", "",
                            "Apply at https://www.lucienpages.com/careers today")
      == "https://www.lucienpages.com", "the site is taken from the posting text")

check(branding.parse_color("#C8102E") == (200, 16, 46), "hex colour parsed")
check(branding.parse_color("#abc") == (170, 187, 204), "short hex expanded")
check(branding.parse_color("rgba(1,2,3,0)") is None, "a transparent colour is not a brand colour")
check(branding.parse_color("teal") is None, "an unparseable colour is skipped")
check(branding.is_usable_accent((200, 16, 46)), "a saturated red is usable as an accent")
check(not branding.is_usable_accent((255, 255, 255)), "white is not an accent")
check(not branding.is_usable_accent((138, 138, 138)), "grey is not an accent")
sample_css = (":root{--color-brand:#C8102E;--grey-500:#8A8A8A}"
              "body{color:#111;font-family:'Söhne',Arial,sans-serif}"
              "h1{font-family:'Canela Deck',Georgia,serif}")
scored = branding._colour_candidates(sample_css)
check(max(scored, key=scored.get) == (200, 16, 46), "the declared brand colour outranks the greys")
check(branding._font_candidates(sample_css)[0] == "söhne", "the body family is found first")
check(branding.classify_font("canela deck") is True, "a serif family is recognised")
check(branding.classify_font("söhne") is False, "a sans family is recognised")
check(branding.brand_for("", "X")["accent"] == branding.NEUTRAL["accent"], "no website falls back")

# ── When the CSS says nothing: the brand's own icon does ──
check(branding._abs("/x.png", "https://a.be") == "https://a.be/x.png", "a root link is absolute")
check(branding._abs("//cdn.a.be/x.png", "https://a.be") == "https://cdn.a.be/x.png",
      "a protocol-relative link gets a scheme")
check(branding._abs("https://b.be/x.png", "https://a.be") == "https://b.be/x.png",
      "an absolute link is left as it is")
_mask = '<link rel="mask-icon" href="/pin.svg" color="#E7792B">'
check(branding._declared_icon_colour(_mask) == (231, 121, 43), "a mask icon states the colour outright")
check(branding._declared_icon_colour('<link rel="mask-icon" href="/p.svg" color="#FFFFFF">') is None,
      "a white mask icon is not a brand colour")
_icons = ('<link rel="icon" sizes="32x32" href="/small.png">'
          '<link rel="apple-touch-icon" sizes="180x180" href="/big.png">'
          '<link rel="icon" href="/vector.svg">')
_urls = branding._icon_urls(_icons, "https://a.be")
check(_urls[0] == "https://a.be/big.png", "the apple touch icon is tried first")
check("https://a.be/vector.svg" not in _urls, "an SVG icon is skipped, having no pixels to read")
check(_urls[-1] == "https://a.be/favicon.ico", "and plain /favicon.ico is the last resort")

try:
    from PIL import Image
    import io as _io

    _img = Image.new("RGBA", (32, 32), (231, 121, 43, 255))
    for _x in range(32):                      # a white stripe, which must not win
        for _y in range(8):
            _img.putpixel((_x, _y), (255, 255, 255, 255))
    _buf = _io.BytesIO()
    _img.save(_buf, format="PNG")

    class _Icon:
        status_code = 200
        content = _buf.getvalue()

    _real_get = sources.http_get
    try:
        sources.http_get = lambda url, **kw: _Icon()
        check(branding._favicon_colour('<link rel="icon" href="/f.png">', "https://a.be")
              == (224, 120, 40), "the favicon's own colour is read off its pixels")
    finally:
        sources.http_get = _real_get
except ImportError:                           # Pillow missing must cost a colour, not a letter
    check(branding._favicon_colour('<link rel="icon" href="/f.png">', "https://a.be") is None,
          "without Pillow the favicon is simply skipped")

_profile_contact = """### Contact
Put based in Ghent if the listing is in Belgium, if the listing is in New York, put new York.
Tel US: +1 (617) 251 8864
Tel BE: +324 963 946 33
Mail: phaedra.imschoot@gmail.com
"""
_found = letter_pdf.phones_from_profile(_profile_contact)
check(_found.get("us") == "+1 (617) 251 8864", "the US number is read from the profile")
check(_found.get("be") == "+324 963 946 33", "the Belgian number is read from the profile")
check(_found.get("email") == "phaedra.imschoot@gmail.com", "the email is read from the profile")
check(letter_pdf.phones_from_profile("nothing here") == {}, "a profile without a Contact section is fine")
check(letter_pdf.sender_for("New York", _profile_contact)["region"] == "us",
      "a New York row uses the US block")
for _loc in ("Ghent", "Antwerp", "Paris", "Amsterdam", "Belgium – other"):
    check(letter_pdf.sender_for(_loc, _profile_contact)["region"] == "be",
          f"a {_loc} row uses the Belgian block")
check(letter_pdf.sender_for("Remote", _profile_contact)["region"] == "be",
      "Remote falls back to the default region")
check(letter_pdf.sender_for("", _profile_contact)["region"] == "be",
      "a row with no location falls back too")
check(letter_pdf.sender_for("New York", _profile_contact)["location"] == "New York",
      "the US block says New York")
check(letter_pdf.sender_for("Ghent", _profile_contact)["location"] == "Ghent",
      "the Belgian block says Ghent")
check(letter_pdf.sender_for("Ghent", "")["phone"].startswith("+32"),
      "config supplies the number when the profile has none")

# ── Typeface: the company's own, or the nearest one that can be embedded ──
check(not branding.is_usable_accent((0, 122, 255)), "a platform blue is not a brand colour")
check(not branding.is_usable_accent((10, 102, 194)), "LinkedIn blue is not a brand colour either")
check(branding.is_usable_accent((231, 121, 43)), "an orange still is")
check(branding.site_root("https://www.hermes.com/?utm_source=google&gclid=abc")
      == "https://www.hermes.com", "a campaign tail is stripped off a stored website")
check(branding.site_root("https://x.be/fr/careers") == "https://x.be",
      "and a deep link comes back to the home page, where the brand lives")

check(letter_pdf.closest_font("Didot HTF", True) == "Playfair Display", "Didot maps to Playfair")
check(letter_pdf.closest_font("Söhne Buch", False) == "Inter", "Söhne maps to Inter")
check(letter_pdf.closest_font("Futura PT", False) == "Jost", "Futura maps to Jost")
check(letter_pdf.closest_font("Canela Text", True) == "Lora", "Canela maps to Lora")
check(letter_pdf.closest_font("", True) == "EB Garamond", "an unknown serif gets a serif default")
check(letter_pdf.closest_font("Unknowable Grotesk Neue", False) == "Space Grotesk",
      "an unknown family still matches on a word it contains")
check(letter_pdf.closest_font("Nothing Familiar", False) == "Inter", "otherwise the sans default")

_real_dl, _asked = letter_pdf._download_font, []


def _no_font(family, bold):
    _asked.append((family, bold))
    return None                     # the fetch failed, as it will with no network


try:
    letter_pdf._download_font = _no_font
    _f = letter_pdf.fonts_for({"serif": True, "font_name": "Bodoni Moda"})
    check(_asked and _asked[0][0] == "Playfair Display", "the matched family is the one fetched")
    check(_f["body"] == "Times-Roman" and _f["bold"] == "Times-Bold",
          "a failed download falls back to a built-in serif, never to no letter")
    check(_f["named"] == "", "and the letter does not claim a font it could not get")
    _asked.clear()
    letter_pdf.CFG["match_fonts"] = False
    letter_pdf.fonts_for({"serif": False, "font_name": "Futura"})
    check(_asked == [], "font matching can be switched off in config")
finally:
    letter_pdf._download_font = _real_dl
    letter_pdf.CFG["match_fonts"] = True

_bad = letter_pdf.FONT_CACHE / "JunkTest-Regular.ttf"
_bad.parent.mkdir(parents=True, exist_ok=True)
_bad.write_bytes(b"<!doctype html><html>not a font at all</html>" * 40)
try:
    letter_pdf._download_font("Junk Test", False)       # no network here, so this fails
    check(not _bad.exists(), "a cached file that is not a TTF is thrown away, not reused")
finally:
    _bad.unlink(missing_ok=True)

_pdf = Path("/tmp/job-agent-test-letter.pdf")
_pdf.unlink(missing_ok=True)
letter_pdf.build_letter_pdf(_pdf, "First paragraph.\n\nSecond paragraph with <angle> & ampersand.",
                            "Example Co", "Press Coordinator",
                            {"accent": "#C8102E", "ink": "#4C0611", "muted": "#C45D6E", "serif": False},
                            sender=letter_pdf.sender_for("New York", _profile_contact))
check(_pdf.exists() and _pdf.stat().st_size > 1000, "a letter renders to a real PDF")
check(_pdf.read_bytes()[:5] == b"%PDF-", "the file really is a PDF")
_pdf.unlink(missing_ok=True)
check(writer._letter_filename("Karla Otto", "Account Coordinator", 1).endswith(".pdf"),
      "letter filenames end in .pdf")
check("v2" in writer._letter_filename("Karla Otto", "Account Coordinator", 2),
      "a second letter is versioned")
check("/" not in writer._letter_filename("A/B Co", "Role", 1), "slashes never reach the filename")


# ── Rows filled in from a job description pasted into the page ──
_posting = ("Allocator\nHybrid or onsite, based in New York City, NY\n" +
            "Analyze sales, inventory and store performance to support allocation decisions. " * 8)
_pages = {
    "pasted": "# Job Description\n" + _posting + "\n## ✍️ Cover letter draft 1 · 02 Oct 2026\nMy letter text.",
    "notes": "# Notes Call\n" + "- they run face to face campaigns for NGOs and I liked the team\n" * 12,
    "failed": "W182698\nWorkforce Strategy Manager, Nutley\n" + _posting +
              f"\n{writer.FAIL_MARKER} automatically (the site may need a login or JavaScript).",
    "empty": "",
}
check("My letter text" not in writer.pasted_posting(_pages["pasted"], needs_heading=True),
      "the agent's own letter draft is not read as the posting")
check(writer.pasted_posting(_pages["notes"], needs_heading=True) == "",
      "interview notes on a row with no link are not a posting")
check(writer.pasted_posting("# Job Description\nshort", needs_heading=True) == "",
      "a few words are not a posting")


def _manual_row(key, url="", title=""):
    return {"id": key, "created": dt.datetime.now(dt.timezone.utc).isoformat(), "company": "Acme",
            "title": title, "url": url, "status": "To Apply", "score": None}


_db = fake_db("status")
_db.schema["Deadline"] = "date"
_written, _appended = {}, {}
_db.page_text = lambda page_id: _pages[page_id]
_db.update_properties = lambda page_id, props: _written.update({page_id: props}) or True
_db.append_blocks = lambda page_id, blocks: _appended.setdefault(page_id, []).extend(blocks) or True
_real_fetch, _real_score = sources.fetch_posting, scoring.deep_score
sources.fetch_posting = lambda url, source="": None
scoring.deep_score = lambda j, profile, taste: {
    "subscores": {"skills_fit": 7, "direction_fit": 9, "growth_onboarding": 8, "taste_match": 8,
                  "feasibility": 9},
    "track": "Fashion business", "title": "Allocator", "location": "New York City, NY",
    "deadline": "2026-10-01"}
try:
    _n = writer.enrich_manual_rows(_db, [
        _manual_row("pasted"), _manual_row("notes"),
        _manual_row("failed", url="https://careers.example.com/job/1", title="Workforce Strategy Manager"),
        _manual_row("empty", url="https://careers.example.com/job/2")], "profile", "taste")
finally:
    sources.fetch_posting, scoring.deep_score = _real_fetch, _real_score
check(_n == 2, f"two rows filled in from pasted text ({_n})")
check(_written["pasted"].get("Score", {}).get("number") is not None, "a row with no link gets a score")
check(_written["pasted"]["Track"] == {"select": {"name": "Fashion business"}}, "and a track")
check(_written["pasted"]["Location"] == {"select": {"name": "New York"}}, "and a location from the posting")
check(_written["pasted"]["Job Title"]["rich_text"][0]["text"]["content"] == "Allocator",
      "and a job title when the row had none")
check(_written["pasted"]["Deadline"] == {"date": {"start": "2026-10-01"}}, "and the deadline")
check("Reference Link" not in _written["pasted"], "no link is invented for a pasted row")
check("notes" not in _written and "notes" not in _appended, "a notes-only row is left untouched")
check("failed" in _written and "Job Title" not in _written["failed"],
      "an unreadable link is filled in from the pasted text, keeping her own title")
check("empty" not in _written and any(writer.FAIL_MARKER in str(b) for b in _appended.get("empty", [])),
      "an unreadable link with nothing pasted gets the note")

# ── Belgium keeps a share of the Suggested list ──
def _entry(score, loc, kind="new"):
    item = {"loc_option": loc} if kind == "new" else {"location": loc, "id": f"q{score}"}
    return {"score": score, "wild": False, "kind": kind, "item": item}


# Ten US rows all scoring above four Belgian ones: without a floor Belgium never appears.
_us = [_entry(9.5 - i * 0.1, "New York") for i in range(10)]
_be = [_entry(7.0 - i * 0.1, "Ghent") for i in range(4)]
_pool = sorted(_us + _be, key=lambda p: -p["score"])
_no_floor = main.keep_room_for_europe(_pool, 10, 0)
check(sum(1 for p in _no_floor if main._in_europe(p)) == 0, "without a floor, Belgium is crowded out")
_with_floor = main.keep_room_for_europe(_pool, 10, 4)
check(len(_with_floor) == 10, "the Suggested list stays the same size")
check(sum(1 for p in _with_floor if main._in_europe(p)) == 4, "four European rows are kept")
_kept_us = sorted(p["score"] for p in _with_floor if not main._in_europe(p))
check(_kept_us[0] > 8.9, "the US rows dropped are the weakest ones, not the best")
check(main.keep_room_for_europe(_pool, 10, 4)[0]["score"] == 9.5, "the top row is still the top row")

# Only two European candidates: take both, do not invent any.
_thin = sorted(_us + _be[:2], key=lambda p: -p["score"])
check(sum(1 for p in main.keep_room_for_europe(_thin, 10, 4) if main._in_europe(p)) == 2,
      "a thin European pool is not padded")
# Already enough Europe: nothing is swapped.
_euro_heavy = sorted([_entry(9.0 - i * 0.1, "Brussels") for i in range(6)] + _us[:4],
                     key=lambda p: -p["score"])
check(main.keep_room_for_europe(_euro_heavy, 10, 4) == _euro_heavy[:10],
      "an already European list is left alone")
check(main._in_europe(_entry(8.0, "Ghent", "queue")), "a Queue row's location counts too")
check(not main._in_europe(_entry(8.0, "New York")), "New York is not Europe")


# ── FashionUnited, the Belgian fashion board ──
FU_PAGE = """<html><body><ul>
<li class="card">
  <h2><a href="/fashion-jobs/stagiair-marketing-and-communication-antwerpen-2029577"
         title="Stagiair Marketing &amp; Communication">Stagiair Marketing &amp; Communication</a></h2>
  <a href="/fashion-jobs/werken-bij/bestseller-belgium-jobs">BESTSELLER</a>
  <p>Keizerstraat 9 | <a href="/fashion-jobs/in/antwerpen">Antwerpen</a>, Vlaanderen 2000</p>
</li>
<li class="card">
  <h2><a href="/fashion-jobs/office-and-operations-coordinator-antwerpen-1987612">Office &amp; Operations Coordinator</a></h2>
  <a href="/fashion-jobs/werken-bij/christian-wijnants-jobs">Christian Wijnants</a>
  <p>Italiëlei 215 | <a href="/fashion-jobs/in/antwerpen">Antwerpen</a>, Vlaanderen 2000</p>
</li>
<li class="card">
  <h2><a href="/fashion-jobs/verkoper-schelle-2029694">Verkoper</a></h2>
  <a href="/fashion-jobs/werken-bij/vanharen-jobs">vanHaren</a>
  <p><a href="/fashion-jobs/in/schelle">Schelle</a></p>
</li>
<li><a href="/fashion-jobs/in/gent">Jobs in Gent</a></li>
<li><a href="/fashion-jobs/werken-bij">Alle werkgevers</a></li>
</ul></body></html>"""


class _FUResponse:
    text = FU_PAGE


_real_get, _real_sleep = sources.http_get, sources.time.sleep
sources.http_get = lambda *a, **k: _FUResponse()
sources.time.sleep = lambda *a, **k: None
sources.CONFIG.setdefault("job_boards", {})["fashionunited"] = {
    "enabled": True, "base": "https://fashionunited.be",
    "cities": ["antwerpen"], "max_per_city": 20}
try:
    _fu = sources.search_fashionunited()
finally:
    sources.http_get, sources.time.sleep = _real_get, _real_sleep

check(len(_fu) == 3, f"three job cards parsed, navigation links ignored ({len(_fu)})")
_first = _fu[0]
check(_first["title"] == "Stagiair Marketing & Communication", "the title is unescaped")
check(_first["company"] == "BESTSELLER", "the employer comes from its own link")
check(_first["url"].startswith("https://fashionunited.be/fashion-jobs/"), "links are absolute")
check(_fu[1]["company"] == "Christian Wijnants", "a second card keeps its own employer")
check(filters.location_bucket(_first["location"])[0] == "Antwerp",
      f"the location buckets to Antwerp ({_first['location']!r})")
check(filters.hard_filter(_fu[1])[0], "a coordinator role passes the filters")
check(not filters.hard_filter(_fu[2])[0], "a Verkoper shop-floor role is dropped")
_off = dict(sources.CONFIG["job_boards"]["fashionunited"], enabled=False)
sources.CONFIG["job_boards"]["fashionunited"] = _off
check(sources.search_fashionunited() == [], "the board can be switched off in config")


# ── Applications that went quiet ──
def _row(rid, company, status, days_old, website="", url="", deadline=""):
    edited = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_old)).isoformat()
    return {"id": rid, "company": company, "title": "Coordinator", "status": status,
            "last_edited": edited, "website": website, "url": url, "deadline": deadline}


check(writer._domain_of({"website": "https://www.christianwijnants.com"}) == "christianwijnants.com",
      "the company domain is read from its website")
check(writer._domain_of({"website": "", "url": "https://sspcs.recruitee.com/o/role"}) == "",
      "an applicant tracking link is not mistaken for the company")
check(writer._domain_of({"website": "", "url": "https://fashionunited.be/fashion-jobs/x-123"}) == "",
      "nor is a job board")


class _ArchiveDB:
    def __init__(self):
        self.status, self.notes = [], []

    def set_status(self, page_id, status):
        self.status.append((page_id, status))
        return True

    def append_blocks(self, page_id, blocks):
        self.notes.append(page_id)
        return True


_rows = [
    _row("old-silent", "Christian Wijnants", "Applied", 30, website="https://christianwijnants.com"),
    _row("old-replied", "BrightAnalytics", "Applied", 30, website="https://brightanalytics.eu"),
    _row("recent", "Les Jumelles", "Applied", 3, website="https://lesjumelles.be"),
    _row("not-applied", "Comme ça", "To Apply", 60, website="https://commeca.be"),
    _row("interviewed", "Suspicious Antwerp", "Interviewed", 60, website="https://suspiciousantwerp.com"),
    _row("deadline-ahead", "Still Open NV", "Applied", 40,
         deadline=(dt.date.today() + dt.timedelta(days=20)).isoformat()),
    _row("deadline-passed", "Closed Already BV", "Applied", 40,
         deadline=(dt.date.today() - dt.timedelta(days=5)).isoformat()),
]
writer.CONFIG["archive_quiet"] = {"enabled": True, "after_days": 14,
                                  "statuses": ["Applied"], "archive_status": "Archived"}
_real_replies = sources.mail_replies_since
sources.mail_replies_since = lambda queries, since: {"old-replied"}
try:
    _db = _ArchiveDB()
    writer.archive_quiet_applications(_db, _rows)
    _archived = {pid for pid, _s in _db.status}
    check("deadline-ahead" not in _archived,
          "an application whose deadline has not passed is left alone")
    check("deadline-passed" in _archived, "one whose deadline has passed is archived")
    check(_archived == {"old-silent", "deadline-passed"},
          f"only the silent, stale, closed applications are archived ({_archived})")
    check(all(s == "Archived" for _p, s in _db.status), "each is set to Archived")
    check(set(_db.notes) == {"old-silent", "deadline-passed"},
          "and each gets a note saying why, so it can be undone")

    # A mail failure reports every company as having replied; nothing may move.
    sources.mail_replies_since = lambda queries, since: {k for k, _d, _n in queries}
    _db2 = _ArchiveDB()
    writer.archive_quiet_applications(_db2, _rows)
    check(_db2.status == [], "when the mailbox cannot be read, nothing is archived")

    # The preview writes nothing at all. With no replies, both stale ones qualify.
    sources.mail_replies_since = lambda queries, since: set()
    _db3 = _ArchiveDB()
    check(writer.archive_quiet_applications(_db3, _rows, dry_run=True) == 3,
          "the preview counts the stale applications when none replied")
    check(_db3.status == [] and _db3.notes == [], "the preview changes nothing")
finally:
    sources.mail_replies_since = _real_replies
    writer.CONFIG["archive_quiet"] = {"enabled": False, "after_days": 14,
                                      "statuses": ["Applied"], "archive_status": "Archived"}
check(writer.archive_quiet_applications(_ArchiveDB(), _rows) == 0, "switched off, it does nothing")
check(sources.mail_replies_since([], dt.date.today()) == set(), "no candidates means no mail check")


# ── The Company Watchlist page she keeps in Notion ──
class _LinkDB:
    def __init__(self, links): self.links = links
    def page_links(self, page_id): return self.links


_real_discover = sources.discover_board
sources.discover_board = lambda url: (
    ({"ats": "teamtailor", "slug": "careers.karlaotto.com", "eu": False}, "teamtailor:careers.karlaotto.com")
    if "karlaotto" in url else (None, "no job board the agent can read"))
main.CONFIG["notion"]["watchlist_page_id"] = "page-1"
try:
    _b = main.watchlist_page_boards(_LinkDB([
        "https://careers.karlaotto.com/jobs", "https://togethergroup.com/talent/"]))
    check(("teamtailor", "careers.karlaotto.com", False) in _b,
          "a careers page that resolves to a board is watched")
    check(len(_b) == 1, "one that cannot be read is not silently added")
    check(main.watchlist_page_boards(_LinkDB([])) == {}, "an empty page adds nothing")
    check(main.watchlist_page_boards(None) == {}, "no database means no page read")
    _all = main.watchlist_boards([], _LinkDB(["https://careers.karlaotto.com/jobs"]))
    check(("teamtailor", "careers.karlaotto.com", False) in _all,
          "page boards join the config watchlist and seeds")
finally:
    sources.discover_board = _real_discover

check(sources.ATS_IN_PAGE.search('<a href="https://job-boards.greenhouse.io/acme/jobs/1">Apply</a>'),
      "a greenhouse link inside a careers page is spotted")
check(sources.ATS_IN_PAGE.search("see https://acme.recruitee.com/o/role for details"),
      "so is a recruitee one")
check(not sources.ATS_IN_PAGE.search("https://example.com/about"),
      "an ordinary page link is not mistaken for a board")


# ── Looking the company up ──
check(branding._tld_preference("Ghent")[0] == ".be", "a Belgian row prefers the .be site")
check(branding._tld_preference("New York")[0] == ".com", "a New York row prefers the .com")
check(branding._tld_preference("Paris")[0] == ".fr", "a Paris row prefers the .fr")


class _Suggest:
    """Stands in for Clearbit's company autocomplete."""
    def __init__(self, items): self.items = items
    def json(self): return self.items


_real_get = sources.http_get
sources.http_get = lambda *a, **k: _Suggest([
    {"name": "Les Jumelles", "domain": "lesjumelles.ie"},
    {"name": "Les Jumelles", "domain": "lesjumelles.be"},
    {"name": "Les Jumelles Boutique", "domain": "lesjumellesboutique-shop.com"}])
try:
    check(branding._from_clearbit("Les Jumelles", (".be", ".eu", ".com")) == "https://lesjumelles.be",
          "the Belgian domain is chosen from several same-named matches")
    check(branding._from_clearbit("Les Jumelles", (".com",)) == "https://lesjumelles.ie",
          "with no preferred match it still returns an exact name match")
    sources.http_get = lambda *a, **k: _Suggest([])
    check(branding._from_clearbit("Nowhere Ltd", (".com",)) == "", "no suggestions means no site")
finally:
    sources.http_get = _real_get

check(branding.find_website("Lucien Pagès Communication", "",
                            "Apply at https://www.lucienpages.com/careers")
      == "https://www.lucienpages.com", "a site in the posting text wins before any lookup")
check(branding.find_website("X", "") == "", "too short a name is not looked up")

# A Brussels brand came back as a same-named .com belonging to someone else.
class _Page:
    def __init__(self, text): self.text = text


_real_get2 = sources.http_get
sources.http_get = lambda url, *a, **k: _Page("Welcome to Bellerose, Brussels") if "bellerose.be" in str(url) else None
try:
    check(branding._local_first("https://belle-rose.com", "Bellerose", (".be", ".com"))
          == "https://bellerose.be", "the local domain wins when the page really is theirs")
    sources.http_get = lambda *a, **k: None
    check(branding._local_first("https://belle-rose.com", "Bellerose", (".be", ".com"))
          == "https://belle-rose.com", "but only when it actually answers")
    check(branding._local_first("https://bellerose.be", "Bellerose", (".be",))
          == "https://bellerose.be", "an already-local match is left alone")
finally:
    sources.http_get = _real_get2


class _LookupDB:
    def __init__(self, text=""):
        self.props, self.blocks, self.text = {}, [], text

    def update_properties(self, page_id, props):
        self.props[page_id] = props
        return True

    def page_text(self, page_id):
        return self.text

    def append_blocks(self, page_id, blocks):
        self.blocks.append(page_id)
        return True


_lookup_rows = [
    {"id": "a", "company": "Christian Wijnants", "website": "", "status": "Suggested",
     "location": "Antwerp", "url": ""},
    {"id": "b", "company": "Already Known", "website": "https://known.com",
     "status": "Suggested", "location": "New York", "url": ""},
    {"id": "c", "company": "Dead Lead", "website": "", "status": "Not interested",
     "location": "New York", "url": ""},
    {"id": "d", "company": "", "website": "", "status": "Suggested", "location": "", "url": ""},
]
_real_find, _real_blurb = branding.find_website, writer.company_blurb
branding.find_website = lambda c, u="", d="", h="", location="": f"https://{c.split()[0].lower()}.be"
writer.company_blurb = lambda company, site: "They design womenswear in Antwerp.\nSignals: small team; independent house"
try:
    _ldb = _LookupDB()
    _n = writer.fill_company_websites(_ldb, _lookup_rows)
    check(_n == 1, f"only the row missing a website is looked up ({_n})")
    check("a" in _ldb.props and "b" not in _ldb.props, "a row that already has one is left alone")
    check("c" not in _ldb.props, "a Not interested row is skipped")
    check("d" not in _ldb.props, "a row with no company name is skipped")
    check(_ldb.props["a"]["Website"]["url"] == "https://christian.be", "the site is written to the row")
    check(_ldb.blocks == ["a"], "and a short note on what they do is added")

    _ldb2 = _LookupDB(text="🏷 About this company\nalready described")
    writer.fill_company_websites(_ldb2, _lookup_rows)
    check(_ldb2.blocks == [], "a company already described is not described twice")

    writer.CONFIG["company_lookup"] = dict(writer.CONFIG["company_lookup"], enabled=False)
    check(writer.fill_company_websites(_LookupDB(), _lookup_rows) == 0, "it can be switched off")
finally:
    branding.find_website, writer.company_blurb = _real_find, _real_blurb
    writer.CONFIG["company_lookup"] = dict(writer.CONFIG["company_lookup"], enabled=True)


print(f"All {passed} offline checks passed.")
