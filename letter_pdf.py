"""Render a cover letter as a PDF on Phaedra's own letterhead.

Her name is the mark. The company contributes an accent colour and whether the
text is serif, both read off their site by branding.py. Their logo never
appears: a letter carrying someone's logo reads as issued by them.

Typography uses the fonts built into every PDF reader, so a letter never fails
to render. To use a real typeface for a particular application, drop a .ttf
into fonts/ and name it under cover_letter.fonts in config.yaml.
"""
import datetime as dt
import io
import json
import re
import urllib.parse
from pathlib import Path

from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (BaseDocTemplate, Frame, PageTemplate, Paragraph,
                                Spacer)

import sources
from settings import CONFIG, ROOT

CFG = CONFIG.get("cover_letter", {})
FONT_FILES = CFG.get("fonts", {}) or {}
FONT_DIR = ROOT / "fonts"

# Labels in the Contact section of the Notion profile. What she writes there
# wins over config.yaml, so the numbers have one home she can edit.
PHONE_LABELS = {"us": r"tel\.?\s*(?:us|usa|united states|amerika)",
                "be": r"tel\.?\s*(?:be|belg\w*)"}


def phones_from_profile(profile_text: str) -> dict:
    """Pull 'Tel BE: ...', 'Tel US: ...' and 'Mail: ...' out of the profile."""
    found = {}
    for region, label in PHONE_LABELS.items():
        m = re.search(label + r"\s*[:\-]\s*([+0-9()./\s-]{6,24})", profile_text or "", re.I)
        if m:
            found[region] = " ".join(m.group(1).split())
    m = re.search(r"(?:^|\n)\s*(?:e-?)?mail\s*[:\-]\s*([\w.+-]+@[\w.-]+\.\w+)",
                  profile_text or "", re.I)
    if m:
        found["email"] = m.group(1)
    return found


def sender_for(location: str = "", profile_text: str = "") -> dict:
    """The contact block for one application, chosen by the row's Location.

    A Belgian or other European posting gets the Belgian number and city; a
    New York posting gets the US number. Remote and unknown fall back to the
    default region.
    """
    cfg = CFG.get("sender", {}) or {}
    regions = cfg.get("regions", {}) or {}
    region = cfg.get("default_region", "be")
    for key, spec in regions.items():
        if location and location in (spec.get("locations") or []):
            region = key
            break
    spec = regions.get(region, {}) or {}
    found = phones_from_profile(profile_text)
    return {"name": cfg.get("name", ""),
            "email": found.get("email") or cfg.get("email", ""),
            "phone": found.get(region) or spec.get("phone", ""),
            "location": spec.get("location_line", ""),
            "links": list(cfg.get("links") or []),
            "region": region}

PAGE_W, PAGE_H = A4
MARGIN_X, MARGIN_TOP, MARGIN_BOTTOM = 24 * mm, 26 * mm, 22 * mm


def _register_custom_fonts():
    """Any .ttf named in config, so a specific application can use real type."""
    pairs = {}
    for role in ("body", "body_bold", "heading"):
        filename = FONT_FILES.get(role)
        if not filename:
            continue
        path = FONT_DIR / filename
        if not path.exists():
            print(f"  [Letter] font {filename} is named in config but not in fonts/")
            continue
        name = f"custom-{role}"
        try:
            pdfmetrics.registerFont(TTFont(name, str(path)))
            pairs[role] = name
        except Exception as e:                       # a bad file must not stop a letter
            print(f"  [Letter] could not load {filename}: {e}")
    return pairs


