"""AI scoring in two steps.

1. screen(): a cheap model looks at title, company, location and a snippet
   and throws out clear misfits. When unsure, it keeps the job.
2. deep_score(): a stronger model reads the full posting, lists what the
   role really needs (including unstated skills), matches each need to
   evidence in the profile, and gives sub-scores. The total is computed
   here in code with the weights from config.yaml, so it stays consistent.
"""
import json
import re
import time

import anthropic

import filters
import settings
from settings import CONFIG

_client = None


def client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    return _client


def ask(model: str, system: str, user: str, max_tokens: int = 1500) -> str:
    for attempt in range(4):
        try:
            resp = client().messages.create(
                model=model, max_tokens=max_tokens, system=system,
                messages=[{"role": "user", "content": user}])
            if resp.stop_reason == "max_tokens":
                print(f"    [Claude] answer cut off at {max_tokens} tokens")
            return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError) as e:
            wait = 10 * (attempt + 1)
            print(f"    [Claude] {type(e).__name__}, retrying in {wait}s")
            time.sleep(wait)
        except anthropic.APIStatusError as e:
            print(f"    [Claude] {e.status_code}: {str(e)[:200]}")
            return ""
    return ""


def parse_json(text: str, expect: str = "object"):
    t = re.sub(r"```(?:json)?", "", text or "").strip()
    opener, closer = ("[", "]") if expect == "array" else ("{", "}")
    start, end = t.find(opener), t.rfind(closer)
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(t[start:end + 1])
    except json.JSONDecodeError:
        return None


# ─── Taste ────────────────────────────────────────────────────────────────────
def taste_block(rows: list) -> str:
    """Your yes/no examples: recent Notion rows first, seeds as backup."""
    st = CONFIG["notion"]["statuses"]
    n = CONFIG["limits"]["taste_examples_each"]
    rows = sorted(rows, key=lambda r: r.get("created", ""), reverse=True)
    yes = [f"{r['company']} | {r['title']}" for r in rows
           if r.get("status") in st["counts_as_yes"] and r.get("title")][:n]
    no = [f"{r['company']} | {r['title']}" + (f" (why not: {r['why_not']})" if r.get("why_not") else "")
          for r in rows if r.get("status") == st["not_interested"] and r.get("title")][:n]
    seeds = CONFIG.get("seeds", {})
    for s in seeds.get("liked", []):
        if len(yes) >= n:
            break
        line = f"{s['company']} | {s['title']}"
        if line not in yes:
            yes.append(line)
    junior = [f"{s['company']} | {s['title']}" for s in seeds.get("company_yes_role_too_junior", [])]
    parts = ["ROLES SHE SAID YES TO:\n" + ("\n".join(f"- {x}" for x in yes) or "- (none yet)")]
    if junior:
        parts.append("RIGHT COMPANY, BUT ROLE TOO JUNIOR (aim above this level):\n" +
                     "\n".join(f"- {x}" for x in junior))
    parts.append("ROLES SHE SAID NO TO:\n" + ("\n".join(f"- {x}" for x in no) or "- (none yet)"))
    return "\n\n".join(parts)


# ─── Step 1: quick screen ─────────────────────────────────────────────────────
SCREEN_SYSTEM = """You screen job postings for one candidate before a detailed review.

CANDIDATE PROFILE:
{profile}

HER TASTE SO FAR:
{taste}

Keep a job if a graduate with this profile could realistically do it at entry level AND it fits her directions, resembles roles she said yes to, or would be a smart wildcard for a business engineering plus management graduate. Reject clear misfits: roles needing a specialist degree she lacks (engineering, law, medicine, design portfolio), senior roles, store-floor sales, and fields with no link to her skills or interests. Judge the substance of the role, never keyword overlap. When unsure, keep.

Return ONLY a JSON array: [{{"i": <number>, "keep": true or false, "reason": "<max 12 words>"}}]"""


def screen(jobs: list, profile: str, taste: str) -> list:
    if not jobs:
        return []
    if not settings.ANTHROPIC_API_KEY:
        print("  [Screen] No ANTHROPIC_API_KEY: keeping everything.")
        return jobs
    system = SCREEN_SYSTEM.format(profile=profile, taste=taste)
    kept = []
    for start in range(0, len(jobs), 20):
        batch = jobs[start:start + 20]
        lines = [f"{i + 1}. {j['title']} | {j['company']} | {j['location']} | "
                 f"{j.get('description', '')[:350]}" for i, j in enumerate(batch)]
        answer = parse_json(ask(settings.MODEL_SCREEN, system, "\n".join(lines), 1500), "array")
        if not isinstance(answer, list):
            kept += batch  # if the screen fails, let the deep score decide
            continue
        verdicts = {int(a.get("i", 0)): a for a in answer if isinstance(a, dict)}
        for i, j in enumerate(batch, 1):
            v = verdicts.get(i, {"keep": True})
            if v.get("keep", True):
                kept.append(j)
        time.sleep(0.5)
    return kept


