"""Collects dated, company-specific news. Anything older than the cutoff never leaves this module."""

from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import requests

from .config import Settings
from .llm import FatalLLMError, Gemini, LLMError, QuotaExhausted
from .signals import Signal
from .text import company_core, normalise_text, strip_legal_suffix

NEWS_EDITIONS = {
    "united states": ("en-US", "US", "US:en"), "usa": ("en-US", "US", "US:en"), "us": ("en-US", "US", "US:en"),
    "united kingdom": ("en-GB", "GB", "GB:en"), "uk": ("en-GB", "GB", "GB:en"), "england": ("en-GB", "GB", "GB:en"),
    "ireland": ("en-IE", "IE", "IE:en"), "canada": ("en-CA", "CA", "CA:en"), "australia": ("en-AU", "AU", "AU:en"),
    "new zealand": ("en-NZ", "NZ", "NZ:en"), "india": ("en-IN", "IN", "IN:en"), "singapore": ("en-SG", "SG", "SG:en"),
    "south africa": ("en-ZA", "ZA", "ZA:en"), "philippines": ("en-PH", "PH", "PH:en"), "malaysia": ("en-MY", "MY", "MY:en"),
    "nigeria": ("en-NG", "NG", "NG:en"), "kenya": ("en-KE", "KE", "KE:en"), "pakistan": ("en-PK", "PK", "PK:en"),
    "united arab emirates": ("en-AE", "AE", "AE:en"), "uae": ("en-AE", "AE", "AE:en"),
}

NOISE_RE = re.compile(
    r"(\b(sells?|buys?|acquires?|purchases?|trims?|boosts?|lifts?|raises?|lowers?|cuts?|increases?|decreases?) "
    r"(its |their )?(stake|position|holdings?)\b|\bshares? (sold|bought|acquired) by\b|\b[\d,]+ shares\b|"
    r"\bprice target\b|\bshort interest\b|\binstitutional (investors?|ownership)\b|\bstock (price|forecast|analysis)\b|"
    r"\b(upgraded|downgraded) (to|by)\b|\bobituar(y|ies)\b|\bjob (opening|posting)s?\b)",
    re.I,
)

USER_AGENT = "Mozilla/5.0 (compatible; KingsResearchOutreach/1.0)"


@dataclass
class Research:
    signals: list[Signal]
    profile: str
    cutoff: str
    notes: list[str] = field(default_factory=list)
    used_web: bool = False


_CACHE: dict[str, tuple[float, Research]] = {}
_CACHE_TTL = 6 * 3600


def _iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%d")


