"""Everything that talks to Notion.

Adapts to your existing "DB Applications" database: property names come
from config.yaml, optional properties are skipped if they don't exist,
and everything that isn't a column goes into the page body.
"""
import datetime as dt
import time
from pathlib import Path

import requests

import settings
from settings import CONFIG

API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
P = CONFIG["notion"]["properties"]
ST = CONFIG["notion"]["statuses"]
TYPES = CONFIG["notion"]["type_options"]
TEXT_LIMIT = 1900


# ─── Block helpers ────────────────────────────────────────────────────────────
def rt(text, link=None, bold=False):
    obj = {"type": "text", "text": {"content": str(text or "")[:TEXT_LIMIT]}}
    if link:
        obj["text"]["link"] = {"url": link}
    if bold:
        obj["annotations"] = {"bold": True}
    return obj


def paragraph(*parts):
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [p if isinstance(p, dict) else rt(p) for p in parts]}}


def heading(text, level=2):
    kind = f"heading_{level}"
    return {"object": "block", "type": kind, kind: {"rich_text": [rt(text)]}}


def bullet(*parts):
    return {"object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": [p if isinstance(p, dict) else rt(p) for p in parts]}}


def callout(text, emoji="⚠️"):
    return {"object": "block", "type": "callout",
            "callout": {"rich_text": [rt(text)], "icon": {"type": "emoji", "emoji": emoji}}}


def divider():
    return {"object": "block", "type": "divider", "divider": {}}


def salary_text(salary):
    if not isinstance(salary, dict) or not salary.get("min"):
        return ""
    lo, hi, cur = salary.get("min"), salary.get("max"), salary.get("currency") or ""
    try:
        lo_s = f"{float(lo):,.0f}"
        hi_s = f" to {float(hi):,.0f}" if hi else ""
    except (TypeError, ValueError):
        return ""
    return f"{cur} {lo_s}{hi_s}".strip()


def job_blocks(job: dict) -> list:
    d = job.get("score_detail") or {}
    sub = d.get("subscores") or {}
    blocks = [callout(f"Score {job.get('total', '?')}/10 · Track: {job.get('track') or 'n/a'} · "
                      f"{d.get('one_line', '')}", "🧭")]
    if job.get("requires_enrollment") or job.get("is_internship"):
        blocks.append(callout("Check eligibility: this internship may require current student enrollment."))
    if job.get("is_trainee"):
        blocks.append(callout("Graduate or trainee programme: structured onboarding likely.", "🎓"))
    if d.get("why_it_fits"):
        blocks.append(heading("Why it fits", 2))
        blocks += [bullet(x) for x in d["why_it_fits"][:4]]
    if d.get("needs"):
        blocks.append(heading("What the role really needs", 3))
        for n in d["needs"][:6]:
            blocks.append(bullet(rt(n.get("need", ""), bold=True),
                                 rt(f": {n.get('evidence', 'none')} ({n.get('met', '?')})")))
    if d.get("gaps"):
        blocks.append(heading("Gaps", 3))
        for g in d["gaps"][:5]:
            tag = "learnable" if g.get("learnable") else "harder to close"
            blocks.append(bullet(f"{g.get('gap', '')} ({tag}): {g.get('how', '')}"))
    if d.get("red_flags"):
        blocks.append(heading("Red flags", 3))
        blocks += [bullet(x) for x in d["red_flags"][:4]]
    blocks.append(heading("Details", 3))
    blocks.append(bullet(
        f"Sub-scores: skills {sub.get('skills_fit', '?')} · direction {sub.get('direction_fit', '?')} · "
        f"growth {sub.get('growth_onboarding', '?')} · taste {sub.get('taste_match', '?')} · "
        f"feasibility {sub.get('feasibility', '?')} · freshness {job.get('freshness', '?')}"))
    blocks.append(bullet(f"Source: {job.get('source', '')}"))
    if job.get("posted_at"):
        blocks.append(bullet(f"Posted: {job['posted_at']}"))
    if job.get("location"):
        blocks.append(bullet(f"Location as posted: {job['location']}"))
    if job.get("address"):
        blocks.append(bullet(f"Address: {job['address']}"))
    if salary_text(job.get("salary")):
        blocks.append(bullet(f"Salary: {salary_text(job['salary'])}"))
    blocks.append(bullet(rt("Open the posting", link=job.get("url") or None)))
    for other in (job.get("also_posted_on") or [])[:6]:
        if other.get("url"):
            blocks.append(bullet(rt("Also posted on: "), rt(other.get("source") or "another site", link=other["url"])))
    return blocks


