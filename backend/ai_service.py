"""External services — Claude AI + Apify + ATS adapters + auth helpers."""

import os
import json
import hmac
import hashlib
import secrets
import urllib.request
import urllib.parse
import urllib.error
from anthropic import Anthropic

# The recruiter sets this in their .env file
client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

MODEL = "claude-opus-4-7"


# ============ HTTP helpers ============

def _http_json(url, method="GET", headers=None, params=None, body=None, timeout=30):
    """Minimal stdlib JSON HTTP client so we avoid adding requests/httpx."""
    if params:
        qs = urllib.parse.urlencode(params)
        url = url + ("&" if "?" in url else "?") + qs
    data = None
    h = {"Accept": "application/json"}
    if headers:
        h.update(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            if not raw:
                return {}
            try:
                return json.loads(raw)
            except Exception:
                return {"_raw": raw}
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code} on {url}: {raw[:300]}")


# ============ Auth helpers (scrypt + session tokens) ============

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt$" + salt.hex() + "$" + h.hex()


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_hex, hash_hex = stored.split("$")
        if algo != "scrypt":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
        h = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(h, expected)
    except Exception:
        return False


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def summarize_conversation(messages: list[dict], candidate_name: str) -> dict:
    """
    Summarize a DM thread and classify the candidate's interest level.
    Returns {"summary": str, "sentiment": str}.
    """
    transcript = "\n".join(
        f"[{m['sender']}]: {m['body']}" for m in messages
    )

    prompt = f"""You are analyzing a LinkedIn DM conversation between a recruiter and a candidate named {candidate_name}.

Conversation transcript:
---
{transcript}
---

Return a JSON object with exactly two fields:
1. "summary": a 2-3 sentence neutral summary of where the conversation stands — what was discussed, what the candidate expressed, what's pending.
2. "sentiment": ONE of these labels based on the candidate's tone:
   - "interested"       (candidate wants to learn more or progress)
   - "maybe"            (open but has questions or concerns)
   - "not_interested"   (explicitly declining or disengaging)
   - "no_response"      (candidate hasn't replied yet)
   - "neutral"          (early / unclear)

Return ONLY the JSON object, no preamble or code fences."""

    response = client.messages.create(
        model=MODEL,
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text.strip()
    # Strip code fences if the model included them
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def draft_reply(
    messages: list[dict],
    candidate_name: str,
    tone: str = "professional",
    recruiter_goal: str = "continue the conversation and move toward a call",
) -> str:
    """
    Generate a reply draft for the last message in the thread.
    Recruiter always reviews before sending.
    """
    transcript = "\n".join(
        f"[{m['sender']}]: {m['body']}" for m in messages
    )

    prompt = f"""You are drafting a LinkedIn reply on behalf of a recruiter. The candidate's name is {candidate_name}.

Conversation so far:
---
{transcript}
---

Task: write the recruiter's next message. Constraints:
- Tone: {tone}
- Goal: {recruiter_goal}
- Keep it under 120 words
- Do NOT use emojis or exclamation marks
- Do NOT start with "Hi {candidate_name}" if the conversation is already ongoing — just continue naturally
- Do NOT invent facts about the role or company that weren't in the conversation
- If the candidate asked a specific question, address it directly

Return ONLY the message body. No preamble, no signature, no quotes, no explanation."""

    response = client.messages.create(
        model=MODEL,
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )

    return response.content[0].text.strip()


# ============ v2 AI functions ============


def _extract_json(text: str) -> dict:
    """Strip ```json fences and parse."""
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        # parts[0] is empty, parts[1] is 'json\n{...}' or '{...}'
        inner = parts[1]
        if inner.lstrip().startswith("json"):
            inner = inner.lstrip()[4:]
        text = inner.strip()
    return json.loads(text)


def parse_jd(jd_text: str, hiring_notes: str = "") -> dict:
    """Turn a raw job description into structured role requirements."""
    prompt = f"""You are a technical recruiter's assistant. Turn this raw job description into a structured, search-ready role spec.

=== JOB DESCRIPTION ===
{jd_text}
=== END ===

{("HIRING MANAGER NOTES:\\n" + hiring_notes) if hiring_notes else ""}

Return a JSON object with EXACTLY these keys:
- "title": clean role title (string)
- "company": company or client name (string, "" if unknown)
- "location": primary location (string, "" if remote or unclear)
- "seniority": one of "junior" | "mid" | "senior" | "lead" | "principal" | "staff" | "director"
- "employment_type": one of "full-time" | "contract" | "part-time" | "internship" | ""
- "must_have": list of 4-8 truly required skills/experiences (short strings, e.g. "5+ yrs Python", "AWS production", "LLM evaluation")
- "nice_to_have": list of 3-6 bonus skills (short strings)
- "target_titles": list of 4-8 current title variations this candidate likely has today (e.g. "Senior ML Engineer", "Staff Software Engineer, Platform")
- "target_companies": list of 5-10 companies likely to have the right talent (guess intelligently from the stack / domain)
- "search_keywords": list of 6-10 boolean-ready search keywords (short, e.g. "MLOps", "Kubernetes", "LangChain")
- "persona": 2-3 sentence description of the ideal candidate
- "outreach_angle": 1-2 sentence pitch angle — why THIS role is interesting to THIS persona
- "exclusions": list of 0-3 things that rule someone out (e.g. "no production ML experience")

Return ONLY the JSON object. No preamble, no code fences, no explanation."""

    response = client.messages.create(
        model=MODEL,
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    return _extract_json(response.content[0].text)


def score_fit(role: dict, candidate: dict) -> dict:
    """Score a candidate against a role. Returns score + reason + risk flags.

    IMPORTANT: This is a prioritization aid for the recruiter, NOT an auto-reject.
    """
    role_block = f"""ROLE
Title: {role.get('title','')}
Company: {role.get('company','')}
Location: {role.get('location','')}
Seniority: {role.get('seniority','')}
Must-have: {role.get('must_have', [])}
Nice-to-have: {role.get('nice_to_have', [])}
Target titles: {role.get('target_titles', [])}
Persona: {role.get('persona','')}
Exclusions: {role.get('exclusions', [])}"""

    cand_block = f"""CANDIDATE
Name: {candidate.get('full_name','')}
Current title: {candidate.get('current_title','')}
Current company: {candidate.get('current_company','')}
Headline: {candidate.get('headline','')}
Location: {candidate.get('location','')}
About: {candidate.get('about','')}"""

    prompt = f"""You are a recruiter's screening assistant. Score this candidate against the role.

{role_block}

{cand_block}

Return a JSON object with EXACTLY these keys:
- "score": integer 0-100. Use this rubric:
    90-100 = exceptional fit, must reach out
    75-89  = strong fit, reach out
    60-74  = good fit, worth a look
    40-59  = maybe, some gaps
    0-39   = weak fit
- "reason": ONE sentence (<= 25 words) explaining the score.
- "bullets": list of 2-4 short evidence bullets supporting the score.
- "risks": list of 0-3 short risk/gap flags (things that might rule this person out or need clarification).

Be honest. Do NOT inflate scores. If data is sparse, say so in risks and score conservatively.
Return ONLY the JSON object."""

    response = client.messages.create(
        model=MODEL,
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    return _extract_json(response.content[0].text)


REPLY_LABELS = [
    "interested",
    "maybe_later",
    "send_jd",
    "salary_mismatch",
    "location_mismatch",
    "not_interested",
    "needs_more_info",
    "refer_someone",
    "already_in_process",
    "no_longer_available",
    "other",
]


def classify_reply(last_message_body: str, prior_context: str = "") -> dict:
    """Classify a candidate's latest reply and suggest the next action."""
    prompt = f"""You are triaging a LinkedIn reply from a candidate to a recruiter's outreach.

{("PRIOR CONTEXT:\\n" + prior_context + "\\n\\n") if prior_context else ""}CANDIDATE'S LATEST REPLY:
---
{last_message_body}
---

Return a JSON object with EXACTLY these keys:
- "label": ONE of {REPLY_LABELS}
- "confidence": integer 0-100
- "suggested_action": ONE short sentence describing what the recruiter should do next
- "suggested_reply": a 2-4 sentence draft reply the recruiter can send. Use a warm professional tone. If the candidate is not interested, keep it brief and thankful. If they asked a question, answer it directly. Do NOT invent facts about roles or compensation.

Return ONLY the JSON object."""

    response = client.messages.create(
        model=MODEL,
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    return _extract_json(response.content[0].text)


def render_template(template_body: str, variables: dict) -> str:
    """Substitute {{var}} placeholders. Missing vars stay as-is (visible to recruiter)."""
    out = template_body
    for k, v in (variables or {}).items():
        out = out.replace("{{" + k + "}}", str(v or ""))
    return out


# ============ Candidate brief (holistic AI summary) ============

def write_candidate_brief(candidate: dict, conversations: list, matches: list) -> dict:
    """Produce a recruiter-to-hiring-manager intro paragraph + strengths + risks + missing info."""
    conv_block = ""
    for c in conversations:
        conv_block += f"\n[{c.get('captured_at','')[:10]}] Summary: {c.get('summary','')}  Sentiment: {c.get('sentiment','')}"
    match_block = "\n".join(
        f"- Role '{m.get('role_title','')}' @ {m.get('role_company','')}: {m.get('fit_score','?')}/100 — {m.get('fit_reason','')}"
        for m in (matches or [])
    )

    prompt = f"""You are writing a 1-paragraph hiring-manager brief about a candidate, from the recruiter's point of view.

Candidate: {candidate.get('full_name','')}
Current title: {candidate.get('current_title','')}
Current company: {candidate.get('current_company','')}
Location: {candidate.get('location','')}
Years experience: {candidate.get('years_experience','unknown')}
Skills: {candidate.get('skills', [])}
Languages: {candidate.get('languages', [])}
Visa status: {candidate.get('visa_status','')}
Open to relocation: {candidate.get('open_to_relocation', False)}
Salary expectation: {candidate.get('salary_expectation','')}
About: {candidate.get('about','')[:800]}

Conversation history (compressed):{conv_block or ' (no conversations yet)'}

Role fit scores:
{match_block or '(none yet)'}

Return a JSON object with EXACTLY these keys:
- "intro": 3-4 sentence polished intro paragraph the recruiter can paste into a Slack or email to the hiring manager. Do NOT invent facts. Reference specific companies/titles only if they appear above.
- "strengths": list of 2-4 short bullets (why this person is worth a look)
- "risks": list of 0-3 short bullets (real risks or gaps based on the data above — not speculation)
- "missing": list of 0-4 data points we don't know yet that would help (e.g. "salary expectation", "notice period")
- "next_action": ONE short sentence recommending the most useful next move

Return ONLY the JSON."""
    response = client.messages.create(
        model=MODEL, max_tokens=900,
        messages=[{"role": "user", "content": prompt}],
    )
    return _extract_json(response.content[0].text)


# ============ Recruiter-flow briefs: conversation + person + pool/role suggestion ============

def analyze_conversation_for_recruiter(
    candidate: dict,
    messages: list,
    pools: list,
    roles: list,
) -> dict:
    """One Claude call that returns EVERYTHING the save-conversation flow needs.

    Inputs:
      candidate : { full_name, headline, current_title, current_company, location, about, skills, ... }
      messages  : [{ sender, body, timestamp? }, ...] (latest-last is fine)
      pools     : [{ id, name, description }]  (recruiter's existing pools)
      roles     : [{ id, title, company, persona, must_have }]  (recruiter's open roles)

    Output JSON (all fields are strings unless noted):
      conversation_brief   : 2-3 sentences describing what's happening in the thread
      sentiment            : interested|maybe|not_interested|no_response|neutral
      person_brief         : 3-4 sentence portrait of the candidate (role, level, seniority signal, domain)
      suggested_pool_id    : integer or null — best matching pool id, from the list provided. null if none fits.
      suggested_pool_reason: short phrase explaining why
      suggested_role_id    : integer or null — best matching role id. null if none fits.
      suggested_role_reason: short phrase
      reply_hint           : 1-2 sentence "what would I send next" hint for the recruiter
    """
    transcript = "\n".join(
        f"[{m.get('sender','?')}]: {m.get('body','')}" for m in (messages or [])
    )[:4000]

    pools_block = "\n".join(
        f"- id={p.get('id')} name={p.get('name')!r} desc={p.get('description','')[:120]!r}"
        for p in (pools or [])
    ) or "(no pools defined yet)"

    roles_block = "\n".join(
        f"- id={r.get('id')} title={r.get('title')!r} company={r.get('company','')!r} persona={r.get('persona','')[:160]!r}"
        for r in (roles or [])
    ) or "(no open roles)"

    prompt = f"""You are a technical recruiter's assistant. A LinkedIn conversation has just been saved.
Your job is to produce a single JSON object that helps the recruiter triage fast.

CANDIDATE PROFILE (from LinkedIn + any prior enrichment):
- Name: {candidate.get('full_name','')}
- Headline: {candidate.get('headline','')}
- Current: {candidate.get('current_title','')} at {candidate.get('current_company','')}
- Location: {candidate.get('location','')}
- Skills: {candidate.get('skills', [])}
- About: {(candidate.get('about','') or '')[:600]}

CONVERSATION TRANSCRIPT (oldest first):
---
{transcript}
---

RECRUITER'S EXISTING POOLS (pick one if clearly relevant, else null):
{pools_block}

RECRUITER'S OPEN ROLES (pick one if the candidate's profile/conversation clearly matches, else null):
{roles_block}

Return ONLY a JSON object with EXACTLY these keys:
- "conversation_brief": 2-3 neutral sentences about where the thread stands (what was discussed, what's pending)
- "sentiment": one of "interested" | "maybe" | "not_interested" | "no_response" | "neutral"
- "person_brief": 3-4 sentence portrait of the candidate (seniority, domain, notable signals)
- "suggested_pool_id": integer id from the list above, or null
- "suggested_pool_reason": short phrase ("matches 'Senior MLE' pool — 6yrs ML at FAANG"), empty string if null
- "suggested_role_id": integer id from the list above, or null
- "suggested_role_reason": short phrase, empty string if null
- "reply_hint": 1-2 sentence hint for the recruiter's next move. Don't draft the full reply — just the angle.

IMPORTANT:
- Only choose a pool_id / role_id that is clearly a strong fit. When in doubt, return null.
- Do NOT invent new pools or roles.
- Return ONLY the JSON. No prose, no code fences.
"""
    response = client.messages.create(
        model=MODEL,
        max_tokens=900,
        messages=[{"role": "user", "content": prompt}],
    )
    return _extract_json(response.content[0].text)


# ============ Apify — LinkedIn profile enrichment (replaces Proxycurl) ============
# Default actor: harvestapi/linkedin-profile-scraper — works on free Apify plan via API.
# Pricing: $4 per 1k profiles (no-email) or $10 per 1k (with email).
# Input shape: { "profileScraperMode": "...", "queries": ["https://linkedin.com/in/..."] }
#
# Legacy actor "2SyF0bVxmgGr8IVCZ" (dev_fusion) is blocked from API runs on the free plan —
# users who pick it will see a clear hint to switch to harvestapi or upgrade.
#
# Users can override the actor id in Settings if they want a different scraper.

APIFY_BASE = "https://api.apify.com/v2"
APIFY_DEFAULT_ACTOR = "harvestapi~linkedin-profile-scraper"
APIFY_DEFAULT_MODE = "Profile details no email ($4 per 1k)"


def _apify_normalize(item: dict, fallback_url: str = "") -> dict:
    """Normalize Apify actor output into our candidate shape.
    Supports harvestapi (default), dev_fusion, apimaestro and similar schemas.
    """
    if not isinstance(item, dict):
        return {"profile_url": fallback_url, "full_name": "Unknown", "source": "apify"}

    full_name = (item.get("fullName") or item.get("name") or item.get("full_name")
                 or (f"{item.get('firstName','')} {item.get('lastName','')}".strip())
                 or "Unknown")

    headline = item.get("headline") or item.get("title") or item.get("occupation") or ""

    current_title = item.get("jobTitle") or item.get("currentJobTitle") or ""
    current_company = item.get("companyName") or item.get("currentCompany") or ""

    cp = item.get("currentPosition")
    if isinstance(cp, list) and cp:
        cp0 = cp[0] if isinstance(cp[0], dict) else {}
        current_title = current_title or cp0.get("title") or cp0.get("position") or ""
        current_company = current_company or (
            (cp0.get("company") or {}).get("name") if isinstance(cp0.get("company"), dict)
            else cp0.get("company") or cp0.get("companyName") or ""
        )
    elif isinstance(cp, dict):
        current_title = current_title or cp.get("title") or ""
        current_company = current_company or (
            (cp.get("company") or {}).get("name") if isinstance(cp.get("company"), dict)
            else cp.get("company") or cp.get("companyName") or ""
        )

    if not current_title or not current_company:
        exps = item.get("experience") or item.get("experiences") or item.get("positions") or []
        if isinstance(exps, list) and exps:
            e0 = exps[0] if isinstance(exps[0], dict) else {}
            current_title = current_title or e0.get("title") or e0.get("position") or ""
            comp = e0.get("company")
            if isinstance(comp, dict):
                current_company = current_company or comp.get("name") or ""
            else:
                current_company = current_company or e0.get("companyName") or comp or ""

    loc = item.get("location") or item.get("addressWithCountry") or \
          item.get("locationName") or item.get("geoLocationName") or ""
    if isinstance(loc, dict):
        location = loc.get("linkedinText") or loc.get("text") or loc.get("name") or ""
    else:
        location = loc or ""

    about = item.get("about") or item.get("summary") or item.get("description") or ""

    skills_raw = item.get("skills") or item.get("topSkills") or []
    if isinstance(skills_raw, list):
        skills = []
        for s in skills_raw:
            if isinstance(s, dict):
                n = s.get("name") or s.get("title") or s.get("skill")
                if n:
                    skills.append(str(n))
            else:
                skills.append(str(s))
    else:
        skills = []

    langs_raw = item.get("languages") or []
    if isinstance(langs_raw, list):
        languages = []
        for l in langs_raw:
            if isinstance(l, dict):
                n = l.get("name") or l.get("language")
                if n:
                    languages.append(str(n))
            else:
                languages.append(str(l))
    else:
        languages = []

    emails_list = item.get("emails") or []
    first_email = ""
    if isinstance(emails_list, list) and emails_list:
        first = emails_list[0]
        first_email = first.get("email") if isinstance(first, dict) else str(first)
    email = item.get("email") or item.get("emailAddress") or first_email or ""
    phone = item.get("phone") or item.get("phoneNumber") or ""

    profile_url = (item.get("linkedinUrl") or item.get("url") or item.get("profileUrl")
                   or fallback_url or "")
    if profile_url:
        profile_url = profile_url.split("?")[0].split("#")[0]

    return {
        "full_name": full_name,
        "profile_url": profile_url,
        "headline": headline,
        "current_title": current_title,
        "current_company": current_company,
        "location": location,
        "about": about,
        "skills": skills,
        "languages": languages,
        "email": email,
        "phone": phone,
        "source": "apify",
        "_raw": item,
    }


def apify_enrich_profile(linkedin_url: str, api_key: str, actor_id: str = APIFY_DEFAULT_ACTOR,
                         timeout: int = 300) -> dict:
    """Run an Apify LinkedIn profile scraper actor synchronously and return one normalized profile."""
    if not api_key:
        raise RuntimeError("Apify API key is not configured. Open Settings to add it.")
    if not linkedin_url:
        raise RuntimeError("No LinkedIn URL provided.")
    actor_id = (actor_id or APIFY_DEFAULT_ACTOR).strip()
    # Apify actor ids can use either "username/name" or "username~name" on REST; normalize.
    actor_path = actor_id.replace("/", "~")

    url = (f"{APIFY_BASE}/acts/{urllib.parse.quote(actor_path)}"
           f"/run-sync-get-dataset-items?token={urllib.parse.quote(api_key)}&timeout={int(timeout)}")

    # Input schema depends on the actor. We send the union of common keys so the
    # right one is picked up whichever actor the user configured.
    body = {
        # harvestapi/linkedin-profile-scraper
        "profileScraperMode": APIFY_DEFAULT_MODE,
        "queries": [linkedin_url],
        # apimaestro/linkedin-profile-batch-scraper-no-cookies-required
        "urls": [linkedin_url],
        # dev_fusion/Mass Linkedin Profile Scraper with Email
        "profileUrls": [linkedin_url],
    }

    try:
        data = _http_json(url, method="POST", body=body, timeout=timeout)
    except Exception as e:
        raise RuntimeError(f"Apify call failed: {e}")

    def _fmt_actor_err(msg: str) -> str:
        m = (msg or "").lower()
        if "free apify plan" in m or ("free plan" in m and "ui" in m) or "users on the free apify plan" in m:
            return (
                "This Apify actor is blocked for API runs on the free plan. "
                "Open Settings and change 'Actor ID' to "
                "'harvestapi/linkedin-profile-scraper' (works on free plan, $4 per 1k) "
                "or upgrade your Apify plan. "
                f"Original message: {msg}"
            )
        return f"Apify actor error: {msg}"

    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(_fmt_actor_err(str(data.get("error"))))
    if isinstance(data, list) and data and isinstance(data[0], dict) and data[0].get("error"):
        raise RuntimeError(_fmt_actor_err(str(data[0].get("error"))))
    items = data if isinstance(data, list) else [data]
    items = [i for i in items if isinstance(i, dict) and (i.get("fullName") or i.get("firstName")
             or i.get("name") or i.get("full_name") or i.get("linkedinUrl") or i.get("publicIdentifier"))]
    if not items:
        raise RuntimeError("Apify actor returned no profile data. "
                           "Check the Actor ID in Settings (recommended: harvestapi/linkedin-profile-scraper).")
    return _apify_normalize(items[0], fallback_url=linkedin_url)


# ============ Greenhouse ATS push ============
# Docs: https://developers.greenhouse.io/harvest.html — Candidates endpoint requires on-behalf-of user id.

GREENHOUSE_BASE = "https://harvest.greenhouse.io/v1"


def greenhouse_push_candidate(api_key: str, on_behalf_of_user_id: str, candidate: dict) -> dict:
    if not api_key or not on_behalf_of_user_id:
        raise RuntimeError("Greenhouse not configured. Add Harvest API key + user id in Settings.")
    import base64
    basic = base64.b64encode(f"{api_key}:".encode()).decode()
    headers = {
        "Authorization": f"Basic {basic}",
        "On-Behalf-Of": str(on_behalf_of_user_id),
    }
    first, _, last = (candidate.get("full_name") or "").partition(" ")
    body = {
        "first_name": first or candidate.get("full_name", ""),
        "last_name": last or ".",
        "title": candidate.get("current_title", ""),
        "company": candidate.get("current_company", ""),
        "tags": (candidate.get("tags") or "").split(",") if candidate.get("tags") else [],
        "phone_numbers": ([{"value": candidate.get("phone"), "type": "mobile"}] if candidate.get("phone") else []),
        "email_addresses": ([{"value": candidate.get("email"), "type": "personal"}] if candidate.get("email") else []),
        "social_media_addresses": ([{"value": candidate.get("profile_url")}] if candidate.get("profile_url") else []),
        "notes": candidate.get("notes", ""),
    }
    return _http_json(f"{GREENHOUSE_BASE}/candidates", method="POST", headers=headers, body=body, timeout=30)


def lever_push_candidate(api_key: str, candidate: dict) -> dict:
    """Minimal Lever push (Postings API, posting id optional). Stub for parity."""
    if not api_key:
        raise RuntimeError("Lever not configured.")
    import base64
    basic = base64.b64encode(f"{api_key}:".encode()).decode()
    headers = {"Authorization": f"Basic {basic}"}
    body = {
        "name": candidate.get("full_name", ""),
        "headline": candidate.get("headline", ""),
        "emails": [candidate["email"]] if candidate.get("email") else [],
        "phones": [{"value": candidate["phone"]}] if candidate.get("phone") else [],
        "links": [candidate["profile_url"]] if candidate.get("profile_url") else [],
    }
    return _http_json("https://api.lever.co/v1/opportunities", method="POST", headers=headers, body=body, timeout=30)


def personalize_outreach(
    role: dict,
    candidate: dict,
    template_body: str = "",
    template_type: str = "first_outreach",
    tone: str = "professional",
) -> str:
    """Generate a personalized outreach message, optionally starting from a template."""
    role_block = (
        f"Role: {role.get('title','')} at {role.get('company','')}. "
        f"Must-have: {role.get('must_have', [])}. "
        f"Angle: {role.get('outreach_angle','')}."
    )
    cand_block = (
        f"Name: {candidate.get('full_name','')}. "
        f"Current: {candidate.get('current_title','')} at {candidate.get('current_company','')}. "
        f"Headline: {candidate.get('headline','')}. "
    )

    rules_by_type = {
        "connection_note": "LinkedIn connection note. Hard limit: 280 characters. ONE sentence explaining why you're reaching out, plus one soft ask. No emojis.",
        "first_outreach": "First DM to a candidate. 80-120 words. Personalize on their current company/title. End with a clear but low-pressure ask (15-min call or yes/no question).",
        "follow_up_1": "Gentle follow-up, assume no reply yet. 50-80 words. Acknowledge they're busy, restate value in one line.",
        "follow_up_2": "Second follow-up, last one. 40-60 words. Offer an easy out (\"feel free to pass\") while keeping the door open.",
        "close_loop": "Closing message when they declined or went silent. 30-50 words. Warm, no pressure, leaves door open for future.",
    }
    rules = rules_by_type.get(template_type, rules_by_type["first_outreach"])

    starter = f"TEMPLATE TO ADAPT (fill the variables and refine, do not copy verbatim):\n{template_body}\n\n" if template_body else ""

    prompt = f"""You are writing an outreach message for a recruiter. Tone: {tone}.

{role_block}

{cand_block}

{starter}TASK: {rules}

Hard rules:
- Do NOT use emojis or exclamation marks.
- Do NOT invent facts (salary, benefits, team size) not given above.
- Do NOT start with "I hope this finds you well" or similar filler.
- If current_company or current_title is empty, do not fake personalization — keep it generic but warm.

Return ONLY the message body. No subject, no signature, no preamble."""

    response = client.messages.create(
        model=MODEL,
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()
