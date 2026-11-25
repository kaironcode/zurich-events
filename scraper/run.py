"""Daily Zurich events check.

Fetches every enabled source in sources.yaml, extracts events with the Claude API
(or parses iCal feeds directly), and upserts them into Supabase.

Env: CLAUDE_CODE_OAUTH_TOKEN (subscription) or ANTHROPIC_API_KEY, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
Optional: MODEL (default claude-haiku-5-5), ONLY=id1,id2 (debug), DRY_RUN=1
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml
from bs4 import BeautifulSoup
from icalendar import Calendar

ROOT = Path(__file__).resolve().parent.parent
TZ = ZoneInfo("Europe/Zurich")
NOW = datetime.now(timezone.utc)
HORIZON_DAYS = 60          # ignore events further out than this
AGG_HORIZON_DAYS = 42      # aggregators: next six weeks only
MAX_TEXT = 45000           # characters of page text sent to Claude
MODEL = os.environ.get("MODEL", "claude-haiku-5-5")
DRY = os.environ.get("DRY_RUN") == "1"

INTERESTS = (
    "literature, readings, book clubs, poetry, writing; philosophy, ethics, debates, "
    "public lectures and talks in the humanities or science; drawing, life drawing, "
    "sketching, painting and art workshops, art exhibitions; classical music, opera, "
    "ballet, chamber music, orchestra; theatre; cheap or free culture and student offers; "
    "social meetups centred on conversation, games or ideas"
)

UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/126.0 Safari/537.36 zurich-events-bot (personal, once daily)",
    "Accept-Language": "de-CH,de;q=0.9,en;q=0.8",
}

# ---------------------------------------------------------------- Supabase

SB_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SB_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")


def sb(method: str, path: str, **kw):
    if DRY:
        return []
    h = {"apikey": SB_KEY, "Content-Type": "application/json"}
    if SB_KEY.startswith("eyJ"):  # legacy JWT keys also go in Authorization; new sb_secret_ keys must not
        h["Authorization"] = f"Bearer {SB_KEY}"
    h.update(kw.pop("headers", {}))
    r = requests.request(method, f"{SB_URL}/rest/v1/{path}", headers=h, timeout=60, **kw)
    if r.status_code >= 300:
        raise RuntimeError(f"Supabase {method} {path}: {r.status_code} {r.text[:300]}")
    return r.json() if r.text else []


def upsert(table: str, rows: list[dict]):
    if rows:
        sb("POST", table, json=rows,
           headers={"Prefer": "resolution=merge-duplicates,return=minimal"})


# ---------------------------------------------------------------- Claude
#
# Two ways to reach Claude:
#  - CLAUDE_CODE_OAUTH_TOKEN set: run the Claude Code CLI ("claude -p"), billed to your
#    Claude subscription (Pro/Max) instead of API credits.
#  - otherwise ANTHROPIC_API_KEY: call the Messages API directly (pay per use).
import subprocess

CLI_MODEL = os.environ.get("CLI_MODEL", "haiku")


def ask_json(instructions: str, content: str, schema: dict, tool: str) -> dict:
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        prompt = (instructions + "\n\nReply with ONLY a JSON object (no prose, no code fences) matching "
                  "this JSON Schema:\n" + json.dumps(schema) + "\n\nThe input follows on stdin.")
        for attempt in range(3):
            p = subprocess.run(
                ["claude", "-p", prompt, "--output-format", "json", "--model", CLI_MODEL,
                 "--max-turns", "1", "--tools", ""],
                input=content, capture_output=True, text=True, timeout=300)
            if p.returncode == 0:
                out = json.loads(p.stdout)
                if out.get("is_error"):
                    raise RuntimeError(f"claude CLI: {out.get('result', '')[:200]}")
                txt = out.get("result", "")
                m = re.search(r"\{.*\}", txt, re.S)
                if m:
                    return json.loads(m.group(0))
                raise RuntimeError(f"claude CLI returned no JSON: {txt[:200]}")
            if "rate" in (p.stderr + p.stdout).lower() or "limit" in (p.stderr + p.stdout).lower():
                time.sleep(30 * (attempt + 1))
                continue
            raise RuntimeError(f"claude CLI exit {p.returncode}: {(p.stderr or p.stdout)[:300]}")
        raise RuntimeError("claude CLI kept hitting limits")

    body = {
        "model": MODEL, "max_tokens": 8000,
        "system": "You extract structured event data for a personal events app in Zurich. "
                  "Accuracy over coverage: when in doubt, leave a field null.",
        "tools": [{"name": tool, "description": "Return the result.", "input_schema": schema}],
        "tool_choice": {"type": "tool", "name": tool},
        "messages": [{"role": "user", "content": instructions + "\n\n--- INPUT ---\n" + content}],
    }
    for attempt in range(3):
        r = requests.post("https://api.anthropic.com/v1/messages",
                          headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                                   "anthropic-version": "2023-06-01", "content-type": "application/json"},
                          json=body, timeout=180)
        if r.status_code in (429, 500, 502, 503, 529):
            time.sleep(10 * (attempt + 1))
            continue
        r.raise_for_status()
        for block in r.json().get("content", []):
            if block.get("type") == "tool_use":
                return block["input"]
        return {}
    raise RuntimeError(f"Claude API kept failing ({r.status_code})")


STATUSES = ["open", "registration_closed", "sold_out", "cancelled", "unknown"]
HIDDEN_STATUSES = {"registration_closed", "sold_out", "cancelled"}
PRICE_DESC = ("Price in CHF that a university student with a Legi pays: if several prices are listed, use the "
              "student/reduced/Legi/under-26 price; otherwise the regular price. 0 if explicitly free. "
              "null only if no price appears anywhere in the text.")
SUMMARY_DESC = "Very brief English description of what the event is, max 12 words, e.g. 'Author reading and talk about her new novel'."
STATUS_DESC = ("cancelled if cancelled/abgesagt/entfällt; sold_out if ausverkauft/sold out/full/no seats; "
               "registration_closed if registration, booking or ticket sales are closed (e.g. 'geschlossen', "
               "'Anmeldung geschlossen', 'closed', 'Verkauf beendet', sale/registration end date passed); "
               "open if tickets/registration are available or entry needs no registration; else unknown. "
               "Words like 'geschlossen' in a venue's opening hours (e.g. 'Montag geschlossen') do NOT count.")
BOOK_DESC = ("True only if this is a book-club discussion where attendees must have read a specific assigned "
             "book beforehand. False for author readings, talks, silent/communal reading sessions, "
             "'bring any book' clubs, and reading-aloud groups that read the text together on site.")

EVENT_SCHEMA = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "start": {"type": ["string", "null"], "description": "ISO 8601 local Zurich time, e.g. 2026-12-02T19:30. Date only if no time."},
                    "end": {"type": ["string", "null"]},
                    "venue": {"type": ["string", "null"]},
                    "price_min": {"type": ["number", "null"], "description": PRICE_DESC},
                    "price_max": {"type": ["number", "null"], "description": "Highest student price in CHF if a range; else same as price_min."},
                    "price_text": {"type": ["string", "null"], "description": "All prices exactly as written, e.g. 'CHF 25 / Studierende CHF 15'."},
                    "registration_deadline": {"type": ["string", "null"], "description": "ISO 8601 if a sign-up/sale deadline is stated."},
                    "url": {"type": ["string", "null"], "description": "Absolute link to this event's own page if present."},
                    "summary": {"type": ["string", "null"], "description": SUMMARY_DESC},
                    "status": {"type": "string", "enum": STATUSES, "description": STATUS_DESC},
                    "assigned_book": {"type": "boolean", "description": BOOK_DESC},
                    "is_offer": {"type": "boolean", "description": "True for a standing discount/membership offer rather than a dated event."},
                },
                "required": ["title", "start", "price_min", "summary", "status", "assigned_book", "is_offer"],
            },
        }
    },
    "required": ["events"],
}


def claude_extract(text: str, src: dict, mode: str = "list") -> list[dict]:
    today = NOW.astimezone(TZ).strftime("%A %Y-%m-%d")
    horizon = AGG_HORIZON_DAYS if src.get("aggregator") else HORIZON_DAYS
    rules = [
        f"Today is {today} (Europe/Zurich). Source: {src['name']} ({src['url']}).",
        "Extract upcoming events from the page text. Only use facts stated in the text: "
        "never guess a date, time, price or deadline. Unknown fields are null.",
        "Infer the year from context when the page omits it (the next occurrence on or after today).",
        f"Skip past events and events more than {horizon} days ahead.",
        "If the page only describes a fixed regular schedule (e.g. 'every Wednesday 19:15') "
        "without dated listings, emit the concrete occurrences within the next 14 days, "
        "using the stated time, and copy any stated price.",
        "Merge duplicates. Keep titles short and in the original language.",
        "Set status from labels like 'Cancelled', 'Abgesagt', 'Sold out', 'Ausverkauft', 'Registration closed', "
        "'Anmeldung geschlossen', 'full'. Always write a brief English summary.",
    ]
    if src.get("aggregator"):
        rules.append(f"This is a large listing: keep ONLY events matching these interests: {INTERESTS}. "
                     "Drop parties, kids' events, sport, business networking, wellness and tourist tours.")
    if src.get("kind") == "offer" or src["category"] == "offer":
        rules.append("This source is about discounts/offers. Return each concrete offer as is_offer=true "
                     "with start=null, plus any dated events it lists as normal events. Only keep offers a "
                     "university student in their early twenties with a student card (Legi) can use, e.g. "
                     "student/under-26/under-30 prices, last-minute tickets, free days, cheap memberships. "
                     "Skip offers that need a bank account, a newspaper or theatre subscription, a club or "
                     "union membership, a disability card, refugee status, group or school-class bookings. "
                     "Merge closely related offers into one item.")
    rules.append("Each event's url must be copied from the <link> next to it in the text, if any.")
    return ask_json("\n".join(rules), text[:MAX_TEXT], EVENT_SCHEMA, "save_events").get("events", [])


DETAIL_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": STATUSES, "description": STATUS_DESC},
        "assigned_book": {"type": "boolean", "description": BOOK_DESC},
        "price_min": {"type": ["number", "null"], "description": PRICE_DESC},
        "price_max": {"type": ["number", "null"]},
        "price_text": {"type": ["string", "null"], "description": "All prices exactly as written."},
        "registration_deadline": {"type": ["string", "null"], "description": "ISO 8601 if a sign-up/sale deadline is stated."},
        "summary": {"type": ["string", "null"], "description": SUMMARY_DESC},
    },
    "required": ["status", "assigned_book", "price_min", "summary"],
}


def claude_detail(text: str, ev: dict) -> dict:
    today = NOW.astimezone(TZ).strftime("%A %Y-%m-%d")
    when = ev.get("start_at") or ""
    rules = [
        f"Today is {today} (Europe/Zurich). This page belongs to ONE event: \"{ev['title']}\" on {when[:16]}.",
        "If the page lists several dates or several events, answer only for this one.",
        "Read the whole text, including the bottom of the page and any ticket/registration box, then return "
        "its status, the student price, any registration/sale deadline and a brief English summary. "
        "Prices are usually written as 'CHF 25', 'Fr. 25.-', '25.00 CHF', 'Eintritt: 15/10' or 'Kosten'. "
        "Never guess; unknown = null.",
    ]
    return ask_json("\n".join(rules), text[:30000], DETAIL_SCHEMA, "event_detail")


def claude_filter(lines: list[str]) -> set[int]:
    """Return indices of listing lines that match INTERESTS."""
    numbered = "\n".join(f"{i}: {l}" for i, l in enumerate(lines))
    schema = {"type": "object", "properties": {"indices": {"type": "array", "items": {"type": "integer"}}},
              "required": ["indices"]}
    res = ask_json(f"Keep only items matching these interests: {INTERESTS}. Drop parties, sport, "
                   "business networking, wellness, dating and tourist tours. Return the indices to keep.",
                   numbered, schema, "keep")
    return set(res.get("indices", []))


# ---------------------------------------------------------------- helpers

def fetch(url: str) -> requests.Response:
    r = requests.get(url, headers=UA, timeout=45)
    r.raise_for_status()
    return r


def page_text(html: str, base: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "svg", "iframe", "form"]):
        t.decompose()
    for t in soup.select("nav, footer, [role=navigation], .cookie, #cookie, .cookies"):
        t.decompose()
    # keep link targets so Claude can attach per-event URLs
    for a in soup.find_all("a", href=True):
        href = requests.compat.urljoin(base, a["href"])
        if href.startswith("http") and a.get_text(strip=True):
            a.replace_with(f"{a.get_text(' ', strip=True)} <{href}>")
    text = soup.get_text("\n", strip=True)
    return re.sub(r"\n{3,}", "\n\n", text)


def parse_dt(s):
    if not s:
        return None, False
    s = str(s).strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            d = datetime.fromisoformat(s).replace(tzinfo=TZ)
            return d, True
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=TZ)
        return d, False
    except ValueError:
        return None, False


def event_id(source_id: str, title: str, start) -> str:
    key = f"{source_id}|{re.sub(r'[^a-z0-9]', '', title.lower())[:80]}|{start.isoformat() if start else ''}"
    return hashlib.sha1(key.encode()).hexdigest()[:20]


PRICE_RE = re.compile(r"(?:CHF|Fr\.?|SFr\.?)\s*(\d+(?:[.,]\d{1,2})?)|(\d+(?:[.,]\d{1,2})?)\s*(?:CHF|Fr\.)", re.I)


def price_from_text(t: str):
    vals = [float((a or b).replace(",", ".")) for a, b in PRICE_RE.findall(t or "")]
    if vals:
        return min(vals), max(vals)
    if re.search(r"\b(free|gratis|kostenlos|eintritt frei|free entry)\b", t or "", re.I):
        return 0.0, 0.0
    return None, None


def to_row(src: dict, e: dict) -> dict | None:
    title = (e.get("title") or "").strip()
    if not title or e.get("assigned_book"):
        return None
    start, all_day = parse_dt(e.get("start"))
    end, _ = parse_dt(e.get("end"))
    is_offer = bool(e.get("is_offer")) and not start
    if not is_offer:
        if not start:
            return None
        horizon = AGG_HORIZON_DAYS if src.get("aggregator") else HORIZON_DAYS
        last = end or (start + timedelta(days=1) if all_day else start)
        if last < NOW or start > NOW + timedelta(days=horizon):
            return None
    deadline, _ = parse_dt(e.get("registration_deadline"))
    pmin, pmax = e.get("price_min"), e.get("price_max")
    if pmin is None and e.get("price_text"):
        pmin, pmax = price_from_text(e["price_text"])
    return {
        "id": event_id(src["id"], title, start),
        "source_id": src["id"],
        "kind": "offer" if is_offer else "event",
        "category": "offer" if is_offer else src["category"],
        "title": title[:200],
        "start_at": start.isoformat() if start else None,
        "end_at": end.isoformat() if end else None,
        "all_day": all_day,
        "venue": (e.get("venue") or None),
        "price_min": pmin,
        "price_max": pmax if pmax is not None else pmin,
        "price_text": e.get("price_text"),
        "registration_deadline": deadline.isoformat() if deadline else None,
        "url": e.get("url") or src["url"],
        "summary": (e.get("summary") or None),
        "status": e.get("status") if e.get("status") in STATUSES else "unknown",
        "ticket_url": e.get("ticket_url"),
        "last_seen": NOW.isoformat(),
    }


# ---------------------------------------------------------------- source kinds

def run_ical(src: dict, prev_fp: str | None):
    raw = fetch(src["url"]).content
    fp = hashlib.sha256(raw).hexdigest()
    if fp == prev_fp:
        return fp, None
    cal = Calendar.from_ical(raw)
    out = []
    for ve in cal.walk("VEVENT"):
        start = ve.decoded("DTSTART", None)
        if start is None:
            continue
        all_day = not isinstance(start, datetime)
        if all_day:
            start = datetime.combine(start, datetime.min.time())
        if start.tzinfo is None:
            start = start.replace(tzinfo=TZ)
        end = ve.decoded("DTEND", None)
        if end is not None and not isinstance(end, datetime):
            end = datetime.combine(end, datetime.min.time())
        if end is not None and end.tzinfo is None:
            end = end.replace(tzinfo=TZ)
        desc = str(ve.get("DESCRIPTION", ""))
        title = str(ve.get("SUMMARY", "")).strip()
        pmin, pmax = price_from_text(desc)
        links = [u for u in re.findall(r"https?://[^\s<>\")\]]+", desc) if "meetup.com" not in u
                 and "whatsapp" not in u and "chat." not in u]
        status = "cancelled" if str(ve.get("STATUS", "")).upper() == "CANCELLED" or "cancel" in title.lower() else "unknown"
        out.append({"ticket_url": links[0] if links else None, "status": status,
            "title": title, "start": start.isoformat(), "end": end.isoformat() if end else None,
            "venue": str(ve.get("LOCATION", "")) or None, "price_min": pmin, "price_max": pmax,
            "price_text": None, "url": str(ve.get("URL", "")) or None,
            "summary": re.sub(r"\s+", " ", desc)[:160] or None, "is_offer": False,
        })
    if src.get("aggregator") and out:
        # let Claude filter the big social feeds by interest (titles only, cheap)
        keep = claude_filter([f"{e['start'][:16]} | {e['title']} | {(e['summary'] or '')[:100]}" for e in out])
        out = [e for i, e in enumerate(out) if i in keep]
    return fp, out


def run_page(src: dict, prev_fp: str | None):
    r = fetch(src["url"])
    text = page_text(r.text, r.url)
    if len(text) < 200:
        raise RuntimeError("page has almost no text (probably rendered by JavaScript)")
    fp = hashlib.sha256(text.encode()).hexdigest()
    if fp == prev_fp:
        return fp, None
    return fp, claude_extract(text, src)


# ---------------------------------------------------------------- detail pass
#
# Opens each event's own page (or the ticket link from a Meetup description) to read
# status, student price, deadline and a short description. New events are checked first,
# then events coming up soon are re-checked so closed registrations disappear.

from concurrent.futures import ThreadPoolExecutor

DETAIL_CAP = int(os.environ.get("DETAIL_CAP", "160"))


def detail_one(ev: dict) -> str:
    target = ev.get("ticket_url") or ev["url"]
    patch = {"checked_at": datetime.now(timezone.utc).isoformat()}
    try:
        r = fetch(target)
        text = page_text(r.text, r.url)
        if len(text) < 150:
            raise RuntimeError("page has almost no text")
        d = claude_detail(text, ev)
    except Exception as ex:
        sb("PATCH", f"events?id=eq.{ev['id']}", json=patch)
        return f"  ? {ev['title'][:50]}: {type(ex).__name__}"
    if d.get("assigned_book"):
        sb("DELETE", f"events?id=eq.{ev['id']}")
        return f"  - {ev['title'][:50]}: book discussion, removed"
    if d.get("status") in STATUSES and d["status"] != "unknown":
        patch["status"] = d["status"]
    if d.get("price_min") is not None:
        patch["price_min"] = d["price_min"]
        patch["price_max"] = d.get("price_max") if d.get("price_max") is not None else d["price_min"]
        if d.get("price_text"):
            patch["price_text"] = d["price_text"]
    if d.get("summary"):
        patch["summary"] = d["summary"][:200]
    if d.get("registration_deadline"):
        dl, _ = parse_dt(d["registration_deadline"])
        if dl:
            patch["registration_deadline"] = dl.isoformat()
    sb("PATCH", f"events?id=eq.{ev['id']}", json=patch)
    return f"  ✓ {ev['title'][:50]}: {patch.get('status', '-')}, {patch.get('price_min', '-')}"


def detail_pass(cfg: list[dict]):
    listing_urls = {s["url"].rstrip("/") for s in cfg}
    now = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    evs = sb("GET", f"events?kind=eq.event&start_at=gte.{now}&select=id,title,start_at,url,ticket_url,status,checked_at,source_id"
                    "&order=start_at.asc&limit=2000")
    soon = NOW + timedelta(days=10)

    def due(e):
        if not (e.get("url") or e.get("ticket_url")):
            return False
        if (e.get("ticket_url") is None) and e["url"].rstrip("/") in listing_urls:
            return False                      # no own page, the listing is all there is
        if e.get("status") in ("cancelled",):
            return False
        if not e.get("checked_at"):
            return True
        age = NOW - datetime.fromisoformat(e["checked_at"])
        start = datetime.fromisoformat(e["start_at"])
        return (start < soon and age > timedelta(hours=20)) or age > timedelta(days=4)

    todo = [e for e in evs if due(e)]
    todo.sort(key=lambda e: (e.get("checked_at") is not None, e["start_at"]))
    todo = todo[:DETAIL_CAP]
    print(f"\nDetail pass: {len(todo)} events")
    with ThreadPoolExecutor(max_workers=4) as pool:
        for line in pool.map(detail_one, todo):
            print(line)


# ---------------------------------------------------------------- main

def main():
    cfg = yaml.safe_load((ROOT / "sources.yaml").read_text())["sources"]
    only = set(filter(None, os.environ.get("ONLY", "").split(",")))
    cfg = [s for s in cfg if s.get("enabled", True) and (not only or s["id"] in only)]
    cfg.sort(key=lambda s: bool(s.get("aggregator")))  # price lookups go to clubs/venues first
    for s in cfg:
        s.setdefault("kind", "page")

    known = {}
    if not DRY:
        upsert("sources", [{k: s[k] for k in ("id", "name", "category", "url", "kind")} for s in cfg])
        known = {r["id"]: r for r in sb("GET", "sources?select=id,fingerprint")}
        existing = {r["id"] for r in sb("GET", f"events?select=id&last_seen=gte.{(NOW - timedelta(days=90)).date()}")}
    else:
        existing = set()

    run = sb("POST", "runs", json={"notes": "running"}, headers={"Prefer": "return=representation"})
    run_id = run[0]["id"] if run else None

    ok = failed = new = 0
    notes = []
    for src in cfg:
        status = {"id": src["id"], "last_checked": NOW.isoformat()}
        try:
            # a manual run for named sources (ONLY=...) re-reads them even if the page is unchanged
            prev = None if only else known.get(src["id"], {}).get("fingerprint")
            fp, items = (run_ical if src["kind"] == "ical" else run_page)(src, prev)
            status["fingerprint"] = fp
            if items is None:
                print(f"= {src['id']}: unchanged")
                if src.get("offer"):
                    o = src["offer"]
                    upsert("events", [to_row(src, {"title": o["title"], "start": None, "is_offer": True,
                                                   "summary": o.get("summary"), "price_min": o.get("price"),
                                                   "price_max": o.get("price"), "url": src["url"]})])
            else:
                if src.get("offer"):
                    o = src["offer"]
                    items = list(items) + [{"title": o["title"], "start": None, "is_offer": True,
                                            "summary": o.get("summary"), "price_min": o.get("price"),
                                            "price_max": o.get("price"), "url": src["url"]}]
                rows = [r for r in (to_row(src, e) for e in items) if r]
                rows = list({r["id"]: r for r in rows}.values())
                # keep what the detail pass already found: a listing never erases a known price,
                # summary, deadline or a closed/cancelled status with an empty value
                if not DRY:
                    cur = {r["id"]: r for r in sb("GET", f"events?source_id=eq.{src['id']}&select="
                                                  "id,price_min,price_max,price_text,summary,registration_deadline,status,ticket_url")}
                    for row in rows:
                        c = cur.get(row["id"])
                        if not c:
                            continue
                        for k in ("price_min", "price_max", "price_text", "summary", "registration_deadline", "ticket_url"):
                            if row.get(k) is None and c.get(k) is not None:
                                row[k] = c[k]
                        if row["status"] not in HIDDEN_STATUSES and c.get("status") in HIDDEN_STATUSES:
                            row["status"] = c["status"]
                        elif row["status"] == "unknown" and c.get("status"):
                            row["status"] = c["status"]
                fresh = [r for r in rows if r["id"] not in existing]
                seen = [r for r in rows if r["id"] in existing]
                new += len(fresh)
                for r in fresh:
                    r["first_seen"] = NOW.isoformat()
                upsert("events", fresh)   # PostgREST needs identical keys within one batch
                upsert("events", seen)
                existing.update(r["id"] for r in fresh)
                # events that vanished from a club/venue page (cancelled, moved) are removed;
                # aggregators are skipped because their interest filter varies from day to day
                if rows and not src.get("aggregator"):
                    sb("DELETE", f"events?source_id=eq.{src['id']}&last_seen=lt.{NOW.strftime('%Y-%m-%dT%H:%M:%SZ')}"
                                 f"&or=(start_at.gte.{NOW.strftime('%Y-%m-%dT%H:%M:%SZ')},kind.eq.offer)")
                print(f"+ {src['id']}: {len(rows)} items, {len(fresh)} new")
            status["last_ok"] = NOW.isoformat()
            status["last_error"] = None
            ok += 1
        except Exception as ex:
            failed += 1
            msg = f"{type(ex).__name__}: {ex}"[:300]
            status["last_error"] = msg
            notes.append(f"{src['id']}: {msg}")
            print(f"! {src['id']}: {msg}")
            traceback.print_exc(limit=1)
        if not DRY:
            sb("PATCH", f"sources?id=eq.{src['id']}", json=status)

    if not DRY:
        detail_pass(cfg)

    if not DRY and not only:
        active = {s["id"] for s in cfg}
        for r in sb("GET", "sources?select=id"):
            if r["id"] not in active:
                sb("DELETE", f"events?source_id=eq.{r['id']}")
                sb("DELETE", f"sources?id=eq.{r['id']}")
                print(f"- removed disabled source {r['id']}")

    # upcoming-count per source, for the settings screen
    if not DRY:
        rows = sb("GET", f"events?select=source_id&or=(start_at.gte.{NOW.strftime('%Y-%m-%dT%H:%M:%SZ')},kind.eq.offer)")
        counts = {}
        for r in rows:
            counts[r["source_id"]] = counts.get(r["source_id"], 0) + 1
        for s in cfg:
            sb("PATCH", f"sources?id=eq.{s['id']}", json={"upcoming": counts.get(s["id"], 0)})
        if run_id:
            sb("PATCH", f"runs?id=eq.{run_id}", json={
                "finished_at": datetime.now(timezone.utc).isoformat(), "sources_ok": ok,
                "sources_failed": failed, "new_events": new, "notes": "\n".join(notes)[:4000] or None})
    print(f"\nDone: {ok} ok, {failed} failed, {new} new events")
    # fail the workflow only if almost everything broke (so GitHub emails you)
    sys.exit(1 if cfg and failed > len(cfg) * 0.6 else 0)


if __name__ == "__main__":
    main()
