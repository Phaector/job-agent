"""Free checks that run before any AI call."""
import datetime as dt
import re
import urllib.parse

from settings import CONFIG

F = CONFIG["filters"]
LOC = CONFIG["locations"]
KEEP_QUERY_PARAMS = {"jk", "jl", "gh_jid", "id", "jobid", "job_id", "currentjobid", "token", "for"}


def _words_rx(patterns):
    """Whole-word regex from a list of words/simple patterns."""
    body = "|".join(p if any(c in p for c in "[]?") else re.escape(p) for p in patterns)
    return re.compile(rf"(?<!\w)(?:{body})(?!\w)", re.IGNORECASE)


EXCLUDED_RX = _words_rx(F["exclude_title_patterns"])
SENIOR_RX = _words_rx(F["senior_title_words"])
INTERN_RX = _words_rx(F["internship_title_patterns"])
TRAINEE_RX = _words_rx(F["trainee_title_patterns"])
LOCATION_RX = [(name, tier, _words_rx(aliases))
               for tier in ("main", "stretch") for name, aliases in LOC[tier].items()]
REMOTE_RX = re.compile(r"(?<!\w)remote(?!\w)", re.IGNORECASE)
REMOTE_OK_RX = _words_rx(LOC["remote_ok_regions"])
EUROPE = set(LOC["europe_options"])

YEAR_PATTERNS = [
    r"(\d{1,2})\s*\+?\s*(?:-|–|to|tot|à)?\s*(?:\d{1,2}\s*)?\+?\s*(?:years?|yrs?)\b[^.\n]{0,40}?\bexperience",
    r"(?:minimum|min\.|at least)\s*(?:of\s*)?(\d{1,2})\s*\+?\s*(?:years?|yrs?)",
    r"(\d{1,2})\s*\+?\s*(?:-|–|tot)?\s*(?:\d{1,2}\s*)?jaar\s*(?:relevante\s*)?(?:werk)?ervaring",
    r"(?:minimum|minstens|ten minste)\s*(\d{1,2})\s*jaar",
    r"(\d{1,2})\s*\+?\s*(?:-|–|à)?\s*(?:\d{1,2}\s*)?ans?\s*d['’]\s*exp[ée]rience",
    r"(?:au moins|minimum)\s*(\d{1,2})\s*ans",
]


# ─── Basic helpers ────────────────────────────────────────────────────────────
def normalize_url(url: str) -> str:
    if not url:
        return ""
    p = urllib.parse.urlparse(url.strip())
    query = urllib.parse.parse_qs(p.query)
    kept = {k: v for k, v in query.items() if k.lower() in KEEP_QUERY_PARAMS}
    path = p.path.rstrip("/")
    q = urllib.parse.urlencode(sorted(kept.items()), doseq=True)
    return f"{p.netloc.lower().removeprefix('www.')}{path}" + (f"?{q}" if q else "")


def days_old(date_str):
    if not date_str:
        return None
    try:
        return (dt.date.today() - dt.date.fromisoformat(date_str[:10])).days
    except ValueError:
        return None


def location_bucket(location: str):
    """Map a location string to (select option, 'main'|'stretch') or (None, None)."""
    text = location or ""
    for name, tier, rx in LOCATION_RX:
        if rx.search(text):
            return name, tier
    if REMOTE_RX.search(text):
        bare = REMOTE_RX.sub("", text).strip(" ,-()/|")
        if not bare or REMOTE_OK_RX.search(text):
            return LOC["remote_option"], "main"
    return None, None


def is_internship(title: str) -> bool:
    return bool(INTERN_RX.search(title or ""))


def is_trainee(title: str) -> bool:
    return bool(TRAINEE_RX.search(title or ""))


def min_years_required(text: str):
    found = []
    for pattern in YEAR_PATTERNS:
        for m in re.finditer(pattern, text or "", re.IGNORECASE):
            n = int(m.group(1))
            if 0 < n <= 20:
                found.append(n)
    return min(found) if found else None


# ─── The filter itself ────────────────────────────────────────────────────────
def hard_filter(job: dict):
    """Returns (keep, reason). Also stamps location/type info onto the job."""
    title = job.get("title", "")
    if not title or not job.get("url"):
        return False, "missing title or link"
    option, tier = location_bucket(job.get("location", ""))
    if not option:
        return False, "outside your locations"
    job["loc_option"], job["loc_tier"] = option, tier
    if EXCLUDED_RX.search(title):
        return False, "excluded role type"
    if SENIOR_RX.search(title):
        return False, "too senior"
    job["is_internship"] = is_internship(title)
    job["is_trainee"] = is_trainee(title)
    if job["is_internship"] and option in EUROPE:
        return False, "internship in Europe"
    years = min_years_required(f"{title} {job.get('description', '')}")
    if years is not None and years > F["max_years_required"]:
        return False, "asks for 3+ years"
    age = days_old(job.get("posted_at"))
    if age is not None and age > CONFIG["limits"]["max_posting_age_days"]:
        return False, "posting too old"
    return True, ""


# ─── Duplicates ───────────────────────────────────────────────────────────────
_COMPANY_SUFFIXES = {"nv", "bv", "bvba", "sa", "srl", "sprl", "vzw", "asbl", "gmbh", "ltd", "llc",
                     "inc", "plc", "group", "belgium", "belgie", "belgique", "usa", "us", "co", "corp"}
_STOP = {"the", "a", "an", "and", "of", "at", "for", "in", "to", "with", "or", "de", "het", "van",
         "voor", "bij", "en", "la", "le", "les", "des", "du", "pour", "m", "f", "x", "h", "v"}


def norm_company(name: str) -> str:
    tokens = re.sub(r"[^\w\s]", " ", (name or "").lower()).split()
    return " ".join(t for t in tokens if t not in _COMPANY_SUFFIXES)


def title_tokens(title: str) -> set:
    tokens = re.sub(r"[^\w\s]", " ", (title or "").lower()).split()
    return {t for t in tokens if len(t) > 1 and t not in _STOP}


def title_similarity(a: str, b: str) -> float:
    ta, tb = title_tokens(a), title_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def matches_existing(job: dict, rows: list, threshold: float = 0.7) -> bool:
    company = norm_company(job.get("company", ""))
    if not company:
        return False
    for r in rows:
        if norm_company(r.get("company", "")) == company and \
                title_similarity(job.get("title", ""), r.get("title", "")) >= threshold:
            return True
    return False


def dedupe_batch(jobs: list, threshold: float = 0.6) -> list:
    """Exact duplicates by link, then the same role at the same company
    (crossposted or reworded) merged into one job with 'also_posted_on'."""
    by_url = {}
    for j in jobs:
        by_url.setdefault(normalize_url(j["url"]), j)
    merged = []
    for j in by_url.values():
        company = norm_company(j.get("company", ""))
        twin = None
        if company:
            twin = next((m for m in merged if norm_company(m.get("company", "")) == company and
                         title_similarity(m["title"], j["title"]) >= threshold), None)
        if twin is None:
            merged.append(j)
            continue
        keep, other = (twin, j) if len(twin.get("description", "")) >= len(j.get("description", "")) else (j, twin)
        keep.setdefault("also_posted_on", [])
        keep["also_posted_on"] += other.get("also_posted_on", []) + \
            [{"source": other.get("source", ""), "url": other["url"]}]
        if keep is j:
            merged[merged.index(twin)] = j
    return merged
