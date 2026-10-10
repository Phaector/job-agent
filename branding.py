"""Read a company's visual identity off its own website.

Used to style a cover letter so it sits next to the company's own material
without pretending to be theirs: her letterhead, their accent colour and the
kind of typeface they use. Never their logo.

Everything here degrades quietly. A site that blocks us, renders through
JavaScript or simply has no usable colour returns the neutral default, and the
letter still goes out looking deliberate.
"""
import re
import colorsys

import settings
import sources
from settings import CONFIG

CFG = CONFIG.get("cover_letter", {}).get("branding", {})
MAX_CSS = int(CFG.get("max_css_bytes", 400_000))
NEUTRAL = {"accent": CFG.get("fallback_accent", "#1F1F1F"), "ink": "#141414",
           "muted": "#6B6B6B", "serif": False, "font_name": "", "source": "default"}

# Property names that tell us a colour is the brand's, not incidental chrome.
BRAND_HINTS = ("brand", "primary", "accent", "main", "theme", "highlight", "cta", "link")
# Families whose look we can approximate. Value says whether it reads as serif.
KNOWN_FAMILIES = {
    "times": True, "georgia": True, "garamond": True, "baskerville": True, "didot": True,
    "bodoni": True, "playfair": True, "cormorant": True, "freight": True, "caslon": True,
    "minion": True, "tiempos": True, "canela": True, "lyon": True, "chronicle": True,
    "recoleta": True, "larken": True, "editorial": True, "serif": True,
    "helvetica": False, "arial": False, "inter": False, "roboto": False, "lato": False,
    "montserrat": False, "poppins": False, "futura": False, "avenir": False, "jost": False,
    "gotham": False, "graphik": False, "founders": False, "neue": False, "grotesk": False,
    "grotesque": False, "suisse": False, "aktiv": False, "circular": False, "sohne": False,
    "söhne": False, "maison": False, "sans": False,
}
GENERIC = {"inherit", "initial", "unset", "revert", "currentcolor", "transparent", "none",
           "system-ui", "-apple-system", "blinkmacsystemfont", "ui-sans-serif", "ui-serif",
           "sans-serif", "serif", "monospace", "cursive", "fantasy", "emoji", "math"}


# ─── Finding the company's own site ──────────────────────────────────────────
# Most rows arrive with the Website column empty, and without a site there is
# no brand to read, so nearly every letter fell back to the neutral style.
# These hosts are where a posting lives, never the company itself.
NOT_THE_COMPANY = (
    "recruitee", "greenhouse", "lever.co", "workable", "ashbyhq", "teamtailor",
    "myworkday", "workday", "adzuna", "linkedin", "indeed", "fashionunited",
    "smartrecruiters", "icims", "jobvite", "rippling", "personio", "jobtoolz",
    "bamboohr", "glassdoor", "ziprecruiter", "joinhandshake", "wellfound",
    "google.com", "facebook.com", "instagram.com", "twitter.com", "x.com",
    "youtube.com", "tiktok.com", "substack.com", "gstatic", "cloudflare",
    "schema.org", "w3.org", "apple.com", "cookiebot", "googleapis",
)


def _plausible_site(url: str, company: str) -> bool:
    """A link that looks like the employer's own front door."""
    host = re.sub(r"^https?://(www\.)?", "", url or "").split("/")[0].lower()
    if not host or "." not in host or any(bad in host for bad in NOT_THE_COMPANY):
        return False
    stem = re.sub(r"[^a-z0-9]", "", (company or "").lower())
    root = re.sub(r"[^a-z0-9]", "", host.split(".")[0])
    if not stem or not root:
        return False
    # The host should carry the company's name, or the other way round for
    # the short domains fashion houses like ("ssp" for Suspicious Antwerp).
    return root in stem or stem in root or stem[:8] == root[:8]


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _tld_preference(location: str):
    """Belgian rows should land on the .be site, New York ones on the .com."""
    if location in ("Antwerp", "Brussels", "Ghent", "Leuven", "Belgium – other"):
        return (".be", ".eu", ".com")
    if location == "Paris":
        return (".fr", ".com")
    if location == "Amsterdam":
        return (".nl", ".com")
    return (".com", ".co", ".be")


