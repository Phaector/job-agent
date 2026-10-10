"""Job agent for Phaedra.

Modes
  daily     full run: your links, cover letters, search, score, push to Notion
  light     only your links + cover letters (runs every 2 hours)
  next      move the next N best rows from Queue to Suggested
  patterns  write the monthly patterns report now
  letters   only the cover letters, optionally for one row (--page <id>)
  quiet     archive applications that went quiet (add --dry-run to preview)
  mailcheck test the Gmail connection and list what it can see, nothing else
  note      add feedback to your Notion profile page (--note "point one | point two")

Add --dry-run to print what would happen without writing to Notion.
"""
import argparse
import datetime as dt
import json
import time
from collections import Counter

import filters
import notion_db
import scoring
import settings
import sources
import writer
from notion_db import NotionDB
from settings import CONFIG

ST = CONFIG["notion"]["statuses"]
LIM = CONFIG["limits"]


# ─── State: postings already screened (so they aren't paid for twice) ────────
def load_seen() -> dict:
    try:
        return json.loads(settings.STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def save_seen(seen: dict):
    cutoff = (dt.date.today() - dt.timedelta(days=LIM["seen_retention_days"])).isoformat()
    seen = {k: v for k, v in seen.items() if v.get("d", "") >= cutoff}
    settings.STATE_FILE.parent.mkdir(exist_ok=True)
    settings.STATE_FILE.write_text(json.dumps(seen, indent=0, sort_keys=True), encoding="utf-8")


def load_profile(db) -> str:
    if settings.NOTION_PROFILE_PAGE_ID and not db.offline:
        text = db.page_text(settings.NOTION_PROFILE_PAGE_ID)
        if len(text) > 300:
            print("[Profile] Using your Notion profile page")
            return text
        print("[Profile] Notion profile page looks empty, using profile.md")
    return settings.PROFILE_FILE.read_text(encoding="utf-8")


# ─── Collecting postings ──────────────────────────────────────────────────────
def watchlist_boards(rows, db=None):
    """Config watchlist + boards linked in your Notion DB, seeds and watchlist page."""
    boards = {}
    for ats, slugs in (CONFIG.get("watchlist") or {}).items():
        for slug, name in (slugs or {}).items():
            boards[(ats, slug, False)] = name
    links = [(r.get("url"), r.get("company")) for r in rows]
    seeds = CONFIG.get("seeds", {})
    for group in ("liked", "company_yes_role_too_junior"):
        links += [(s.get("url"), s.get("company")) for s in seeds.get(group, [])]
    links += [(u, None) for u in seeds.get("watch_only", [])]
    for url, company in links:
        info = sources.detect_ats(url or "")
        if info and info.get("slug"):
            key = (info["ats"], info["slug"], info.get("eu", False))
            if not boards.get(key):
                boards[key] = company or None
    boards.update(watchlist_page_boards(db))
    return boards


def watchlist_page_boards(db):
    """Companies she has pasted onto the Company Watchlist page in Notion.

    She adds the careers page she browses, not a feed, so each link is read
    as a job board directly and, failing that, the page is fetched to see
    which one sits behind it. What could not be read is reported rather than
    dropped, since a link silently doing nothing is worse than no link.
    """
    page_id = (CONFIG.get("notion") or {}).get("watchlist_page_id")
    if not db or not page_id:
        return {}
    urls = db.page_links(page_id)
    if not urls:
        return {}
    boards, unread = {}, []
    for url in urls:
        info, note = sources.discover_board(url)
        if info and info.get("slug"):
            boards[(info["ats"], info["slug"], info.get("eu", False))] = None
            print(f"  [Watchlist page] {url} -> {note}")
        else:
            unread.append(f"{url} ({note})")
            print(f"  [Watchlist page] {url}: {note}")
    print(f"[Watchlist page] {len(urls)} link(s), {len(boards)} now watched")
    if unread:
        sources.annotate("warning", "From your Company Watchlist page, these cannot be "
                                    "watched automatically: " + "; ".join(unread[:8]))
    return boards


def collect(rows, db=None):
    jobs = []
    boards = watchlist_boards(rows, db)
    print(f"[Watchlist] Reading {len(boards)} company boards")
    for (ats, slug, eu), name in sorted(boards.items()):
        found = sources.fetch_board(ats, slug, name, eu)
        print(f"  {ats}:{slug} -> {len(found)}")
        jobs += found
        time.sleep(0.5)

    adz = CONFIG["search"]["adzuna"]
    if settings.ADZUNA_APP_ID:
        print("[Adzuna] Searching")
        for country, spec in adz["countries"].items():
            for term in spec["terms"]:
                found = sources.search_adzuna(country, term, spec.get("where", ""),
                                              adz["max_days_old"], adz["results_per_query"])
                jobs += found
                time.sleep(1.2)
            print(f"  {country}: done")
    else:
        print("[Adzuna] No ADZUNA_APP_ID set, skipping")

    serp = CONFIG["search"]["serpapi"]
    if settings.SERPAPI_KEY and serp["queries"]:
        n, qs = serp["max_queries_per_run"], serp["queries"]
        start = (dt.date.today().timetuple().tm_yday * n) % len(qs)
        todays = [qs[(start + i) % len(qs)] for i in range(min(n, len(qs)))]
        print(f"[Google Jobs] {len(todays)} queries")
        for q in todays:
            jobs += sources.search_serpapi(q["q"], q["location"], q.get("gl", "us"))

    fu = sources.search_fashionunited()
    if fu:
        print(f"[FashionUnited] {len(fu)} postings")
    jobs += fu

    for url, source_name in sources.newsletter_job_links():
        job = sources.fetch_posting(url, source=source_name)
        if job:
            jobs.append(job)
        time.sleep(0.5)
    by_source = Counter(j.get("source", "?") for j in jobs)
    be_by_source = Counter(j.get("source", "?") for j in jobs if _is_belgian(j.get("location")))
    print(f"[Collect] {len(jobs)} postings in total")
    for name, n in by_source.most_common():
        print(f"    {name}: {n} ({be_by_source.get(name, 0)} Belgian)")
    sources.annotate("notice", "Collected " + "; ".join(
        f"{name} {n}/{be_by_source.get(name, 0)} BE" for name, n in by_source.most_common()))
    return jobs


# ─── Search → screen → score ─────────────────────────────────────────────────
def _is_belgian(text: str) -> bool:
    """A rough read of whether a posting's location is in Belgium.

    Only used for the run's own bookkeeping, so that a run can say how many
    Belgian postings it saw and where they were lost. The real decision is
    filters.location_bucket.
    """
    option, _tier = filters.location_bucket(text or "")
    return option in ("Antwerp", "Brussels", "Ghent", "Leuven", "Belgium – other")


def _trace_belgium(raw, fresh, kept, scored):
    """Where Belgian postings were lost, reported as a run annotation.

    Run logs are served from a host this agent cannot reach, so without this
    a run that finds nothing Belgian looks the same as a run that found
    twenty and threw them all away.
    """
    at_source = sum(1 for j in raw if _is_belgian(j.get("location")))
    passed = sum(1 for j in fresh if _is_belgian(j.get("location")))
    screened = sum(1 for j in kept if _is_belgian(j.get("location")))
    final = sum(1 for j in scored if j.get("loc_option") in filters.EUROPE)
    line = (f"Belgium this run: {at_source} collected, {passed} past the free filters, "
            f"{screened} past the screen, {final} scored (of {len(scored)} scored in total).")
    print("[Belgium] " + line)
    sources.annotate("notice" if final else "warning", line)

    # The screen is where Belgium loses most of its postings, so say what it
    # rejected and why. Keeping the titles makes it possible to tell a correct
    # rejection from the screen quietly preferring what it already knows.
    kept_urls = {j.get("norm_url") for j in kept}
    rejected = [j for j in fresh
                if _is_belgian(j.get("location")) and j.get("norm_url") not in kept_urls]
    if rejected:
        shown = "; ".join(f"{j['title']} @ {j['company'] or '?'} — {j.get('screen_reason', '?')}"
                          for j in rejected[:12])
        print(f"[Belgium] screen rejected {len(rejected)}: {shown}")
        sources.annotate("warning", f"Screen rejected {len(rejected)} Belgian postings. {shown}")
    return at_source


def search_and_score(rows, profile, taste, seen, db=None):
    raw = collect(rows, db)
    existing_urls = {filters.normalize_url(r["url"]) for r in rows if r.get("url")}
    stats, fresh = Counter(), []
    dropped_be = Counter()
    for job in raw:
        nu = filters.normalize_url(job["url"])
        job["norm_url"] = nu
        if not nu:
            stats["no link"] += 1
        elif nu in existing_urls or filters.matches_existing(job, rows):
            stats["already in Notion"] += 1
        elif nu in seen:
            stats["screened before"] += 1
        else:
            keep, reason = filters.hard_filter(job)
            if keep:
                fresh.append(job)
            else:
                stats[reason] += 1
                if _is_belgian(job.get("location")):
                    dropped_be[reason] += 1
    fresh = filters.dedupe_batch(fresh)
    print("[Filter] " + ", ".join(f"{k}: {v}" for k, v in stats.most_common()))
    print(f"[Filter] {len(fresh)} new postings passed the free filters")
    if dropped_be:
        why = ", ".join(f"{k}: {v}" for k, v in dropped_be.most_common())
        print(f"[Filter] Belgian postings dropped for: {why}")
        sources.annotate("warning", f"Belgian postings dropped at the free filters: {why}")

    kept = scoring.screen(fresh, profile, taste)
    print(f"[Screen] {len(kept)} kept for a full score")
    today = dt.date.today().isoformat()
    for job in fresh:
        seen[job["norm_url"]] = {"d": today, "s": None}

    kept.sort(key=lambda j: (filters.days_old(j.get("posted_at")) is None, filters.days_old(j.get("posted_at")) or 0))
    to_score, later = kept[:LIM["max_deep_scores_per_run"]], kept[LIM["max_deep_scores_per_run"]:]
    for job in later:          # not scored today: let tomorrow's run consider them again
        seen.pop(job["norm_url"], None)

    scored = []
    for job in to_score:
        if job.get("description_is_snippet") or len(job.get("description", "")) < 400:
            full = sources.fetch_posting(job["url"], source=job["source"])
            if full and len(full.get("description", "")) > len(job.get("description", "")):
                job["description"] = full["description"]
                job["address"] = job.get("address") or full.get("address", "")
                job["deadline"] = job.get("deadline") or full.get("deadline", "")
        result = scoring.deep_score(job, profile, taste)
        if not result:             # not scored: let the next run try again
            seen.pop(job["norm_url"], None)
            continue
        scoring.apply_result(job, result)
        if job["is_internship"] and job.get("loc_option") in filters.EUROPE:
            continue
        seen[job["norm_url"]]["s"] = job["total"]
        scored.append(job)
        print(f"  {job['total']:>4} · {job['track'] or '?':<16} · {job['title']} @ {job['company']}")
    _trace_belgium(raw, fresh, kept, scored)
    return scored


# ─── Choosing what goes into Notion ──────────────────────────────────────────
def _in_europe(entry) -> bool:
    item = entry["item"]
    where = item.get("loc_option") if entry["kind"] == "new" else item.get("location")
    return where in filters.EUROPE


def keep_room_for_europe(main_pool, limit, floor):
    """Top N by score, except that European rows get a guaranteed share.

    Every taste seed is a US company, so taste_match quietly favours US roles
    and Belgium loses the top of the list on score alone. This swaps out the
    weakest US picks for the strongest European ones, but only as far as there
    are European candidates to use, and never below their own score floor.
    """
    chosen = main_pool[:limit]
    if not floor:
        return chosen
    europe = [p for p in main_pool if _in_europe(p)]
    missing = min(floor, len(europe)) - sum(1 for p in chosen if _in_europe(p))
    if missing <= 0:
        return chosen
    picked = {id(p) for p in chosen}
    promote = [p for p in europe if id(p) not in picked][:missing]
    droppable = sorted([p for p in chosen if not _in_europe(p)], key=lambda p: p["score"])
    dropped = {id(p) for p in droppable[:len(promote)]}
    kept = [p for p in chosen if id(p) not in dropped] + promote
    if promote:
        print(f"[Select] Kept {len(promote)} place(s) for Europe over higher-scoring US rows")
    return sorted(kept, key=lambda p: -p["score"])


def select_and_push(db, rows, scored):
    """Top N (+ wildcards) become Suggested, the rest of the good ones go to Queue.
    Queue rows from earlier days compete too and can be promoted."""
    pool = []
    for job in scored:
        needed = LIM["min_score_stretch"] if job.get("loc_tier") == "stretch" else LIM["min_score_queue"]
        if job["total"] >= needed:
            pool.append({"score": job["total"], "wild": job.get("track") == "Wildcard", "kind": "new", "item": job})
    queue_rows = [r for r in rows if r.get("status") == ST["queue"] and r.get("score") is not None]
    pool += [{"score": r["score"], "wild": r.get("track") == "Wildcard", "kind": "queue", "item": r}
             for r in queue_rows]

    main_pool = sorted([p for p in pool if not p["wild"]], key=lambda p: -p["score"])
    wild_pool = sorted([p for p in pool if p["wild"] and p["score"] >= LIM["min_score_wildcard"]],
                       key=lambda p: -p["score"])
    chosen = keep_room_for_europe(main_pool, LIM["suggested_per_run"],
                                  LIM.get("min_europe_per_run", 0))
    chosen += wild_pool[:LIM["wildcards_per_run"]]
    chosen_ids = {id(p) for p in chosen}

    added = promoted = queued = 0
    for p in chosen:
        if p["kind"] == "new":
            added += bool(db.create_job(p["item"], ST["suggested"]))
        else:
            promoted += bool(db.set_status(p["item"]["id"], ST["suggested"]))

    queue_count = len(queue_rows) - promoted
    for p in sorted(pool, key=lambda p: -p["score"]):
        if p["kind"] != "new" or id(p) in chosen_ids:
            continue
        if queue_count >= LIM["queue_cap"]:
            break
        if db.create_job(p["item"], ST["queue"]):
            queued += 1
            queue_count += 1
    print(f"[Notion] {added} new Suggested, {promoted} promoted from Queue, {queued} added to Queue")


def expire_queue(db, rows):
    cutoff = (dt.date.today() - dt.timedelta(days=LIM["queue_expiry_days"])).isoformat()
    old = [r for r in rows if r.get("status") == ST["queue"] and (r.get("date_found") or "9999") < cutoff]
    for r in old:
        db.trash(r["id"])
    if old:
        print(f"[Queue] Moved {len(old)} expired Queue rows to trash")


def promote_next(db, rows, count):
    queue_rows = sorted([r for r in rows if r.get("status") == ST["queue"] and r.get("score") is not None],
                        key=lambda r: -r["score"])[:count]
    for r in queue_rows:
        db.set_status(r["id"], ST["suggested"])
    print(f"[Queue] Moved {len(queue_rows)} rows to Suggested")


def add_profile_note(db, note: str):
    """Append dated feedback bullets to the Notion profile page, which the scorer reads."""
    points = [p.strip() for p in note.split("|") if p.strip()]
    if not points:
        print("[Note] Nothing to add: pass --note \"point one | point two\"")
        return
    if not settings.NOTION_PROFILE_PAGE_ID:
        print("[Note] NOTION_PROFILE_PAGE_ID is not set")
        return
    blocks = [notion_db.heading(f"Feedback {dt.date.today():%Y-%m-%d}", 3)]
    blocks += [notion_db.bullet(p) for p in points]
    if db.append_blocks(settings.NOTION_PROFILE_PAGE_ID, blocks):
        print(f"[Note] Added {len(points)} points to your profile page")
    else:
        raise SystemExit("[Note] Could not write to your profile page")


# ─── Entry point ──────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Phaedra's job agent")
    ap.add_argument("--mode", default="daily",
                    choices=["daily", "light", "letters", "quiet", "next", "patterns", "note", "mailcheck"])
    ap.add_argument("--count", type=int, default=10, help="rows to promote in mode 'next'")
    ap.add_argument("--note", default="", help="feedback for mode 'note'; separate points with |")
    ap.add_argument("--page", default="", help="one Notion page id, for mode 'letters'")
    ap.add_argument("--dry-run", action="store_true", help="print instead of writing to Notion")
    args = ap.parse_args()

    print(f"\n=== Job agent · {args.mode} · {dt.datetime.now():%Y-%m-%d %H:%M} ===\n")

    if args.mode == "mailcheck":       # before Notion: needs no key and writes nothing
        raise SystemExit(0 if sources.mail_probe() else 1)

    db = NotionDB(dry_run=args.dry_run)
    rows = db.all_rows()
    print(f"[Notion] {len(rows)} rows in your database")

    if args.mode == "note":
        add_profile_note(db, args.note)
        return

    if args.mode == "next":
        promote_next(db, rows, args.count)
        return

    profile = load_profile(db)
    taste = scoring.taste_block(rows)

    if args.mode == "patterns":
        writer.patterns_report(db, rows, profile)
        return

    if args.mode == "quiet":
        writer.archive_quiet_applications(db, rows, dry_run=args.dry_run)
        return

    if args.mode == "letters":
        writer.cover_letters(db, rows, profile, only_page=args.page or None)
        return

    writer.enrich_manual_rows(db, rows, profile, taste)
    writer.fill_company_websites(db, rows)
    writer.cover_letters(db, rows, profile)
    writer.archive_quiet_applications(db, rows)
    if args.mode == "light":
        return

    seen = load_seen()
    scored = search_and_score(rows, profile, taste, seen, db)
    select_and_push(db, rows, scored)
    expire_queue(db, rows)
    if not db.dry_run:
        save_seen(seen)
    if dt.date.today().day == 1:
        writer.patterns_report(db, rows, profile)
    print("\n=== Done. Open your Suggested view in Notion. ===")


if __name__ == "__main__":
    main()
