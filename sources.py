"""Where jobs come from.

1. Company job boards via their public JSON feeds (Greenhouse, Lever,
   Ashby, Workable, Recruitee). No scraping, no login.
2. Adzuna search API (US, Belgium, France, Netherlands).
3. Google Jobs via SerpApi (optional).
4. Job links inside newsletter emails (optional, via Gmail IMAP).
   Gmail is also read for companies answering her applications.
5. Reading a single posting from any link (for rows you add yourself).

Every source returns the same job dict, built by make_job().
"""
import datetime as dt
import email
import email.policy
import email.utils
import html
import imaplib
import json
import os
import re
import time
import urllib.parse
import urllib.robotparser
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

import settings
from settings import CONFIG

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9,nl;q=0.8,fr;q=0.7",
})

IMAP_TIMEOUT = 30          # never let an unreachable mail server hang a run


def annotate(level: str, message: str):
    """Report a result as a GitHub Actions annotation as well as on stdout.

    Run logs are served from a host the agent itself cannot reach, so an
    annotation is the only part of a run that can be read back through the
    API. level is notice, warning or error.
    """
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::{level}::{' '.join(str(message).split())}")
ADZUNA_CURRENCY = {"us": "USD", "be": "EUR", "fr": "EUR", "nl": "EUR"}


# ─── Small helpers ────────────────────────────────────────────────────────────
def clean(text) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(text or ""))).strip()