def _from_clearbit(company: str, prefer):
    """Clearbit's company autocomplete: free, no key, name in, domain out."""
    r = sources.http_get("https://autocomplete.clearbit.com/v1/companies/suggest",
                         params={"query": company}, retries=0, timeout=10)
    try:
        items = r.json() if r is not None else []
    except ValueError:
        return ""
    if not isinstance(items, list) or not items:
        return ""
    target = _norm(company)
    exact = [i for i in items if _norm(i.get("name")) == target]
    partial = [i for i in items
               if target.startswith(_norm(i.get("name"))) or _norm(i.get("name")).startswith(target)]
    for group in (exact, partial):
        for tld in prefer:
            for item in group:
                if (item.get("domain") or "").endswith(tld):
                    return f"https://{item['domain']}"
        if group and group[0].get("domain"):
            return f"https://{group[0]['domain']}"
    return ""


def _from_search(company: str):
    """A plain Google search through SerpApi, when there is a key to spend."""
    if not settings.SERPAPI_KEY:
        return ""
    data = sources.get_json("https://serpapi.com/search.json", {
        "engine": "google", "q": f"{company} official website", "num": 5,
        "api_key": settings.SERPAPI_KEY})
    for result in (data or {}).get("organic_results", []) or []:
        link = result.get("link") or ""
        if _plausible_site(link, company):
            m = re.match(r"https?://[^/]+", link)
            return m.group(0) if m else ""
    return ""


def find_website(company: str, posting_url: str = "", description: str = "",
                 page_html: str = "", location: str = "") -> str:
    """The company's own site. Cheapest sources first, paid search last."""
    if not company or len(_norm(company)) < 3:
        return ""
    prefer = _tld_preference(location)

    for candidate in re.findall(r"https?://[^\s\"'<>)\]]+", f"{description} {page_html}"):
        candidate = candidate.rstrip(".,);")
        if _plausible_site(candidate, company):
            m = re.match(r"https?://[^/]+", candidate)
            if m:
                return m.group(0)

    for lookup in (_from_clearbit, _from_search):
        try:
            found = lookup(company, prefer) if lookup is _from_clearbit else lookup(company)
        except Exception as e:                  # a lookup must never stop a run
            print(f"  [Website] {lookup.__name__} failed for {company}: {e}")
            continue
        if found and _plausible_site(found, company):
            return found
        if found:                               # a search result we cannot vouch for
            print(f"  [Website] ignoring {found} for {company}: name does not match")

    stem = _norm(company)[:30]
    if len(stem) < 4:
        return ""
    for tld in prefer:
        url = f"https://{stem}{tld}"
        r = sources.http_get(url, retries=0, timeout=10)
        if r is None or not getattr(r, "text", ""):
            continue
        if stem[:6] in _norm(r.text[:4000]):
            return url
    return ""


# ─── Colour helpers ───────────────────────────────────────────────────────────
def parse_color(raw: str):
    """'#abc', '#aabbcc', 'rgb(1,2,3)', 'rgba(1,2,3,.5)' -> (r, g, b) or None."""
    text = (raw or "").strip().lower()
    m = re.fullmatch(r"#([0-9a-f]{3})", text)
    if m:
        return tuple(int(c * 2, 16) for c in m.group(1))
    m = re.fullmatch(r"#([0-9a-f]{6})", text)
    if m:
        return tuple(int(m.group(1)[i:i + 2], 16) for i in (0, 2, 4))
    m = re.fullmatch(r"rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)\s*(?:[,/]\s*([\d.%]+)\s*)?\)", text)
    if m:
        if m.group(4) and float(m.group(4).rstrip("%")) in (0, 0.0):
            return None                      # fully transparent, so not a brand colour
        try:
            return tuple(min(255, max(0, int(float(m.group(i))))) for i in (1, 2, 3))
        except ValueError:
            return None
    return None


def to_hex(rgb) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def hsl(rgb):
    h, l, s = colorsys.rgb_to_hls(*[c / 255 for c in rgb])
    return h, s, l


def is_usable_accent(rgb) -> bool:
    """Coloured enough to read as a choice, not black, white or a grey."""
    _h, s, l = hsl(rgb)
    return s >= 0.18 and 0.12 <= l <= 0.78


def darken(rgb, amount=0.45):
    h, s, l = hsl(rgb)
    r, g, b = colorsys.hls_to_rgb(h, max(l * (1 - amount), 0.06), s)
    return tuple(round(c * 255) for c in (r, g, b))