# ─── Step 2: deep score ───────────────────────────────────────────────────────
DEEP_SYSTEM = """You are a sharp career advisor scoring one job posting for one candidate.

CANDIDATE PROFILE:
{profile}

HER TASTE (learn from these, do not copy them):
{taste}

HOW TO SCORE
1. List up to 6 things the role really needs, including unstated ones. A showroom associate needs client handling and logistics coordination even if the posting only says "support the showroom". For each need, find concrete evidence in her profile (a job, project, course or skill) or say there is none.
2. Give sub-scores from 1 to 10:
   - skills_fit: how well those needs are evidenced in her profile.
   - direction_fit: 10 = business role at a fashion-tech company, or fashion business/operations close to product, ateliers or suppliers. 8 = business role at a tech/AI company or at a fashion or luxury company. 5 = solid business role elsewhere that builds relevant skills. 2 = unrelated.
   - growth_onboarding: 10 = graduate or trainee programme, or explicit structured training and mentoring, or a large group with internal mobility. 5 = no information. 2 = "hit the ground running", solo role, no team.
   - taste_match: similarity to roles she said yes to versus no to.
   - feasibility: 10 = realistic entry-level hire today. Subtract for required years she lacks, hard credentials, or daily client work in French at native level (her French is fluent but rusty: small penalty only). Learnable tools such as CLO 3D, PLM systems or a specific CRM: small penalty only.
3. Track: "Fashion business", "Tech business", "Fashion-tech" (a company combining both), or "Wildcard" (outside her directions but a smart fit for her transferable skills).
4. Flag internships and whether they require current student enrollment. Extract a deadline, salary and street address only if the posting states them.

Write reasons as specific evidence ("ran onboarding deliverables at TechWolf"), never generic praise. Be honest: a weak fit gets low scores.

Return ONLY this JSON:
{{
  "needs": [{{"need": "...", "evidence": "... or none", "met": "yes|partly|no"}}],
  "subscores": {{"skills_fit": 0, "direction_fit": 0, "growth_onboarding": 0, "taste_match": 0, "feasibility": 0}},
  "track": "Fashion business|Tech business|Fashion-tech|Wildcard",
  "one_line": "<what the job is, max 20 words>",
  "why_it_fits": ["...", "...", "..."],
  "gaps": [{{"gap": "...", "learnable": true, "how": "..."}}],
  "red_flags": ["..."],
  "is_internship": false,
  "requires_enrollment": false,
  "is_graduate_programme": false,
  "deadline": "YYYY-MM-DD or empty",
  "salary": {{"min": null, "max": null, "currency": ""}},
  "address": "",
  "posting_language": "en|nl|fr|other"
}}"""


def deep_score(job: dict, profile: str, taste: str):
    if not settings.ANTHROPIC_API_KEY:
        return None
    system = DEEP_SYSTEM.format(profile=profile, taste=taste)
    posted = job.get("posted_at") or "unknown"
    user = (f"TITLE: {job['title']}\nCOMPANY: {job['company']}\nLOCATION: {job['location']}\n"
            f"POSTED: {posted}\nEMPLOYMENT TYPE: {job.get('employment_type') or 'unknown'}\n\n"
            f"POSTING:\n{(job.get('description') or '(no description available)')[:7000]}")
    result = parse_json(ask(settings.MODEL_DEEP, system, user, 4000), "object")
    if not isinstance(result, dict) or not isinstance(result.get("subscores"), dict):
        print(f"    [Score] could not read the answer for {job['title']} @ {job['company']}; retrying next run")
        return None
    return result


def freshness_score(job: dict) -> int:
    age = filters.days_old(job.get("posted_at"))
    if age is None:
        score = 5
    elif age <= 2:
        score = 10
    elif age <= 5:
        score = 8
    elif age <= 10:
        score = 6
    elif age <= 20:
        score = 4
    else:
        score = 2
    applicants = job.get("applicants")
    if isinstance(applicants, int):
        score += 1 if applicants < 25 else (-2 if applicants > 200 else 0)
    return max(1, min(10, score))


def compute_total(subscores: dict, job: dict) -> float:
    w = CONFIG["weights"]
    total = 0.0
    for key in ("skills_fit", "direction_fit", "growth_onboarding", "taste_match", "feasibility"):
        try:
            value = float(subscores.get(key, 5))
        except (TypeError, ValueError):
            value = 5.0
        total += max(1.0, min(10.0, value)) * w[key]
    total += freshness_score(job) * w["freshness"]
    return round(total, 1)


def apply_result(job: dict, result: dict) -> dict:
    """Copy the deep-score result onto the job dict."""
    job["score_detail"] = result
    job["track"] = result.get("track", "")
    job["freshness"] = freshness_score(job)
    job["total"] = compute_total(result["subscores"], job)
    job["is_internship"] = bool(job.get("is_internship")) or bool(result.get("is_internship"))
    job["requires_enrollment"] = bool(result.get("requires_enrollment"))
    job["is_trainee"] = bool(job.get("is_trainee")) or bool(result.get("is_graduate_programme"))
    if not job.get("deadline") and re.match(r"^\d{4}-\d{2}-\d{2}$", str(result.get("deadline", ""))):
        job["deadline"] = result["deadline"]
    if not job.get("address") and result.get("address"):
        job["address"] = result["address"]
    sal = result.get("salary") or {}
    if not job.get("salary") and isinstance(sal, dict) and sal.get("min"):
        job["salary"] = sal
    return job
