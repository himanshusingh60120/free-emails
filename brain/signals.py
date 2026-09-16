"""Turns raw news items into scored business triggers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timezone

TRIGGER_PATTERNS: dict[str, str] = {
    "m_and_a": r"\b(acquir\w*|acquisition|merg(e|er|es|ing)|takeover|buyout|to buy|buys|bought|divest\w*|sells? (its |a )?(unit|division|business)|spin[- ]?off|carve[- ]?out)\b",
    "funding": r"\b(raises?|raised|funding|series [a-f]\b|seed round|investment from|backed by|ipo|goes public|listing|secures? \$|secures? (usd|eur|gbp|inr)|capital injection|growth equity)\b",
    "expansion_geo": r"\b(expands? (into|to|in)|expansion (into|in|to)|enters?|entry into|launch(es|ed)? in|opens? (an? )?(office|hub|branch|subsidiary|headquarters)|new (office|market|region|country)|international expansion|global expansion|expands? (its )?(dealer|distribution|distributor|sales|partner|retail) (network|footprint)|new (dealers?|distributors?) in)\b",
    "facility": r"\b(new (plant|facility|factory|site|warehouse|distribution cent(er|re)|data cent(er|re)|campus|production line|line)|opens? (a |an |its )?(new )?(plant|facility|factory|warehouse|distribution cent(er|re)|data cent(er|re)|site)|breaks? ground|groundbreaking|capacity expansion|expands? (capacity|production|manufacturing)|invests? .{0,40}(plant|facility|factory|capacity))\b",
    "product_launch": r"\b(launch(es|ed|ing)?|unveil(s|ed)?|introduc(es|ed|ing)|rolls? out|debuts?|releases?|new (product|platform|solution|range|line|service|model)|now available)\b",
    "partnership": r"\b(partner(s|ed|ing|ship)? with|partnership|collaborat\w*|alliance|joint venture|\bjv\b|mou|memorandum of understanding|teams? up|distribution agreement|strategic agreement)\b",
    "leadership": r"\b(appoint\w*|names? .{0,40}(ceo|cfo|coo|cmo|cto|chief|president|head|director|vp|vice president)|hires? .{0,40}(as|to lead)|new (ceo|cfo|coo|cmo|cto|chief|president|head)|joins? .{0,30}as|promoted to|steps? down|succeed\w*|takes? the helm)\b",
    "contract_win": r"\b((wins?|won|secures?|secured|lands?|landed|awarded|bags?) (a |an |the |its |new )?([\w$.,-]+ ){0,4}(contracts?|deals?|orders?|tenders?|bids?|framework|mandates?)|selected (by|as) .{0,40}(supplier|partner|vendor|provider)|framework agreements?|contract (with|from)|orders? from|chosen by)\b",
    "pricing": r"\b(pric(e|es|ing) (increases?|hikes?|cuts?|changes?|rises?|war)|(raises?|cuts?|slashes?|hikes?|lowers?|reduces?|increases?) ([\w-]+ ){0,3}prices?|new pricing|pricing (model|strategy)|surcharges?|fixed[- ]fee|subscription (price|model)|usage-based pricing)\b",
    "tariff_trade": r"\b(tariffs?|duties|anti-?dumping|export controls?|import (ban|restrictions?)|trade (war|deal|policy|restrictions?)|sanctions|customs|section 232|section 301)\b",
    "regulation": r"\b(regulat\w*|compliance|directive|legislation|law\b|ban(s|ned)?\b|mandate|fda|epa|ema\b|fca\b|sec\b|approval|approved|clearance|certif\w*|standard(s)? (for|on)|policy)\b",
    "supply_chain": r"\b(supply chain|shortage|supplier|sourcing|nearshor\w*|reshor\w*|onshor\w*|disruption|lead times?|raw material|input costs?|logistics)\b",
    "sustainability": r"\b(sustainab\w*|net[- ]zero|decarboni[sz]\w*|emissions?|carbon|esg|recycl\w*|circular|renewable|green|climate|epr\b|plastic)\b",
    "technology": r"\b(ai\b|artificial intelligence|machine learning|automation|robot\w*|digital\w*|cloud|platform|software|generative|agentic|iot|patent\w*)\b",
    "restructuring": r"\b(layoffs?|lay off|job cuts?|redundanc\w*|restructur\w*|closes? (plant|facility|site|stores?)|closure|bankrupt\w*|chapter 11|administration\b|insolven\w*|cost[- ]cutting|downsiz\w*|profit warning)\b",
    "financial_results": r"\b(earnings|results|revenue|profit|quarter(ly)?|q[1-4]\b|fiscal|guidance|record (sales|year|quarter)|sales (rose|fell|grew|increased|declined)|outlook)\b",
    "hiring": r"\b(hiring|to hire|recruit\w*|jobs|headcount|workforce expansion|new roles|adds? \d+ (jobs|employees|staff))\b",
    "demand": r"\b(demand|orders|backlog|bookings|surge in|growing interest)\b",
    "award": r"\b(award\w*|recogni[sz]ed|honou?r\w*|ranked|named (to|among|one of)|best (places|companies)|(achieves?|attains?|earns?|receives?) .{0,50}(competency|certification|accreditation|status|designation))\b",
    "event": r"\b(conference|trade show|expo|exhibit\w*|webinar|summit|keynote|booth)\b",
}

BASE_STRENGTH = {
    "m_and_a": 1.0, "funding": 0.95, "expansion_geo": 0.95, "facility": 0.9, "product_launch": 0.85,
    "tariff_trade": 0.85, "pricing": 0.85, "leadership": 0.8, "restructuring": 0.8, "regulation": 0.75,
    "supply_chain": 0.8, "partnership": 0.75, "contract_win": 0.75, "technology": 0.65, "sustainability": 0.65,
    "financial_results": 0.6, "hiring": 0.6, "demand": 0.55, "award": 0.3, "event": 0.25, "general": 0.35,
}

# News that should never be used as a cheerful hook, and only very carefully at all.
SENSITIVE_RE = re.compile(
    r"\b(layoffs?|job cuts?|redundanc\w*|bankrupt\w*|chapter 11|insolven\w*|lawsuit|sued|fraud|investigation|probe|"
    r"fined?|penalt(y|ies)|recall\w*|data breach|breach|cyberattack|ransomware|hack\w*|explosion|fire at|accident|"
    r"fatal\w*|death|died|dies|obituary|scandal|strike|walkout|protest|closure|closes|shut(s|ting)? down|profit warning)\b",
    re.I,
)

GENERIC_NEGATIVE_BOOST = {"restructuring"}


@dataclass
class Signal:
    date: str
    title: str
    source: str = ""
    url: str = ""
    snippet: str = ""
    origin: str = "news"  # news | web
    triggers: list[str] = field(default_factory=list)
    primary: str = "general"
    sensitive: bool = False
    about_prospect: bool = False
    age_days: int = 0
    strength: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    def short(self) -> str:
        return f"{self.date} | {self.title}" + (f" ({self.source})" if self.source else "")


def _age_days(date_str: str, today: date) -> int:
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return 999
    return max(0, (today - d).days)


def classify_signal(sig: Signal, company_core: str, prospect_name: str = "", today: date | None = None,
                    max_age_days: int = 90) -> Signal:
    today = today or datetime.now(timezone.utc).date()
    text = f"{sig.title} {sig.snippet}".lower()
    triggers = [name for name, pat in TRIGGER_PATTERNS.items() if re.search(pat, text, re.I)]

    # Reduce false positives: "launch" inside an expansion headline is expansion, results words
    # inside an M&A headline are secondary, and generic tech words are weak on their own.
    if "expansion_geo" in triggers and "product_launch" in triggers and re.search(r"launch(es|ed)? in\b", text):
        triggers.remove("product_launch")
    if "regulation" in triggers and re.search(r"\b(certif\w*|approved vendor|iso \d+)\b", text) and not re.search(r"\b(fda|epa|law|directive|ban|mandate)\b", text):
        triggers.remove("regulation")
        triggers.append("award")

    sig.triggers = triggers or ["general"]
    sig.primary = max(sig.triggers, key=lambda t: BASE_STRENGTH.get(t, 0.3))
    sig.sensitive = bool(SENSITIVE_RE.search(text))
    name = (prospect_name or "").strip().lower()
    sig.about_prospect = bool(name and len(name) > 4 and name in text)
    if sig.about_prospect and "leadership" not in sig.triggers and re.search(r"\b(joins?|appoint\w*|named|promoted|hires?)\b", text):
        sig.triggers.append("leadership")
        sig.primary = "leadership"

    sig.age_days = _age_days(sig.date, today)
    recency = max(0.4, 1.0 - sig.age_days / (max_age_days * 1.6))
    headline = sig.title.lower()
    subject_weight = 1.0 if company_core and company_core in re.sub(r"[^a-z0-9 ]+", " ", headline)[: max(40, len(headline) // 2 + 10)] else 0.8
    origin_weight = 1.0 if sig.origin == "news" else 0.85
    strength = BASE_STRENGTH.get(sig.primary, 0.35)
    if sig.about_prospect and sig.primary == "leadership":
        strength = 1.0  # a new role is the best possible trigger for that person
    if sig.sensitive:
        strength *= 0.35
    sig.strength = round(strength * recency * subject_weight * origin_weight, 3)
    return sig


def classify_all(signals: list[Signal], company_core: str, prospect_full_name: str = "",
                 max_age_days: int = 90) -> list[Signal]:
    out = [classify_signal(s, company_core, prospect_full_name, max_age_days=max_age_days) for s in signals]
    return sorted(out, key=lambda s: s.strength, reverse=True)