def is_recent(date_str: str, cutoff: datetime, now: datetime) -> bool:
    try:
        d = datetime.strptime(str(date_str), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    lo = cutoff.replace(hour=0, minute=0, second=0, microsecond=0)
    return lo <= d <= now + timedelta(days=1)


def parse_news_rss(xml: str) -> list[Signal]:
    items: list[Signal] = []
    for block in re.findall(r"<item>([\s\S]*?)</item>", xml or ""):
        def get(tag: str) -> str:
            m = re.search(rf"<{tag}(?:\s[^>]*)?>([\s\S]*?)</{tag}>", block)
            if not m:
                return ""
            value = re.sub(r"<!\[CDATA\[([\s\S]*?)\]\]>", r"\1", m.group(1))
            return html.unescape(value).strip()

        title, source = get("title"), get("source")
        if source and title.endswith(" - " + source):
            title = title[: -(len(source) + 3)]
        try:
            published = parsedate_to_datetime(get("pubDate"))
        except (TypeError, ValueError):
            continue
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        snippet = html.unescape(re.sub(r"<[^>]+>", " ", get("description")))
        snippet = re.sub(r"\s+", " ", snippet).strip()[:300]
        items.append(Signal(date=_iso(published), title=title, source=source, url=get("link"), snippet=snippet, origin="news"))
    return items


def filter_news(items: list[Signal], company: str, cutoff: datetime, now: datetime) -> list[Signal]:
    core = company_core(company)
    compact = core.replace(" ", "")
    seen: set[str] = set()
    out: list[Signal] = []
    for it in items:
        if not is_recent(it.date, cutoff, now):
            continue
        hay = normalise_text(f"{it.title} {it.snippet}")
        if core and core not in hay and compact not in hay.replace(" ", ""):
            continue
        if NOISE_RE.search(it.title):
            continue
        key = normalise_text(it.title)[:70]
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def fetch_google_news(company: str, country: str, cutoff: datetime, now: datetime,
                      http: requests.Session) -> list[Signal]:
    core = company_core(company)
    phrase = strip_legal_suffix(company) if (len(core.split()) >= 2 or len(core) >= 5) else company.strip()
    query = f'"{phrase.replace(chr(34), "")}" after:{_iso(cutoff)}'
    local = NEWS_EDITIONS.get((country or "").strip().lower())
    editions = [local, NEWS_EDITIONS["united states"]] if local and local[1] != "US" else [NEWS_EDITIONS["united states"]]
    items: list[Signal] = []
    for hl, gl, ceid in editions:
        url = f"https://news.google.com/rss/search?q={quote(query)}&hl={hl}&gl={gl}&ceid={quote(ceid)}"
        try:
            resp = http.get(url, timeout=15, headers={"User-Agent": USER_AGENT})
        except requests.RequestException:
            continue
        if resp.status_code != 200:
            continue
        items.extend(parse_news_rss(resp.text))
        if filter_news(items, company, cutoff, now):
            break
    return filter_news(items, company, cutoff, now)


WEB_RESEARCH_PROMPT = """Use Google Search to research this company. Today is {today}.
Company: {company}
{domain_line}{country_line}
Find:
1) profile: 1-2 plain sentences on what the company sells, to whom, its industry, and its size if a source states it (employees, revenue, locations). Only facts you found.
2) events: up to 5 concrete business events published ON OR AFTER {cutoff} (the last {days} days).
   Good events: expansion, new facility or location, acquisition, funding, leadership hire, product launch, new market,
   partnership, major contract, restructuring, pricing change, a regulation or tariff that names or directly affects this company, a hiring push.
   Include an event ONLY if a source you found shows it was published on or after {cutoff}. Never guess dates.
   If you are not sure a result is about this exact company, leave it out.

Return ONLY a JSON object, no markdown:
{{"profile": "...", "events": [{{"date": "YYYY-MM-DD", "headline": "...", "detail": "one sentence", "source": "publication or domain"}}]}}
If nothing recent is found, return "events": []."""


def web_research(company: str, domain: str, country: str, cutoff: datetime, now: datetime, llm: Gemini,
                 settings: Settings, days: int, deadline: float | None) -> tuple[str, list[Signal]]:
    prompt = WEB_RESEARCH_PROMPT.format(
        today=_iso(now), company=company, cutoff=_iso(cutoff), days=days,
        domain_line=f"Website domain: {domain} (use it to confirm you have the right company)\n" if domain else "",
        country_line=f"Country: {country}\n" if country else "",
    )
    data, resp = llm.generate_json(prompt=prompt, search=True, temperature=0.1,
                                   models=[settings.search_model], deadline=deadline)
    chunks = resp.grounding_chunks
    if not chunks:  # the model answered from memory, not from a search: don't trust it
        return "", []
    events: list[Signal] = []
    for ev in data.get("events") or []:
        if not isinstance(ev, dict):
            continue
        date_str, headline = str(ev.get("date", "")), str(ev.get("headline", "")).strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str) or not headline or not is_recent(date_str, cutoff, now):
            continue
        src = str(ev.get("source", "")).lower()
        url = next((c.get("uri", "") for c in chunks
                    if src and c.get("title") and (c["title"].lower() in src or src in c["title"].lower())), "")
        events.append(Signal(date=date_str, title=headline[:220], source=str(ev.get("source", ""))[:80], url=url,
                             snippet=str(ev.get("detail", ""))[:300], origin="web"))
    return str(data.get("profile", ""))[:600], events


def research_company(company: str, domain: str, country: str, llm: Gemini | None, settings: Settings,
                     http: requests.Session, deadline: float | None = None, web_mode: str | None = None) -> Research:
    web_mode = web_mode or settings.web_research
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=settings.max_news_age_days)
    key = "|".join([company_core(company), (country or "").lower(), web_mode, _iso(now)])
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        cached = hit[1]
        return Research(signals=list(cached.signals), profile=cached.profile, cutoff=cached.cutoff, notes=list(cached.notes), used_web=False)

    notes: list[str] = []
    try:
        signals = fetch_google_news(company, country, cutoff, now, http)
    except Exception as exc:  # network oddities should never kill the row
        signals = []
        notes.append(f"news lookup failed: {str(exc)[:60]}")

    profile, used_web = "", False
    if llm and (web_mode == "always" or (web_mode == "fallback" and not signals)):
        used_web = True
        try:
            profile, events = web_research(company, domain, country, cutoff, now, llm, settings, settings.max_news_age_days, deadline)
            signals.extend(events)
        except QuotaExhausted:
            notes.append("web research quota used up today")
        except FatalLLMError:
            raise
        except LLMError as exc:
            notes.append(f"web research failed: {str(exc)[:60]}")

    dedup: dict[str, Signal] = {}
    for s in signals:
        if is_recent(s.date, cutoff, now):
            dedup.setdefault(normalise_text(s.title)[:50], s)
    ordered = sorted(dedup.values(), key=lambda s: s.date, reverse=True)
    result = Research(signals=ordered, profile=profile, cutoff=_iso(cutoff), notes=notes, used_web=used_web)
    if not notes:
        _CACHE[key] = (time.time(), result)
    return result