# The typeface a company licenses cannot be downloaded, so each is matched to
# the closest one that can. Keys are matched inside the detected family name.
CLOSEST_FONT = {
    # serif, roughly from high-contrast display down to book faces
    "didot": "Playfair Display", "bodoni": "Playfair Display", "playfair": "Playfair Display",
    "canela": "Lora", "tiempos": "Lora", "lyon": "Lora", "chronicle": "Lora",
    "garamond": "EB Garamond", "sabon": "EB Garamond", "minion": "EB Garamond",
    "caslon": "Libre Caslon Text", "baskerville": "Libre Baskerville",
    "georgia": "Libre Baskerville", "times": "Libre Baskerville", "freight": "Lora",
    "recoleta": "Playfair Display", "editorial": "Playfair Display", "larken": "Playfair Display",
    # sans
    "futura": "Jost", "avenir": "Jost", "jost": "Jost", "gotham": "Jost",
    "circular": "Poppins", "maison": "Poppins", "poppins": "Poppins",
    "grotesk": "Space Grotesk", "grotesque": "Space Grotesk",
    "helvetica": "Inter", "neue": "Inter", "suisse": "Inter", "aktiv": "Inter",
    "sohne": "Inter", "söhne": "Inter", "graphik": "Inter", "founders": "Inter",
    "inter": "Inter", "arial": "Roboto", "roboto": "Roboto", "montserrat": "Montserrat",
    "lato": "Lato", "raleway": "Raleway", "work sans": "Work Sans", "dm sans": "DM Sans",
}
DEFAULT_FONT = {True: "EB Garamond", False: "Inter"}
# Fonts come from Google's own repository rather than their stylesheet API,
# because the repository serves .ttf, which is all reportlab can embed.
FONT_REPO = "https://raw.githubusercontent.com/google/fonts/main"
FONT_LICENCES = ("ofl", "apache", "ufl")
# Most families there are a single variable file; the handful that are not
# ship one file per weight.
FONT_SHAPES = ("[wght]", "[opsz,wght]", "[wdth,wght]", "[wght,wdth]", "-Regular")
FONT_CACHE = ROOT / "state" / "fonts"
FONT_INDEX = FONT_CACHE / "sources.json"


def tidy_family(detected: str) -> str:
    """The first family in a CSS stack, spelled as Google spells it.

    "manrope", roboto  ->  Manrope.  Returns "" for anything that is not a
    plain ASCII name, since a mangled "Söhne" would only waste a lookup.
    """
    first = (detected or "").split(",")[0].strip().strip("\"'").strip()
    first = re.sub(r"\s+", " ", first)
    if not first or not re.fullmatch(r"[A-Za-z0-9 ]{2,40}", first):
        return ""
    return " ".join(w.capitalize() if w.islower() else w for w in first.split())


def closest_font(detected: str, serif: bool) -> str:
    low = (detected or "").lower()
    for token, family in CLOSEST_FONT.items():
        if token in low:
            return family
    return DEFAULT_FONT[bool(serif)]


# The first four bytes of a real TrueType file. Checked before anything is
# cached, so one bad response cannot poison every later run.
TTF_MAGIC = (b"\x00\x01\x00\x00", b"true", b"ttcf")


def _is_ttf(blob: bytes) -> bool:
    return bool(blob) and blob[:4] in TTF_MAGIC


def _fetch(url: str):
    r = sources.http_get(url, retries=1, timeout=25)
    if r is None or getattr(r, "status_code", 0) != 200:
        return None
    blob = getattr(r, "content", b"") or b""
    return blob if _is_ttf(blob) else None


def _index() -> dict:
    try:
        return json.loads(FONT_INDEX.read_text())
    except Exception:
        return {}


def _remember(family: str, shape) -> None:
    FONT_CACHE.mkdir(parents=True, exist_ok=True)
    try:
        FONT_INDEX.write_text(json.dumps(dict(_index(), **{family: shape})))
    except OSError:
        pass


def _locate(family: str):
    """Where this family lives in the repository: [licence, shape], or None.

    Looked up once and remembered, misses included, so a later letter for the
    same house costs nothing.
    """
    known = _index()
    if family in known:
        return known[family]
    bare = family.replace(" ", "")
    slug = bare.lower()
    for licence in FONT_LICENCES:
        for shape in FONT_SHAPES:
            r = sources.http_get(f"{FONT_REPO}/{licence}/{slug}/{bare}{shape}.ttf",
                                 retries=0, timeout=12)
            if r is not None and getattr(r, "status_code", 0) == 200:
                _remember(family, [licence, shape])
                return [licence, shape]
    _remember(family, None)
    return None


def _instance(blob: bytes, weight: int, out: Path) -> bool:
    """Cut one real weight out of a variable font.

    A variable file's built-in default is often the lightest weight — Manrope's
    is ExtraLight — so embedding it as it comes would set the letter in a face
    nobody chose.
    """
    try:
        from fontTools.ttLib import TTFont as _FT
        from fontTools.varLib import instancer
    except ImportError:
        return False
    try:
        font = _FT(io.BytesIO(blob))
        if "fvar" not in font:
            out.write_bytes(blob)
            return True
        instancer.instantiateVariableFont(font, {"wght": weight}, inplace=True,
                                          updateFontNames=True)
        font.save(str(out))
        return True
    except Exception as e:
        print(f"  [Letter] could not cut {out.stem} from the variable font: {e}")
        return False