def mute(rgb, amount=0.45):
    """Toward grey, for footer text that should recede."""
    h, s, l = hsl(rgb)
    r, g, b = colorsys.hls_to_rgb(h, min(l + (1 - l) * 0.25, 0.62), s * (1 - amount))
    return tuple(round(c * 255) for c in (r, g, b))


# ─── Reading the page ─────────────────────────────────────────────────────────
def _stylesheets(html: str, base: str, limit=3):
    """The first few same-origin stylesheets, as text."""
    out = []
    for href in re.findall(r'<link[^>]+rel=["\']?stylesheet["\']?[^>]*>', html, re.I):
        m = re.search(r'href=["\']([^"\']+)["\']', href)
        if not m:
            continue
        url = m.group(1)
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("/"):
            url = base.rstrip("/") + url
        elif not url.startswith("http"):
            url = base.rstrip("/") + "/" + url
        r = sources.http_get(url, retries=0)
        if r is not None and getattr(r, "text", ""):
            out.append(r.text[:MAX_CSS])
        if len(out) >= limit:
            break
    return out


def _colour_candidates(css: str):
    """(rgb, score) pairs. Declared brand colours outrank incidental ones."""
    found = {}

    def add(rgb, weight):
        if rgb and is_usable_accent(rgb):
            found[rgb] = found.get(rgb, 0) + weight

    for name, value in re.findall(r"(--[\w-]+)\s*:\s*([^;{}]+)", css):
        weight = 60 if any(h in name.lower() for h in BRAND_HINTS) else 8
        add(parse_color(value), weight)
    for prop, value in re.findall(r"(background-color|background|color|border-color|fill)\s*:\s*([^;{}!]+)", css):
        add(parse_color(value.strip().split()[0] if value.strip() else ""),
            4 if prop == "color" else 6)
    return found


def _font_candidates(css: str):
    """Declared families, most-declared first, generics dropped."""
    counts = {}
    for stack in re.findall(r"font-family\s*:\s*([^;{}!]+)", css, re.I):
        for part in stack.split(","):
            name = part.strip().strip("'\"").lower()
            if not name or name in GENERIC or len(name) > 40:
                continue
            counts[name] = counts.get(name, 0) + 1
            break               # only the first real family in each stack
    return sorted(counts, key=lambda n: -counts[n])


def classify_font(name: str):
    """Serif or not, by family name. None when we cannot tell."""
    low = (name or "").lower()
    for token, serif in KNOWN_FAMILIES.items():
        if token in low:
            return serif
    return None


# ─── The one function the rest of the agent calls ─────────────────────────────
def brand_for(website: str, company: str = "") -> dict:
    """Accent colour, ink, muted tone and serif-or-not for one company."""
    if not CFG.get("enabled", True) or not website:
        return dict(NEUTRAL, source="no website on the row")
    r = sources.http_get(website, retries=1)
    if r is None or not getattr(r, "text", ""):
        return dict(NEUTRAL, source=f"could not read {website}")
    html = r.text
    base = re.match(r"https?://[^/]+", website)
    sheets = _stylesheets(html, base.group(0) if base else website)
    css = "\n".join(sheets)

    scores = _colour_candidates(css)
    for m in re.findall(r'<meta[^>]+name=["\']theme-color["\'][^>]+content=["\']([^"\']+)', html, re.I):
        rgb = parse_color(m)
        if rgb and is_usable_accent(rgb):
            scores[rgb] = scores.get(rgb, 0) + 80        # the site says this is its colour
    inline = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.S | re.I))
    for rgb, weight in _colour_candidates(inline).items():
        scores[rgb] = scores.get(rgb, 0) + weight

    if not scores:
        brand = dict(NEUTRAL, source=f"no brand colour found on {website}")
    else:
        accent = max(scores, key=lambda c: (scores[c], -hsl(c)[2]))
        brand = {"accent": to_hex(accent), "ink": to_hex(darken(accent, 0.62)),
                 "muted": to_hex(mute(accent)), "source": f"colour from {website}"}

    families = _font_candidates(css + "\n" + inline)
    brand["font_name"], brand["serif"] = "", False
    for name in families:
        serif = classify_font(name)
        if serif is not None:
            brand["font_name"], brand["serif"] = name, serif
            break
    if not brand["font_name"] and families:
        brand["font_name"] = families[0]
    return brand
