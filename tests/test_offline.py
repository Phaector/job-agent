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

print(f"All {passed} offline checks passed.")
