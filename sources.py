"""Where jobs come from.

1. Company job boards via their public JSON feeds (Greenhouse, Lever,
   Ashby, Workable, Recruitee). No scraping, no login.
2. Adzuna search API (US, Belgium, France, Netherlands).
3. Google Jobs via SerpApi (optional).
4. Job links inside newsletter emails (optional, via Gmail IMAP).
5. Reading a single posting from any link (for rows you add yourself).

Every source returns the same job dict, built by make_job().
"""
import datetime as dt
import email
import html
import imaplib
import json
import re
import time
import urllib.parse

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
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except (ValueError, OverflowError, OSError):
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


BOARD_READERS = {
    "greenhouse": board_greenhouse, "lever": board_lever, "ashby": board_ashby,
    "workable": board_workable, "recruitee": board_recruitee,
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
                  "linkedin.com/jobs", "glassdoor", "indeed.", "wellfound.com", "builtinnyc.com")
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


def resolve_link(url: str) -> str:
    try:
        r = SESSION.get(url, allow_redirects=True, timeout=15, stream=True)
        final = r.url
        r.close()
        return final
    except requests.RequestException:
        return url


def newsletter_job_links():
    """Job links from recent emails carrying the configured Gmail label."""
    if not (settings.GMAIL_ADDRESS and settings.GMAIL_APP_PASSWORD):
        return []
    cfg = CONFIG["newsletter"]
    since_day = dt.date.today() - dt.timedelta(days=cfg["lookback_days"])
    since = f"{since_day.day:02d}-{MONTHS[since_day.month - 1]}-{since_day.year}"
    raw_links = []
    try:
        box = imaplib.IMAP4_SSL("imap.gmail.com")
        box.login(settings.GMAIL_ADDRESS, settings.GMAIL_APP_PASSWORD)
        status, _ = box.select(f'"{cfg["gmail_label"]}"', readonly=True)
        if status != "OK":
            print(f"  [Newsletter] Gmail label '{cfg['gmail_label']}' not found.")
            box.logout()
            return []
        _, data = box.search(None, "SINCE", since)
        for num in (data[0].split() if data and data[0] else [])[-10:]:
            _, msg_data = box.fetch(num, "(RFC822)")
            if not msg_data or not isinstance(msg_data[0], tuple):
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            soup = BeautifulSoup(_html_part(msg), "html.parser")
            raw_links += [a["href"] for a in soup.find_all("a", href=True)]
        box.logout()
    except (imaplib.IMAP4.error, OSError) as e:
        print(f"  [Newsletter] Could not read Gmail: {e}")
        return []

    out, seen = [], set()
    for href in raw_links:
        low = href.lower()
        if not low.startswith("http") or any(s in low for s in SKIP_HINTS):
            continue
        final = resolve_link(href) if ("substack" in low or "redirect" in low or "click" in low) else href
        if looks_like_job(final) and final not in seen:
            seen.add(final)
            out.append(final)
        if len(out) >= cfg["max_links"]:
            break
    print(f"  [Newsletter] {len(out)} job links found")
    return out