def _download_font(family: str, bold: bool):
    """One weight of a Google font as a .ttf on disk, or None."""
    FONT_CACHE.mkdir(parents=True, exist_ok=True)
    bare = family.replace(" ", "")
    path = FONT_CACHE / f"{bare}-{'Bold' if bold else 'Regular'}.ttf"
    if path.exists():
        if _is_ttf(path.read_bytes()[:4]):
            return path
        path.unlink()                      # a bad cache entry, from a failed fetch
    found = _locate(family)
    if not found:
        return None
    licence, shape = found
    slug = family.lower().replace(" ", "")
    if shape == "-Regular":                # a family shipped one weight per file
        blob = _fetch(f"{FONT_REPO}/{licence}/{slug}/{bare}-{'Bold' if bold else 'Regular'}.ttf")
        if not blob:
            return None
        path.write_bytes(blob)
        return path
    blob = _fetch(f"{FONT_REPO}/{licence}/{slug}/{urllib.parse.quote(bare + shape)}.ttf")
    if not blob or not _instance(blob, 700 if bold else 400, path):
        return None
    return path


def fonts_for(brand: dict) -> dict:
    """Body, bold and display faces, as close to the company's as licensing allows.

    A file named in config wins. Otherwise the company's own family is tried
    first, since plenty are open source, and the nearest lookalike after it.
    Anything that fails falls back to the faces built into every PDF reader,
    so a letter always renders.
    """
    serif = bool(brand.get("serif"))
    custom = _register_custom_fonts()
    base = ("Times-Roman", "Times-Bold") if serif else ("Helvetica", "Helvetica-Bold")
    picked = {"body": custom.get("body", base[0]),
              "bold": custom.get("body_bold", base[1]),
              "display": custom.get("heading", custom.get("body_bold", base[1])),
              "named": ""}
    if custom.get("body") or not CFG.get("match_fonts", True):
        return picked
    # Her own typeface is often open source — Manrope, Inter, Lora and the
    # rest are all on Google Fonts — so try the real family before settling
    # for the nearest lookalike.
    detected = brand.get("font_name", "")
    wanted = [f for f in (tidy_family(detected), closest_font(detected, serif)) if f]
    family, regular, strong = "", None, None
    for candidate in dict.fromkeys(wanted):
        regular = _download_font(candidate, False)
        if regular:
            family, strong = candidate, _download_font(candidate, True)
            break
    try:
        if not regular:
            return picked
        name = family.replace(" ", "")
        pdfmetrics.registerFont(TTFont(name, str(regular)))
        picked["body"] = name
        if strong:
            pdfmetrics.registerFont(TTFont(f"{name}-Bold", str(strong)))
            picked["bold"] = picked["display"] = f"{name}-Bold"
        else:
            picked["bold"] = picked["display"] = name
        picked["named"] = family
    except Exception as e:                      # a font must never cost a letter
        print(f"  [Letter] could not use {family}: {e}")
    return picked


class _Letterhead:
    """Draws the fixed furniture: the wordmark, the rule and the footer."""

    def __init__(self, brand, fonts, company, sender):
        self.accent = HexColor(brand.get("accent", "#1F1F1F"))
        self.muted = HexColor(brand.get("muted", "#6B6B6B"))
        self.ink = HexColor(brand.get("ink", "#141414"))
        self.fonts = fonts
        self.company = company
        self.sender = sender

    def __call__(self, canvas, doc):
        canvas.saveState()
        name = (self.sender.get("name") or "").upper()
        canvas.saveState()                  # character spacing persists in the text
        wordmark = canvas.beginText(MARGIN_X, PAGE_H - MARGIN_TOP + 6 * mm)
        wordmark.setFont(self.fonts["display"], 13)
        wordmark.setFillColor(self.ink)
        wordmark.setCharSpace(2.6)          # only text objects can track letters
        wordmark.textOut(name)
        canvas.drawText(wordmark)
        canvas.restoreState()               # state, so it must not leak to the footer

        rule_y = PAGE_H - MARGIN_TOP + 2 * mm
        canvas.setStrokeColor(self.accent)
        canvas.setLineWidth(1.6)
        canvas.line(MARGIN_X, rule_y, PAGE_W - MARGIN_X, rule_y)

        bits = [self.sender.get("email"), self.sender.get("phone"), self.sender.get("location")]
        bits += list(self.sender.get("links") or [])
        footer = "   ·   ".join(b for b in bits if b)
        if footer:
            line = canvas.beginText(MARGIN_X, MARGIN_BOTTOM - 8 * mm)
            line.setFont(self.fonts["body"], 7.6)
            line.setFillColor(self.muted)
            line.setCharSpace(0)
            line.textOut(footer)
            canvas.drawText(line)
        canvas.setStrokeColor(self.accent)
        canvas.setLineWidth(0.6)
        canvas.line(MARGIN_X, MARGIN_BOTTOM - 4.5 * mm, MARGIN_X + 16 * mm, MARGIN_BOTTOM - 4.5 * mm)
        canvas.restoreState()