# ─── Database access ──────────────────────────────────────────────────────────
class NotionDB:
    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self.offline = not (settings.NOTION_API_KEY and settings.NOTION_DATABASE_ID)
        self.headers = {"Authorization": f"Bearer {settings.NOTION_API_KEY}",
                        "Notion-Version": NOTION_VERSION, "Content-Type": "application/json"}
        self.schema, self.title_prop = {}, "Company"
        if self.offline:
            print("  [Notion] No NOTION_API_KEY / NOTION_DATABASE_ID: running without Notion (dry run).")
            self.dry_run = True
        else:
            self._load_schema()

    # low-level request with retries on rate limits
    def _req(self, method, path, payload=None):
        for attempt in range(5):
            try:
                r = requests.request(method, API + path, headers=self.headers, json=payload, timeout=30)
            except requests.RequestException as e:
                print(f"  [Notion] {e}")
                time.sleep(3)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(float(r.headers.get("Retry-After", 2 + attempt * 2)))
                continue
            if r.status_code >= 400:
                print(f"  [Notion {r.status_code}] {method} {path}: {r.text[:300]}")
                return None
            time.sleep(0.35)  # stay under Notion's ~3 requests/second
            return r.json()
        return None

    def _load_schema(self):
        data = self._req("GET", f"/databases/{settings.NOTION_DATABASE_ID}")
        if not data:
            raise SystemExit("Could not read the Notion database. Is the integration connected to it?")
        self.schema = {name: prop["type"] for name, prop in data["properties"].items()}
        self.title_prop = next((n for n, t in self.schema.items() if t == "title"), "Company")
        for key in ("job_title", "status", "link", "score"):
            if P[key] not in self.schema:
                print(f"  [Notion] Missing property '{P[key]}' ({key}). Check config.yaml.")
        status_prop = data["properties"].get(P["status"], {})
        status_kind = status_prop.get("type")
        if status_kind not in ("select", "status"):
            print(f"  [Notion] '{P['status']}' is a {status_kind} field; it must be select or status.")
        elif status_kind == "status":
            # Notion creates missing options for a select on the fly, but never
            # for a status property: a name that is not already there is refused.
            have = {o["name"] for o in (status_prop.get("status") or {}).get("options", [])}
            want = [ST["suggested"], ST["queue"], ST["not_interested"]]
            missing = [w for w in want if w not in have]
            if missing:
                print(f"  [Notion] '{P['status']}' has no option named " +
                      ", ".join(f"'{m}'" for m in missing) +
                      ". Add it in Notion, or the agent cannot set it.")
        loc_type = self.schema.get(P["location"])
        if loc_type and loc_type not in ("select", "rich_text"):
            print(f"  [Notion] 'Location' is a {loc_type} field; switch it to a select field "
                  "so the agent can fill it. City goes in the page body for now.")

    def has(self, key, *types):
        return self.schema.get(P.get(key, ""), None) in types

    # ── reading ──
    @staticmethod
    def _plain(prop):
        if not prop:
            return None
        t = prop.get("type")
        if t in ("title", "rich_text"):
            return "".join(x.get("plain_text", "") for x in prop.get(t, []))
        if t == "url":
            return prop.get("url")
        if t == "status":
            return (prop.get("status") or {}).get("name")
        if t == "select":
            return (prop.get("select") or {}).get("name")
        if t == "multi_select":
            return [o.get("name") for o in prop.get("multi_select", [])]
        if t == "number":
            return prop.get("number")
        if t == "date":
            return (prop.get("date") or {}).get("start")
        return None

    def all_rows(self):
        if self.offline:
            return []
        rows, cursor = [], None
        while True:
            payload = {"page_size": 100}
            if cursor:
                payload["start_cursor"] = cursor
            data = self._req("POST", f"/databases/{settings.NOTION_DATABASE_ID}/query", payload)
            if not data:
                break
            for page in data.get("results", []):
                props = page.get("properties", {})
                g = lambda key: self._plain(props.get(P.get(key, "")))
                rows.append({
                    "id": page["id"], "created": page.get("created_time", ""),
                    "company": self._plain(props.get(self.title_prop)) or "",
                    "title": g("job_title") or "", "url": g("link") or "",
                    "status": g("status"), "score": g("score"), "date_found": g("date_found"),
                    "next_action": g("next_action") or [], "why_not": g("why_not") or "",
                    "track": g("track"), "location": g("location"), "type": g("type"),
                    "deadline": g("deadline"), "website": g("website") or "",
                })
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return rows

    def page_text(self, page_id) -> str:
        if self.offline:
            return ""
        lines, cursor = [], None
        while True:
            path = f"/blocks/{page_id}/children?page_size=100" + (f"&start_cursor={cursor}" if cursor else "")
            data = self._req("GET", path)
            if not data:
                break
            for b in data.get("results", []):
                t = b.get("type")
                texts = (b.get(t) or {}).get("rich_text", [])
                text = "".join(x.get("plain_text", "") for x in texts)
                if not text:
                    continue
                prefix = {"heading_1": "# ", "heading_2": "## ", "heading_3": "### ",
                          "bulleted_list_item": "- ", "numbered_list_item": "- ", "to_do": "- "}.get(t, "")
                lines.append(prefix + text)
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return "\n".join(lines)

    # ── writing ──
    def _set(self, props, key, types, value):
        name = P.get(key)
        if name and self.schema.get(name) in types:
            props[name] = value

    def _choice(self, key, option: str):
        """Payload for a one-option property, which Notion types as either
        "select" or "status". Status and Type were written as select only, so
        on a database where Status is a status property nothing was written
        and every row came back with no status at all."""
        kind = self.schema.get(P.get(key, ""))
        if kind in ("select", "status"):
            return {kind: {"name": option}}
        return None

    def build_properties(self, job: dict, status=None, only_fill_empty_row=None):
        """Properties for a job. With only_fill_empty_row, fields already
        filled in that row are left alone (used for rows you added)."""
        row = only_fill_empty_row or {}
        props = {}
        if not row.get("company") or row["company"].startswith("New Application"):
            props[self.title_prop] = {"title": [rt(job.get("company") or "Unknown company")]}
        if not row.get("title"):
            self._set(props, "job_title", ("rich_text",), {"rich_text": [rt(job.get("title", ""))]})
        if status:
            self._set(props, "status", ("select", "status"), self._choice("status", status))
        if not row.get("url") and job.get("url"):
            self._set(props, "link", ("url",), {"url": job["url"]})
        if not row.get("type"):
            kind = TYPES["internship"] if job.get("is_internship") else TYPES["job"]
            self._set(props, "type", ("select", "status"), self._choice("type", kind))
        if job.get("loc_option") and not row.get("location"):
            loc_type = self.schema.get(P["location"])
            if loc_type == "select":
                props[P["location"]] = {"select": {"name": job["loc_option"]}}
            elif loc_type == "rich_text":
                props[P["location"]] = {"rich_text": [rt(job["loc_option"])]}
        if not only_fill_empty_row:
            self._set(props, "date_found", ("date",), {"date": {"start": dt.date.today().isoformat()}})
        if job.get("deadline") and not row.get("deadline"):
            self._set(props, "deadline", ("date",), {"date": {"start": job["deadline"]}})
        if job.get("total") is not None:
            self._set(props, "score", ("number",), {"number": job["total"]})
        if job.get("track"):
            self._set(props, "track", ("select", "status"), self._choice("track", job["track"]))
        sal = job.get("salary")
        if isinstance(sal, dict) and sal.get("min") and str(sal.get("currency", "")).upper() == "USD":
            try:
                self._set(props, "salary", ("number",), {"number": float(sal["min"])})
            except (TypeError, ValueError):
                pass
        return props

    def create_job(self, job: dict, status: str):
        if self.dry_run:
            print(f"    [dry run] would add as {status}: {job.get('total')} · {job['title']} @ {job['company']}"
                  f" ({job.get('loc_option')}, {job.get('track')})")
            return "dry-run"
        blocks = job_blocks(job)
        payload = {"parent": {"database_id": settings.NOTION_DATABASE_ID},
                   "properties": self.build_properties(job, status), "children": blocks[:100]}
        page = self._req("POST", "/pages", payload)
        if page and len(blocks) > 100:
            self.append_blocks(page["id"], blocks[100:])
        return page["id"] if page else None

    def update_properties(self, page_id, props):
        if self.dry_run:
            print(f"    [dry run] would update {page_id}: {list(props)}")
            return True
        return self._req("PATCH", f"/pages/{page_id}", {"properties": props}) is not None

    def set_status(self, page_id, status):
        value = self._choice("status", status)
        if not value:
            print(f"  [Notion] '{P['status']}' is not a select or status field; cannot set it")
            return False
        return self.update_properties(page_id, {P["status"]: value})

    def set_next_action(self, page_id, options):
        """Replace the Next Action multi-select with exactly these options."""
        name = P.get("next_action")
        if self.schema.get(name) != "multi_select":
            return False
        return self.update_properties(
            page_id, {name: {"multi_select": [{"name": o} for o in options]}})

    # ── file uploads ──
    def upload_file(self, path, content_type="application/pdf"):
        """Put a file in Notion's storage and return its file_upload id.

        Three steps: ask for an upload, send the bytes to the URL it gives
        back, then the id can be attached to a page or property. Returns None
        on any failure, so a letter still lands as text if this part breaks.
        """
        path = Path(path)
        if self.dry_run:
            print(f"    [dry run] would upload {path.name}")
            return "dry-run"
        created = self._req("POST", "/file_uploads", {
            "filename": path.name, "content_type": content_type})
        if not created or not created.get("upload_url"):
            print(f"  [Notion] Could not start an upload for {path.name}. "
                  "The integration may not have file upload enabled.")
            return None
        try:
            with open(path, "rb") as fh:
                r = requests.post(
                    created["upload_url"],
                    headers={"Authorization": f"Bearer {settings.NOTION_API_KEY}",
                             "Notion-Version": NOTION_VERSION},
                    files={"file": (path.name, fh, content_type)}, timeout=120)
        except requests.RequestException as e:
            print(f"  [Notion] Upload of {path.name} failed: {e}")
            return None
        if r.status_code >= 400:
            print(f"  [Notion {r.status_code}] upload of {path.name}: {r.text[:300]}")
            return None
        time.sleep(0.35)
        return created["id"]

    def attach_file(self, page_id, prop_name, upload_id, display_name):
        """Add an uploaded file to a Files & media property, keeping what's there."""
        # The REST API calls this type "files"; Notion's own schema says "file".
        if self.schema.get(prop_name) not in ("files", "file"):
            print(f"  [Notion] '{prop_name}' is not a Files & media property "
                  f"({self.schema.get(prop_name) or 'missing'}); skipping the attachment.")
            return False
        existing = []
        page = self._req("GET", f"/pages/{page_id}")
        if page:
            current = (page.get("properties", {}).get(prop_name) or {}).get("files", []) or []
            for f in current:
                if f.get("type") == "external":
                    existing.append({"name": f.get("name", "file"), "type": "external",
                                     "external": f["external"]})
                elif f.get("type") == "file_upload":
                    existing.append(f)
                # A "file" entry is Notion-hosted behind an expiring url and
                # cannot be written back, so previous PDFs drop off the
                # property over time. The text version stays on the page.
        files = existing + [{"type": "file_upload", "name": display_name,
                             "file_upload": {"id": upload_id}}]
        return self.update_properties(page_id, {prop_name: {"files": files[-8:]}})

    def append_blocks(self, page_id, blocks):
        if self.dry_run:
            print(f"    [dry run] would add {len(blocks)} blocks to {page_id}")
            return True
        for i in range(0, len(blocks), 100):
            if self._req("PATCH", f"/blocks/{page_id}/children", {"children": blocks[i:i + 100]}) is None:
                return False
        return True

    def trash(self, page_id):
        """Moves a page to Notion's trash (recoverable for 30 days)."""
        if self.dry_run:
            print(f"    [dry run] would move {page_id} to trash")
            return True
        return self._req("PATCH", f"/pages/{page_id}", {"archived": True}) is not None

    def create_child_page(self, parent_page_id, title, blocks):
        if self.dry_run:
            print(f"    [dry run] would create page '{title}' with {len(blocks)} blocks")
            return None
        payload = {"parent": {"page_id": parent_page_id},
                   "properties": {"title": {"title": [rt(title)]}}, "children": blocks[:100]}
        page = self._req("POST", "/pages", payload)
        if page and len(blocks) > 100:
            self.append_blocks(page["id"], blocks[100:])
        return page["id"] if page else None