def html_to_text(raw) -> str:
    if not raw:
        return ""
    text = BeautifulSoup(html.unescape(str(raw)), "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def to_date(value):
    """Return YYYY-MM-DD from an ISO string or epoch (s or ms), else None."""
    if value in (None, "", 0):
        return None
    try:
        if isinstance(value, (int, float)):
            seconds = value / 1000 if value > 1e11 else value
            return dt.datetime.fromtimestamp(seconds, dt.timezone.utc).date().isoformat()
        text = str(value).strip()
        if re.match(r"^\d{4}-\d{2}-\d{2}", text):
            return text[:10]
        if "," in text and re.search(r"\d{4}", text):     # RFC 822, as RSS feeds use
            return email.utils.parsedate_to_datetime(text).date().isoformat()
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def relative_to_date(text: str):
    """'3 days ago' -> date string (used for Google Jobs)."""
    m = re.search(r"(\d+)\s*(hour|day|week|month)", (text or "").lower())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    days = {"hour": 0, "day": n, "week": 7 * n, "month": 30 * n}[unit]
    return (dt.date.today() - dt.timedelta(days=days)).isoformat()


def pretty_slug(slug: str) -> str:
    return re.sub(r"[-_]+", " ", slug or "").title()


def make_job(url, title, company="", location="", description="", posted_at=None,
             source="", salary=None, employment_type="", address="", deadline=""):
    return {
        "url": (url or "").strip(),
        "title": clean(title),
        "company": clean(company),
        "location": clean(location),
        "description": (description or "").strip(),
        "posted_at": to_date(posted_at),
        "source": source,
        "salary": salary,
        "employment_type": employment_type or "",
        "address": address or "",
        "deadline": to_date(deadline) or "",
        "applicants": None,
        "also_posted_on": [],
    }


def http_get(url, params=None, timeout=20, retries=2, **kw):
    for attempt in range(retries + 1):
        try:
            r = SESSION.get(url, params=params, timeout=timeout, **kw)
            if r.status_code == 429 and attempt < retries:
                time.sleep(5 * (attempt + 1))
                continue
            return r
        except requests.RequestException as e:
            if attempt == retries:
                print(f"    [http] {url[:90]}: {e}")
                return None
            time.sleep(2)
    return None


def get_json(url, params=None):
    r = http_get(url, params)
    if r is None:
        return None
    if r.status_code != 200:
        print(f"    [http {r.status_code}] {url[:90]}")
        return None
    try:
        return r.json()
    except ValueError:
        return None


# ─── ATS detection ────────────────────────────────────────────────────────────
def detect_ats(url: str):
    """Recognise company job-board links. Returns
    {"ats", "slug", "job_id", "eu"} or None."""
    if not url:
        return None
    p = urllib.parse.urlparse(url.strip())
    host = p.netloc.lower()
    parts = [x for x in p.path.split("/") if x]
    q = urllib.parse.parse_qs(p.query)

    if host.endswith("greenhouse.io"):
        if parts and parts[0] == "embed":
            slug = (q.get("for") or [None])[0]
            jid = (q.get("token") or [None])[0]
        elif parts:
            slug = parts[0]
            jid = parts[2] if len(parts) >= 3 and parts[1] == "jobs" else (q.get("gh_jid") or [None])[0]
        else:
            return None
        return {"ats": "greenhouse", "slug": slug, "job_id": jid, "eu": False} if slug else None
    if host in ("jobs.lever.co", "jobs.eu.lever.co") and parts:
        return {"ats": "lever", "slug": parts[0], "job_id": parts[1] if len(parts) > 1 else None,
                "eu": host.startswith("jobs.eu")}
    if host == "jobs.ashbyhq.com" and parts:
        return {"ats": "ashby", "slug": parts[0], "job_id": parts[1] if len(parts) > 1 else None, "eu": False}
    if host == "apply.workable.com" and parts and parts[0] not in ("api", "j"):
        jid = parts[2] if len(parts) >= 3 and parts[1] == "j" else None
        return {"ats": "workable", "slug": parts[0], "job_id": jid, "eu": False}
    if host.endswith(".workable.com") and host not in ("www.workable.com", "apply.workable.com"):
        jid = parts[1] if len(parts) >= 2 and parts[0] == "j" else None
        return {"ats": "workable", "slug": host.split(".")[0], "job_id": jid, "eu": False}
    if host.endswith(".recruitee.com"):
        jid = parts[1] if len(parts) >= 2 and parts[0] == "o" else None
        return {"ats": "recruitee", "slug": host.split(".")[0], "job_id": jid, "eu": False}
    # Teamtailor: <name>.teamtailor.com, or a careers host whose job paths look
    # like /jobs/8462906-consolidation-assistant (its distinctive id-slug form).
    tt_path = len(parts) >= 2 and parts[0] == "jobs" and re.match(r"^\d{5,}-", parts[1])
    if host.endswith(".teamtailor.com") or (host.startswith("careers.") and tt_path):
        return {"ats": "teamtailor", "slug": host.removeprefix("www."),
                "job_id": parts[1] if tt_path else None, "eu": False}
    return None


# ─── Company boards ───────────────────────────────────────────────────────────
def _gh_job(j, slug, company):
    return make_job(
        url=j.get("absolute_url", ""), title=j.get("title", ""),
        company=company or j.get("company_name") or pretty_slug(slug),
        location=(j.get("location") or {}).get("name", ""),
        description=html_to_text(j.get("content", "")),
        posted_at=j.get("first_published") or j.get("updated_at"),
        source="Company board (Greenhouse)")


def _lever_job(j, slug, company):
    cats = j.get("categories") or {}
    lists = " ".join(f"{l.get('text', '')}: {html_to_text(l.get('content', ''))}"
                     for l in (j.get("lists") or []))
    desc = " ".join(filter(None, [j.get("descriptionPlain", ""), lists, j.get("additionalPlain", "")]))
    loc = cats.get("location") or ", ".join(cats.get("allLocations") or [])
    if j.get("workplaceType") == "remote":
        loc = f"{loc} (Remote)".strip()
    return make_job(
        url=j.get("hostedUrl", ""), title=j.get("text", ""), company=company or pretty_slug(slug),
        location=loc, description=desc, posted_at=j.get("createdAt"),
        source="Company board (Lever)", employment_type=cats.get("commitment", ""))


def board_greenhouse(slug, company=None, eu=False):
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", {"content": "true"})
    items = data.get("jobs", []) if isinstance(data, dict) else []
    return [_gh_job(j, slug, company) for j in items if isinstance(j, dict)]


def board_lever(slug, company=None, eu=False):
    base = "https://api.eu.lever.co" if eu else "https://api.lever.co"
    data = get_json(f"{base}/v0/postings/{slug}", {"mode": "json"})
    items = data if isinstance(data, list) else []
    return [_lever_job(j, slug, company) for j in items if isinstance(j, dict)]


def board_ashby(slug, company=None, eu=False):
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", {"includeCompensation": "true"})
    items = data.get("jobs", []) if isinstance(data, dict) else []
    jobs = []
    for j in items:
        loc = j.get("location", "") or ""
        if j.get("isRemote"):
            loc = f"{loc} (Remote)".strip()
        jobs.append(make_job(
            url=j.get("jobUrl", ""), title=j.get("title", ""), company=company or pretty_slug(slug),
            location=loc, description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml", "")),
            posted_at=j.get("publishedAt"), source="Company board (Ashby)",
            employment_type=j.get("employmentType", "")))
    return jobs


def _workable_location(j):
    locs = j.get("locations") or ([j["location"]] if isinstance(j.get("location"), dict) else [])
    if locs:
        first = locs[0] or {}
        text = ", ".join(filter(None, [first.get("city"), first.get("region"), first.get("country")]))
    else:
        text = ", ".join(filter(None, [j.get("city"), j.get("state"), j.get("country")]))
    if j.get("telecommuting"):
        text = f"{text} (Remote)".strip()
    return text


def board_workable(slug, company=None, eu=False):
    data = get_json(f"https://apply.workable.com/api/v1/widget/accounts/{slug}", {"details": "true"})
    if not isinstance(data, dict):
        return []
    name = company or data.get("name") or pretty_slug(slug)
    jobs = []
    for j in data.get("jobs", []) or []:
        url = j.get("url") or j.get("shortlink") or f"https://apply.workable.com/{slug}/j/{j.get('shortcode', '')}/"
        desc = html_to_text(j.get("full_description") or j.get("description") or "")
        jobs.append(make_job(
            url=url, title=j.get("title", ""), company=name, location=_workable_location(j),
            description=desc, posted_at=j.get("published_on") or j.get("created_at"),
            source="Company board (Workable)", employment_type=j.get("employment_type", "")))
    return jobs


def board_recruitee(slug, company=None, eu=False):
    data = get_json(f"https://{slug}.recruitee.com/api/offers/")
    items = data.get("offers", []) if isinstance(data, dict) else []
    jobs = []
    for o in items:
        loc = o.get("location") or ", ".join(filter(None, [o.get("city"), o.get("country")]))
        jobs.append(make_job(
            url=o.get("careers_url") or o.get("url", ""), title=o.get("title", ""),
            company=company or o.get("company_name") or pretty_slug(slug), location=loc,
            description=html_to_text((o.get("description") or "") + " " + (o.get("requirements") or "")),
            posted_at=o.get("published_at") or o.get("created_at"), source="Company board (Recruitee)"))
    return jobs


TT_NS = {"tt": "https://teamtailor.com/locations"}


def _tt_values(node, prefer=("city", "name")):
    """Text under a Teamtailor element, preferring the tags that name a place.

    A filled locations element looks like
      <tt:locations><tt:location><tt:name>New York - Park Ave S</tt:name>
      <tt:address/><tt:zip/><tt:city>New York</tt:city><tt:country/></tt:location>
    and is written empty on some postings, so this reads whatever is there:
    the city (or name) tags when present, otherwise all text and attributes.
    """
    if node is None:
        return []
    for tag in prefer:
        hits = [clean(el.text) for el in node.iter()
                if el.tag.rsplit("}", 1)[-1] == tag and clean(el.text)]
        if hits:
            return list(dict.fromkeys(hits))
    out = []
    for el in node.iter():
        out += [el.text] + list(el.attrib.values())
    return list(dict.fromkeys(clean(v) for v in out if clean(v)))


def _one_known_city(text: str):
    """A city from the config, but only when the text names exactly one.

    Used when a posting carries no location of its own: a description that
    mentions several offices is left alone rather than guessed at.
    """
    import filters
    hits = {name for name, _tier, rx in filters.LOCATION_RX if rx.search(text or "")}
    return hits.pop() if len(hits) == 1 else ""


def board_teamtailor(slug, company=None, eu=False):
    """Teamtailor's public RSS feed. Used by fashion PR and creative agencies.

    The slug is either a careers host ("careers.karlaotto.com") or a
    Teamtailor subdomain name ("karlaotto" -> karlaotto.teamtailor.com).
    """
    host = slug if "." in slug else f"{slug}.teamtailor.com"
    r = http_get(f"https://{host}/jobs.rss")
    if not r:
        return []
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError as e:
        print(f"    [teamtailor:{slug}] feed is not XML: {e}")
        return []
    channel_name = clean((root.findtext("./channel/title") or ""))
    jobs = []
    for item in root.iterfind("./channel/item"):
        desc = html_to_text(item.findtext("description") or "")
        location = ", ".join(_tt_values(item.find("tt:locations", TT_NS)))
        if not location:
            location = _one_known_city(f"{item.findtext('title') or ''} {desc[:1200]}")
        if not location and (item.findtext("remoteStatus") or "").lower() in ("fully", "remote"):
            location = "Remote"
        # tt:division names the agency inside a group feed (Karla Otto, K2, ...)
        division = ", ".join(_tt_values(item.find("tt:division", TT_NS), prefer=()))
        jobs.append(make_job(
            url=item.findtext("link") or "", title=item.findtext("title") or "",
            company=division or company or channel_name or pretty_slug(slug),
            location=location, description=desc,
            posted_at=item.findtext("pubDate"), source="Company board (Teamtailor)"))
    return jobs


BOARD_READERS = {
    "greenhouse": board_greenhouse, "lever": board_lever, "ashby": board_ashby,
    "workable": board_workable, "recruitee": board_recruitee,
    "teamtailor": board_teamtailor,
}


def fetch_board(ats, slug, company=None, eu=False):
    reader = BOARD_READERS.get(ats)
    if not reader or not slug:
        return []
    try:
        return reader(slug, company, eu)
    except Exception as e:  # one broken board must never stop the run
        print(f"    [{ats}:{slug}] {e}")
        return []


# ─── Search APIs ──────────────────────────────────────────────────────────────
def search_adzuna(country, what, where="", max_days_old=7, results=20):
    if not (settings.ADZUNA_APP_ID and settings.ADZUNA_APP_KEY):
        return []
    params = {
        "app_id": settings.ADZUNA_APP_ID, "app_key": settings.ADZUNA_APP_KEY,
        "what": what, "results_per_page": results, "max_days_old": max_days_old,
        "sort_by": "date", "content-type": "application/json",
    }
    if where:
        params["where"] = where
    data = get_json(f"https://api.adzuna.com/v1/api/jobs/{country}/search/1", params)
    jobs = []
    for r in (data or {}).get("results", []) or []:
        salary = None
        if r.get("salary_min") and str(r.get("salary_is_predicted", "0")) == "0":
            salary = {"min": r.get("salary_min"), "max": r.get("salary_max"),
                      "currency": ADZUNA_CURRENCY.get(country, "")}
        job = make_job(
            url=r.get("redirect_url", ""), title=html_to_text(r.get("title", "")),
            company=(r.get("company") or {}).get("display_name", ""),
            location=(r.get("location") or {}).get("display_name", ""),
            description=html_to_text(r.get("description", "")), posted_at=r.get("created"),
            source="Adzuna", salary=salary, employment_type=r.get("contract_time", ""))
        job["description_is_snippet"] = True
        jobs.append(job)
    return jobs


AGGREGATOR_HINTS = ("linkedin.", "indeed.", "glassdoor.", "ziprecruiter.", "talent.com", "jooble.", "adzuna.")


def search_serpapi(q, location, gl="us"):
    if not settings.SERPAPI_KEY:
        return []
    data = get_json("https://serpapi.com/search.json", {
        "engine": "google_jobs", "q": q, "location": location, "gl": gl, "hl": "en",
        "api_key": settings.SERPAPI_KEY})
    jobs = []
    for r in (data or {}).get("jobs_results", []) or []:
        options = r.get("apply_options") or []
        direct = [o for o in options if not any(h in (o.get("link") or "") for h in AGGREGATOR_HINTS)]
        pick = (direct or options or [{}])[0]
        link = pick.get("link") or r.get("share_link") or ""
        ext = r.get("detected_extensions") or {}
        job = make_job(
            url=link, title=r.get("title", ""), company=r.get("company_name", ""),
            location=r.get("location", ""), description=r.get("description", ""),
            posted_at=relative_to_date(ext.get("posted_at", "")),
            source=f"Google Jobs ({r.get('via', '').replace('via ', '')})".replace(" ()", ""),
            employment_type=ext.get("schedule_type", ""))
        job["also_posted_on"] = [{"source": o.get("title", ""), "url": o.get("link", "")}
                                 for o in options if o.get("link") and o.get("link") != link][:5]
        jobs.append(job)
    return jobs


# ─── Reading one posting from any link ───────────────────────────────────────
ROBOTS_CACHE = {}


def robots_allows(url: str) -> bool:
    """Whether a site's robots.txt lets us read this page.

    Careers pages pasted into the watchlist are fetched to work out which
    applicant tracking system sits behind them, and some of those sites say
    no. A site that cannot be checked is treated as allowed, which is the
    usual reading, but one that says no is left alone.
    """
    m = re.match(r"(https?://[^/]+)", url or "")
    if not m:
        return False
    base = m.group(1)
    if base not in ROBOTS_CACHE:
        parser = urllib.robotparser.RobotFileParser()
        r = http_get(f"{base}/robots.txt", retries=0, timeout=10)
        try:
            parser.parse((r.text if r is not None else "").splitlines())
        except Exception:
            ROBOTS_CACHE[base] = None
            return True
        ROBOTS_CACHE[base] = parser
    parser = ROBOTS_CACHE[base]
    if parser is None:
        return True
    try:
        return parser.can_fetch(SESSION.headers.get("User-Agent", "*"), url)
    except Exception:
        return True


ATS_IN_PAGE = re.compile(
    r"https?://(?:[\w-]+\.)?(?:boards\.greenhouse\.io|job-boards\.greenhouse\.io|"
    r"jobs\.lever\.co|jobs\.ashbyhq\.com|apply\.workable\.com|[\w-]+\.recruitee\.com|"
    r"[\w-]+\.teamtailor\.com|careers\.[\w.-]+/jobs)[^\s\"'<>)]*", re.I)


def discover_board(url: str):
    """Work out which job board sits behind a careers page.

    She pastes the page she browses, not a feed, so a link is first read as
    an applicant tracking system directly and, failing that, the page is
    fetched and searched for one. Returns (info, note) where info is what
    detect_ats returns, or None with a note saying why not.
    """
    info = detect_ats(url)
    if info and info.get("slug"):
        return info, f"{info['ats']}:{info['slug']}"
    if not robots_allows(url):
        return None, "the site's robots.txt asks us not to read it"
    r = http_get(url, retries=1, timeout=15)
    if r is None or not getattr(r, "text", ""):
        return None, "the page could not be read"
    for found in ATS_IN_PAGE.findall(r.text):
        info = detect_ats(found)
        if info and info.get("slug"):
            return info, f"{info['ats']}:{info['slug']} (found on the page)"
    return None, "no job board the agent can read; the roles are probably loaded by JavaScript"


# ─── Job boards (not tied to one company) ────────────────────────────────────
# FashionUnited lists fashion jobs per country. Every Belgian fashion role
# Phaedra has actually wanted came from here rather than from Adzuna, which
# returns mostly generic tech and recruitment for Belgium.
#
# There is no feed, so the listing pages are read. The parser keys off the
# site's URL shapes rather than its class names, which survive a redesign:
#   a job     /fashion-jobs/<slug>-<numeric id>
#   a company /fashion-jobs/werken-bij/<name>-jobs
#   a city    /fashion-jobs/in/<city>
FU_JOB_RX = re.compile(r"^/fashion-jobs/[^/]+?-(\d{5,})/?$")
FU_COMPANY_RX = re.compile(r"^/fashion-jobs/werken-bij/")
FU_CITY_RX = re.compile(r"^/fashion-jobs/in/")


def _fu_card(anchor):
    """Walk up from a job link until the element also holds its company link."""
    node = anchor
    for _ in range(5):
        node = node.parent
        if node is None:
            return anchor
        if node.find("a", href=FU_COMPANY_RX):
            return node
    return anchor


def search_fashionunited():
    cfg = (CONFIG.get("job_boards") or {}).get("fashionunited") or {}
    if not cfg.get("enabled"):
        return []
    base = (cfg.get("base") or "https://fashionunited.be").rstrip("/")
    jobs, seen = [], set()
    for city in cfg.get("cities") or []:
        r = http_get(f"{base}/fashion-jobs/in/{city}", retries=1)
        if r is None or not getattr(r, "text", ""):
            print(f"  [FashionUnited] could not read {city}")
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        found = 0
        for link in soup.find_all("a", href=FU_JOB_RX):
            url = urllib.parse.urljoin(base, link["href"])
            title = clean(link.get("title") or link.get_text(" ", strip=True))
            if not title or url in seen:
                continue
            card = _fu_card(link)
            employer = card.find("a", href=FU_COMPANY_RX)
            where = card.find("a", href=FU_CITY_RX)
            seen.add(url)
            jobs.append(make_job(
                url=url, title=title,
                company=clean(employer.get_text(" ", strip=True)) if employer else "",
                location=f"{clean(where.get_text(' ', strip=True)) if where else city}, Belgium",
                description="", posted_at=None, source="FashionUnited"))
            found += 1
            if found >= int(cfg.get("max_per_city", 20)):
                break
        print(f"  [FashionUnited] {city}: {found}")
        time.sleep(0.8)          # a courtesy gap, the site sets no crawl delay
    return jobs


# ─── Reading one posting from any link ───────────────────────────────────────
def _jsonld_posting(soup):
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except (ValueError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        expanded = []
        for it in items:
            if isinstance(it, dict) and isinstance(it.get("@graph"), list):
                expanded.extend(it["@graph"])
            else:
                expanded.append(it)
        for it in expanded:
            if isinstance(it, dict) and "JobPosting" in str(it.get("@type", "")):
                return it
    return None


def _jsonld_to_job(it, url, source):
    org = it.get("hiringOrganization")
    company = org.get("name", "") if isinstance(org, dict) else (org or "")
    loc = it.get("jobLocation")
    if isinstance(loc, list):
        loc = loc[0] if loc else {}
    addr = (loc or {}).get("address", {}) if isinstance(loc, dict) else {}
    if not isinstance(addr, dict):
        addr = {}
    country = addr.get("addressCountry")
    if isinstance(country, dict):
        country = country.get("name", "")
    location = ", ".join(filter(None, [addr.get("addressLocality"), addr.get("addressRegion"), country]))
    address = ", ".join(filter(None, [addr.get("streetAddress"), addr.get("postalCode"),
                                      addr.get("addressLocality"), country]))
    if "TELECOMMUTE" in str(it.get("jobLocationType", "")).upper():
        location = f"{location} (Remote)".strip()
    salary = None
    base = it.get("baseSalary")
    if isinstance(base, dict):
        val = base.get("value") if isinstance(base.get("value"), dict) else {}
        lo, hi = val.get("minValue") or val.get("value"), val.get("maxValue")
        if lo:
            salary = {"min": lo, "max": hi, "currency": base.get("currency", "")}
    return make_job(
        url=it.get("url") or url, title=it.get("title", ""), company=company, location=location,
        description=html_to_text(it.get("description", ""))[:12000], posted_at=it.get("datePosted"),
        source=source, salary=salary, employment_type=str(it.get("employmentType", "")),
        address=address, deadline=it.get("validThrough"))


def fetch_posting(url, source="Added by you"):
    """Read a single job posting from any link. Returns a job dict or None."""
    info = detect_ats(url)
    if info and info.get("job_id"):
        jid, slug = info["job_id"], info["slug"]
        if info["ats"] == "greenhouse":
            j = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{jid}")
            if isinstance(j, dict) and j.get("title"):
                job = _gh_job(j, slug, None)
                job["source"] = source
                return job
        elif info["ats"] == "lever":
            base = "https://api.eu.lever.co" if info.get("eu") else "https://api.lever.co"
            j = get_json(f"{base}/v0/postings/{slug}/{jid}")
            if isinstance(j, dict) and j.get("text"):
                job = _lever_job(j, slug, None)
                job["source"] = source
                return job
        else:
            for job in fetch_board(info["ats"], slug, eu=info.get("eu", False)):
                if jid.lower() in job["url"].lower():
                    job["source"] = source
                    return job

    r = http_get(url, timeout=20)
    if r is None or r.status_code >= 400:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    posting = _jsonld_posting(soup)
    if posting:
        job = _jsonld_to_job(posting, r.url, source)
        if job["title"]:
            job["url"] = url
            return job

    def meta(prop):
        tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        return tag.get("content", "") if tag else ""

    title = meta("og:title") or (soup.h1.get_text(" ", strip=True) if soup.h1 else "") or \
        (soup.title.get_text(" ", strip=True) if soup.title else "")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        tag.decompose()
    body = soup.select_one("main, article, [class*='description'], [id*='description']") or soup.body
    text = re.sub(r"\s+", " ", body.get_text(" ", strip=True)) if body else ""
    if not title:
        return None
    return make_job(url=url, title=title, company=meta("og:site_name"), description=text[:12000], source=source)


# ─── Newsletter emails (Gmail IMAP) ──────────────────────────────────────────
JOB_HOST_HINTS = ("greenhouse.io", "lever.co", "ashbyhq.com", "workable.com", "recruitee.com",
                  "smartrecruiters.com", "myworkdayjobs.com", "rippling.com", "bamboohr.com",
                  "teamtailor.com", "jobvite.com", "icims.com", "successfactors", "welcometothejungle.com",
                  "linkedin.com/jobs", "linkedin.com/comm/jobs", "glassdoor", "indeed.",
                  "wellfound.com", "builtinnyc.com")
SKIP_HINTS = ("unsubscribe", "substack.com/app", "substack.com/profile", "/subscribe", "substack.com/@",
              "substackcdn", "twitter.com", "x.com/", "instagram.com", "facebook.com", "tiktok.com",
              "mailto:", "apps.apple.com", "play.google.com", "/account", "/settings")
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def looks_like_job(url: str) -> bool:
    u = url.lower()
    if "substack.com" in u:
        return False
    if any(h in u for h in JOB_HOST_HINTS):
        return True
    return bool(re.search(r"/(careers?|jobs?|vacatures?|emplois?|positions?|openings?)(/|$|\?|-)", u))


def _html_part(msg):
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if part.get_content_type() == "text/html":
            payload = part.get_payload(decode=True) or b""
            return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    return ""


def tidy_link(url: str) -> str:
    """Strip tracking from links that arrive in alert emails.

    LinkedIn alerts link to /comm/jobs/view/<id>/?trackingId=... ; the plain
    /jobs/view/<id>/ form is the same posting and dedupes across alerts.
    """
    m = re.search(r"linkedin\.com/(?:comm/)?jobs/view/(\d+)", url)
    if m:
        return f"https://www.linkedin.com/jobs/view/{m.group(1)}/"
    return url


def resolve_link(url: str) -> str:
    try:
        r = SESSION.get(url, allow_redirects=True, timeout=15, stream=True)
        final = r.url
        r.close()
        return final
    except requests.RequestException:
        return url


def _mask(address: str) -> str:
    name, _, host = (address or "").partition("@")
    return f"{name[:1]}{'*' * max(len(name) - 1, 0)}@{host}" if host else "(not set)"


def mail_replies_since(queries, since_day):
    """Which of these companies have written to her since a given date.

    queries: list of (key, domain, company_name). The domain is matched
    against the sender; where a row has no website, the company name is
    matched anywhere in the message instead, which is looser but catches a
    reply sent through a recruiting platform.

    Returns the set of keys that have mail. On any failure it returns every
    key, so that a mail problem can never be read as "nobody replied" by
    something that archives on silence.
    """
    all_keys = {k for k, _d, _n in queries}
    if not queries:
        return set()
    if not (settings.GMAIL_ADDRESS and settings.GMAIL_APP_PASSWORD):
        print("  [Replies] No Gmail credentials, so no application is treated as silent")
        return all_keys
    since = f"{since_day.day:02d}-{MONTHS[since_day.month - 1]}-{since_day.year}"
    found = set()
    try:
        box = imaplib.IMAP4_SSL("imap.gmail.com", timeout=IMAP_TIMEOUT)
        box.login(settings.GMAIL_ADDRESS, settings.GMAIL_APP_PASSWORD)
        mailbox = _all_mail_box(box)
        if box.select(mailbox, readonly=True)[0] != "OK":
            box.select("INBOX", readonly=True)
        for key, domain, name in queries:
            term = f'FROM "{domain}"' if domain else f'TEXT "{_imap_quote(name)}"'
            if not domain and len(clean(name)) < 4:
                found.add(key)          # too short to match safely, so assume a reply
                continue
            status, data = box.search(None, f"(SINCE {since} {term})")
            if status != "OK":
                found.add(key)          # a search we cannot trust must not archive
                continue
            if data and data[0]:
                found.add(key)
        box.logout()
    except (imaplib.IMAP4.error, OSError) as e:
        print(f"  [Replies] Could not read Gmail ({e}), so no application is treated as silent")
        return all_keys
    return found


def _imap_quote(text: str) -> str:
    return clean(text).replace("\\", "").replace('"', "")


def _all_mail_box(box) -> str:
    """The All Mail folder, whatever this account's language calls it.

    Gmail marks it \\All in a LIST response, so the name is never guessed.
    This matters more than it looks: falling back to INBOX hides anything
    that has been filed under a label and archived, which is most of a
    tidy mailbox.
    """
    try:
        status, lines = box.list()
    except (imaplib.IMAP4.error, OSError):
        return "INBOX"
    if status != "OK":
        return "INBOX"
    for raw in lines or []:
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        m = re.match(r'\(([^)]*)\)\s+"[^"]*"\s+(.+)$', text.strip())
        if not m or "\\All" not in m.group(1):
            continue
        name = m.group(2).strip()
        return name if name.startswith('"') else f'"{name}"'
    return "INBOX"


def mail_probe():
    """Check the Gmail connection and report, without scoring anything.

    Answers three questions in order: are the credentials there, does the
    login work, and does each configured sender actually have mail in the
    lookback window. Costs nothing and writes nothing.
    """
    cfg = CONFIG["newsletter"]
    print("\n--- Gmail check ---")
    print(f"GMAIL_ADDRESS:      {_mask(settings.GMAIL_ADDRESS)}")
    pw = settings.GMAIL_APP_PASSWORD
    print(f"GMAIL_APP_PASSWORD: {f'set, {len(pw)} characters' if pw else 'NOT SET'}")
    if not (settings.GMAIL_ADDRESS and settings.GMAIL_APP_PASSWORD):
        missing = " and ".join(n for n, v in (("GMAIL_ADDRESS", settings.GMAIL_ADDRESS),
                                              ("GMAIL_APP_PASSWORD", settings.GMAIL_APP_PASSWORD)) if not v)
        annotate("error", f"Gmail is switched off: {missing} is not set. Add it under "
                          "Settings, Secrets and variables, Actions.")
        print(f"\nResult: the email source is switched off, because {missing} is missing.")
        return False
    try:
        box = imaplib.IMAP4_SSL("imap.gmail.com", timeout=IMAP_TIMEOUT)
    except OSError as e:
        annotate("error", f"Could not reach imap.gmail.com: {e}")
        print(f"\nResult: could not reach imap.gmail.com: {e}")
        return False
    try:
        box.login(settings.GMAIL_ADDRESS, settings.GMAIL_APP_PASSWORD)
        print("Login:              OK")
    except imaplib.IMAP4.error as e:
        pw = settings.GMAIL_APP_PASSWORD
        shape = (f"the secret is {len(pw)} characters"
                 f"{' with spaces' if ' ' in pw else ''}; a Google app password is 16 letters "
                 f"(19 if you keep the spaces)")
        annotate("error", f"Gmail refused the login for {_mask(settings.GMAIL_ADDRESS)}: {e}. "
                          f"{shape}. If that length looks like your normal account password, "
                          "that is the problem: Gmail only accepts an app password over IMAP. "
                          "Make one at myaccount.google.com/apppasswords.")
        print(f"Login:              FAILED ({e})")
        print(f"\nResult: Gmail refused the login. {shape}.")
        return False
    mailbox = _all_mail_box(box)
    if box.select(mailbox, readonly=True)[0] != "OK":
        print(f"Mailbox:            {mailbox} could not be opened, falling back to INBOX")
        print("                    (IMAP may be off: Gmail settings -> Forwarding and POP/IMAP)")
        mailbox = "INBOX"
        box.select(mailbox, readonly=True)
    print(f"Mailbox:            {mailbox}"
          + ("  <- only the inbox, so archived mail is not searched" if mailbox == "INBOX" else ""))
    since_day = dt.date.today() - dt.timedelta(days=cfg["lookback_days"])
    since = f"{since_day.day:02d}-{MONTHS[since_day.month - 1]}-{since_day.year}"
    print(f"Looking back to:    {since_day} ({cfg['lookback_days']} days)")
    seen_before = _read_mail_state()
    total, counts = 0, []
    for src in cfg.get("sources") or []:
        addr, name = src.get("from", ""), src.get("name") or src.get("from", "")
        status, data = box.search(None, f'(SINCE {since} FROM "{addr}")')
        nums = (data[0].split() if status == "OK" and data and data[0] else [])
        total += len(nums)
        counts.append(f"{name}: {len(nums)}")
        print(f"\n  {name} <{addr}>")
        print(f"    {len(nums)} emails in the window")
        for num in nums[-3:]:
            _, msg_data = box.fetch(num, "(BODY.PEEK[HEADER.FIELDS (SUBJECT DATE MESSAGE-ID)])")
            if not msg_data or not isinstance(msg_data[0], tuple):
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            key = clean(msg.get("Message-ID") or "")
            mark = "already read" if key in seen_before else "NEW, would be read"
            print(f"    - {clean(msg.get('Date'))[:25]} | {clean(msg.get('Subject'))[:60]} | {mark}")
    box.logout()
    per_sender = "; ".join(counts) or "no senders configured"
    summary = (f"Gmail works for {_mask(settings.GMAIL_ADDRESS)}: {total} emails matched "
               f"across {len(cfg.get('sources') or [])} senders ({per_sender}).")
    annotate("notice" if total else "warning",
             summary if total else summary + " No matches, so check the sender addresses "
                                             "under newsletter.sources in config.yaml.")
    print("\nResult: " + summary)
    return True


def _read_mail_state() -> dict:
    try:
        return json.loads(settings.MAIL_STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def _write_mail_state(state: dict, keep_days: int = 120):
    cutoff = (dt.date.today() - dt.timedelta(days=keep_days)).isoformat()
    state = {k: v for k, v in state.items() if v >= cutoff}
    settings.MAIL_STATE_FILE.parent.mkdir(exist_ok=True)
    settings.MAIL_STATE_FILE.write_text(json.dumps(state, indent=0, sort_keys=True),
                                       encoding="utf-8")


# ─── Replies to her applications (Gmail IMAP) ────────────────────────────────
def _gmail_raw(query: str) -> str:
    """A Gmail search string, quoted for IMAP's X-GM-RAW."""
    return '"' + query.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _read_json(path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def read_reply_state() -> dict:
    return _read_json(settings.REPLY_STATE_FILE)


def write_reply_state(state: dict, keep_days: int = 60):
    cutoff = (dt.date.today() - dt.timedelta(days=keep_days)).isoformat()
    state = {k: v for k, v in state.items() if v >= cutoff}
    settings.REPLY_STATE_FILE.parent.mkdir(exist_ok=True)
    settings.REPLY_STATE_FILE.write_text(json.dumps(state, indent=0, sort_keys=True),
                                        encoding="utf-8")


def _message_text(msg) -> str:
    """The readable body of an email, plain text preferred."""
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
    except (AttributeError, KeyError):
        part = None
    if part is None:
        return ""
    try:
        content = part.get_content()
    except (LookupError, ValueError, AssertionError):
        payload = part.get_payload(decode=True) or b""
        content = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    if part.get_content_type() == "text/html":
        content = html_to_text(content)
    return re.sub(r"\n\s*\n+", "\n\n", content or "").strip()


def application_emails(query: str, lookback_days: int, skip_keys=(), max_emails: int = 30):
    """Recent emails that may be a company answering one of her applications.

    Uses Gmail's own search syntax over IMAP (X-GM-RAW), so the query in
    config.yaml reads exactly like a search typed into Gmail. Emails already
    handled (their Message-ID is in skip_keys) are not fetched again.

    Returns a list of dicts, newest last, or None when Gmail could not be
    read, so that a mail problem is never mistaken for "no replies".
    """
    if not (settings.GMAIL_ADDRESS and settings.GMAIL_APP_PASSWORD):
        print("  [Replies] Skipped: GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set")
        return None
    full_query = f"newer_than:{int(lookback_days)}d {query}".strip()
    out = []
    try:
        box = imaplib.IMAP4_SSL("imap.gmail.com", timeout=IMAP_TIMEOUT)
        box.login(settings.GMAIL_ADDRESS, settings.GMAIL_APP_PASSWORD)
        mailbox = _all_mail_box(box)
        if box.select(mailbox, readonly=True)[0] != "OK":
            box.select("INBOX", readonly=True)
        status, data = box.search(None, "X-GM-RAW", _gmail_raw(full_query))
        if status != "OK":
            print(f"  [Replies] Gmail refused the search: {data}")
            box.logout()
            return None
        nums = data[0].split() if data and data[0] else []
        for num in nums[-int(max_emails):]:
            # Headers first: an email already handled costs one small fetch, not a full one.
            _, head = box.fetch(num, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
            if head and isinstance(head[0], tuple):
                key = clean(email.message_from_bytes(head[0][1]).get("Message-ID") or "")
                if key and key in skip_keys:
                    continue
            _, msg_data = box.fetch(num, "(X-GM-THRID BODY.PEEK[])")
            if not msg_data or not isinstance(msg_data[0], tuple):
                continue
            meta, raw = msg_data[0]
            msg = email.message_from_bytes(raw, policy=email.policy.default)
            key = clean(str(msg.get("Message-ID") or "")) or f"imap:{num.decode()}"
            if key in skip_keys:
                continue
            thread = re.search(rb"X-GM-THRID (\d+)", meta or b"")
            sender_name, sender_addr = email.utils.parseaddr(str(msg.get("From") or ""))
            try:
                when = email.utils.parsedate_to_datetime(str(msg.get("Date")))
            except (TypeError, ValueError):
                when = None
            out.append({
                "key": key,
                "date": (when.date() if when else dt.date.today()).isoformat(),
                "sender_name": clean(sender_name),
                "sender": sender_addr.lower(),
                "subject": clean(str(msg.get("Subject") or "")),
                "body": _message_text(msg)[:6000],
                "link": (f"https://mail.google.com/mail/u/0/#all/{int(thread.group(1)):x}"
                         if thread else ""),
            })
        box.logout()
    except (imaplib.IMAP4.error, OSError) as e:
        print(f"  [Replies] Could not read Gmail: {e}")
        return None
    return out


def newsletter_job_links():
    """Job links from recent emails, found by sender address.

    Searches "[Gmail]/All Mail" for each configured sender, so it works whether
    the mail sits in the inbox, is archived, or carries a label. No Gmail label
    or filter needed: the earlier version selected a mailbox named after a label
    that was never created, so this source silently yielded nothing.

    Each email is parsed once only: its Message-ID is remembered in
    state/seen_emails.json, which keeps the lookback window generous (a job
    board that arrives every few weeks is still caught) without re-resolving
    hundreds of tracking links every morning.

    Returns a list of (url, source_name) pairs.
    """
    if not (settings.GMAIL_ADDRESS and settings.GMAIL_APP_PASSWORD):
        missing = [n for n, v in (("GMAIL_ADDRESS", settings.GMAIL_ADDRESS),
                                  ("GMAIL_APP_PASSWORD", settings.GMAIL_APP_PASSWORD)) if not v]
        print(f"  [Newsletter] Skipped: {' and '.join(missing)} not set. "
              "Add them as repository secrets to read your job newsletters and LinkedIn alerts.")
        return []
    cfg = CONFIG["newsletter"]
    senders = cfg.get("sources") or []
    if not senders:
        print("  [Newsletter] No senders configured, skipping")
        return []
    mail_state = _read_mail_state()
    today = dt.date.today().isoformat()
    since_day = dt.date.today() - dt.timedelta(days=cfg["lookback_days"])
    since = f"{since_day.day:02d}-{MONTHS[since_day.month - 1]}-{since_day.year}"
    raw_links = []            # (href, source_name)
    try:
        box = imaplib.IMAP4_SSL("imap.gmail.com", timeout=IMAP_TIMEOUT)
        box.login(settings.GMAIL_ADDRESS, settings.GMAIL_APP_PASSWORD)
        mailbox = _all_mail_box(box)
        if box.select(mailbox, readonly=True)[0] != "OK":
            mailbox = "INBOX"
            box.select(mailbox, readonly=True)
        print(f"  [Newsletter] Reading {mailbox}")
        for src in senders:
            addr, name = src.get("from", ""), src.get("name") or src.get("from", "email")
            if not addr:
                continue
            status, data = box.search(None, f'(SINCE {since} FROM "{addr}")')
            nums = (data[0].split() if status == "OK" and data and data[0] else [])
            if not nums:
                print(f"  [Newsletter] {name}: no mail since {since}")
                continue
            new_emails, before = 0, len(raw_links)
            for num in nums[-int(src.get("max_emails", 6)):]:
                _, msg_data = box.fetch(num, "(RFC822)")
                if not msg_data or not isinstance(msg_data[0], tuple):
                    continue
                msg = email.message_from_bytes(msg_data[0][1])
                key = clean(msg.get("Message-ID") or "") or f"{addr}:{num!r}"
                if key in mail_state:
                    continue
                mail_state[key] = today
                new_emails += 1
                soup = BeautifulSoup(_html_part(msg), "html.parser")
                raw_links += [(a["href"], name) for a in soup.find_all("a", href=True)]
            print(f"  [Newsletter] {name}: {len(nums)} emails in window, {new_emails} new, "
                  f"{len(raw_links) - before} raw links")
        box.logout()
    except (imaplib.IMAP4.error, OSError) as e:
        print(f"  [Newsletter] Could not read Gmail: {e}. If this says the login failed, "
              "the app password has been revoked or GMAIL_ADDRESS is wrong.")
        return []

    out, seen = [], set()
    for href, name in raw_links:
        low = href.lower()
        if not low.startswith("http") or any(s in low for s in SKIP_HINTS):
            continue
        final = resolve_link(href) if ("substack" in low or "redirect" in low
                                       or "click" in low) else href
        final = tidy_link(final)
        if looks_like_job(final) and final not in seen:
            seen.add(final)
            out.append((final, name))
        if len(out) >= cfg["max_links"]:
            break
    _write_mail_state(mail_state)
    print(f"  [Newsletter] {len(out)} job links found")
    return out