def _clean(text: str) -> str:
    """Letter text into paragraph markup, escaping what reportlab would eat."""
    out = (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"\s+", " ", out).strip()


def build_letter_pdf(path, letter: str, company: str, role: str, brand: dict,
                     recipient: str = "", sender: dict = None, fonts: dict = None) -> Path:
    """Write the PDF and return its path.

    letter: the body, paragraphs separated by blank lines, no greeting or
    sign-off of its own; those are set here so every letter is laid out alike.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    sender = sender or sender_for()
    fonts = fonts or fonts_for(brand)
    ink = HexColor(brand.get("ink", "#141414"))
    accent = HexColor(brand.get("accent", "#1F1F1F"))
    muted = HexColor(brand.get("muted", "#6B6B6B"))

    body = ParagraphStyle("body", fontName=fonts["body"], fontSize=10.1, leading=15.4,
                          textColor=ink, alignment=TA_JUSTIFY, spaceAfter=7.2)
    meta = ParagraphStyle("meta", parent=body, fontSize=8.4, leading=12,
                          textColor=muted, alignment=0, spaceAfter=0)
    greeting = ParagraphStyle("greeting", parent=body, spaceAfter=9, alignment=0)
    signoff = ParagraphStyle("signoff", parent=body, alignment=0, spaceAfter=0)
    role_style = ParagraphStyle("role", parent=body, fontName=fonts["bold"], fontSize=9.4,
                                leading=13, textColor=accent, alignment=0, spaceAfter=14)

    doc = BaseDocTemplate(str(path), pagesize=A4,
                          leftMargin=MARGIN_X, rightMargin=MARGIN_X,
                          topMargin=MARGIN_TOP, bottomMargin=MARGIN_BOTTOM,
                          title=f"Cover letter — {sender.get('name', '')} — {company}",
                          author=sender.get("name", ""), subject=role)
    frame = Frame(MARGIN_X, MARGIN_BOTTOM, PAGE_W - 2 * MARGIN_X,
                  PAGE_H - MARGIN_TOP - MARGIN_BOTTOM, id="body",
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id="letter", frames=[frame],
                                       onPage=_Letterhead(brand, fonts, company, sender))])

    story = [Spacer(1, 6 * mm),
             Paragraph(f"{dt.date.today():%d %B %Y}", meta),
             Spacer(1, 7 * mm)]
    if company:
        story.append(Paragraph(_clean(company), ParagraphStyle(
            "company", parent=body, fontName=fonts["bold"], fontSize=10.4, alignment=0, spaceAfter=1)))
    if role:
        story.append(Paragraph(_clean(role), role_style))
    if recipient:
        salutation = recipient
    elif company:
        salutation = f"Dear {company} team,"
    else:
        salutation = "Dear hiring team,"
    story.append(Paragraph(_clean(salutation), greeting))
    for para in re.split(r"\n\s*\n", letter or ""):
        if para.strip():
            story.append(Paragraph(_clean(para), body))
    story += [Spacer(1, 4 * mm),
              Paragraph("Kind regards,", signoff),
              Spacer(1, 2 * mm),
              Paragraph(_clean(sender.get("name", "")), ParagraphStyle(
                  "name", parent=signoff, fontName=fonts["bold"]))]
    doc.build(story)
    return path
