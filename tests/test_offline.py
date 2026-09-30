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
check(total == round(8 * .3 + 9 * .2 + 6 * .15 + 8 * .15 + 7 * .1 + 10 * .1, 1), f"total {total}")
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


print(f"All {passed} offline checks passed.")
