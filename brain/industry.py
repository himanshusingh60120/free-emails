"""Infers the prospect company's industry, whether it sells to consumers, and roughly how big it is."""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict

INDUSTRY_KEYWORDS: dict[str, str] = {
    "investment_firm": r"\b(private equity|venture capital|capital partners|asset management|investment firm|family office|hedge fund|\bpe firm|portfolio companies)\b",
    "packaging_printing": r"\b(packaging|printing|print|label(s)?|carton|corrugated|paperbox|paper box|folding box|flexible packaging|printers?)\b",
    "semiconductors_electronics": r"\b(semiconductor\w*|chips?|wafer|electronics|pcb|lithograph\w*|asml|microelectronics)\b",
    "automotive": r"\b(automotive|vehicle\w*|\bev\b|car maker|carmaker|auto parts|tier 1 supplier|sunroof|oem parts|motors)\b",
    "aerospace_defense": r"\b(aerospace|defen[cs]e|aviation|aircraft|drones?|uas|uav|counter-uas|radar|space|satellite\w*|airspace)\b",
    "chemicals_materials": r"\b(chemical\w*|polymer\w*|resin\w*|coatings?|materials|additives|glass|plastics?|specialty chemicals|algal|bio-based)\b",
    "energy": r"\b(energy|oil|gas|lng|solar|wind|hydrogen|battery|batteries|utility|utilities|power generation|renewables?|nuclear|grid)\b",
    "healthcare_life_sciences": r"\b(pharma\w*|biotech\w*|life sciences?|medical|medtech|health\s?care|hospital|clinic\w*|diagnostic\w*|genom\w*|sequencing|therapeut\w*|drug|devices)\b",
    "financial_services": r"\b(bank\w*|insurance|insurer|fintech|payments?|lending|credit|wealth|brokerage|financial services)\b",
    "logistics": r"\b(logistics|freight|shipping|trucking|3pl|warehous\w*|courier|transport(ation)?|supply chain services)\b",
    "telecom": r"\b(telecom\w*|5g|mobile network|broadband|carrier|isp|fiber|fibre)\b",
    "technology": r"\b(software|saas|cloud|\bai\b|data platform|cyber\w*|it services|digital|platform|app|analytics|tech|technologies|gtm intelligence|devops)\b",
    "construction_real_estate": r"\b(construction|contractor|engineering and construction|real estate|property|homebuilder|architect\w*|infrastructure|building materials|excavat\w*|heavy equipment)\b",
    "agriculture_food": r"\b(agri\w*|farm\w*|crop|seeds?|fertili[sz]er|food ingredients|food additives|dairy|meat|poultry|aquaculture)\b",
    "mining_metals": r"\b(mining|mine|metals?|steel|aluminium|aluminum|copper|lithium|ore|smelter)\b",
    "consumer": r"\b(retail\w*|consumer|fmcg|cpg|packaged foods?|snacks?|confectionery|beverage\w*|food and beverage|restaurant\w*|hospitality|hotel\w*|fashion|apparel|beauty|cosmetic\w*|personal care|household|e-?commerce|grocery|brand(s)? for|hair styling|toys?|furniture|home goods|travel)\b",
    "manufacturing": r"\b(manufactur\w*|industrial|machinery|equipment|factory|fabricat\w*|components|precision|foundry|tooling)\b",
    "professional_services": r"\b(consult\w*|advisory|law firm|legal services|accounting|staffing|recruit\w*|agency|marketing services|engineering services|design services)\b",
    "education_public": r"\b(university|college|school|academy|education|government|ministry|municipal|council|public sector|non-?profit|charity|foundation)\b",
}

B2C_INDUSTRIES = {"consumer"}
B2C_HINTS = re.compile(r"\b(sells? (directly )?to consumers|consumer products company|consumer brand|shoppers?|retail customers|households|b2c|d2c|direct-to-consumer|retail stores|restaurants?|hotels?|app users)\b", re.I)
# "supplies consumer brands" describes a B2B supplier, so strip those phrases before looking for consumer signals.
SUPPLIER_TO_CONSUMER_RE = re.compile(r"\b(suppl\w*|serv\w*|for|to|customers include|clients include|works with) (major |leading |global |large |top )?(consumer|fmcg|cpg|retail\w*) (brands|goods|packaged goods|companies|manufacturers|clients|customers|retailers)\b", re.I)

LARGE_HINTS = re.compile(
    r"\b(nyse|nasdaq|lse|tse|ftse|s&p 500|fortune 500|publicly traded|listed on|multinational|global leader|"
    r"world'?s largest|billion|\d{1,3},\d{3} employees|[1-9]\d{3,} employees|operations in \d{2,} countries|"
    r"tier 1|group\b|holdings\b|corporation\b)", re.I)
SMALL_HINTS = re.compile(
    r"\b(family[- ]owned|family business|small business|local|regional|single[- ]site|boutique|independent|"
    r"start-?up|\b[1-9]\d? employees|founded in (19|20)\d\d by)", re.I)


@dataclass
class CompanyRead:
    industry: str
    b2c: bool
    scale: str  # small | mid | large
    evidence: str

    def to_dict(self) -> dict:
        return asdict(self)


def infer_company(company: str, domain: str, profile: str, signal_texts: list[str],
                  news_count: int, major_trigger_count: int) -> CompanyRead:
    name_text = f"{company} {domain.replace('.', ' ')}".lower()
    profile = SUPPLIER_TO_CONSUMER_RE.sub(" ", profile or "")
    body = f"{profile} {' '.join(signal_texts)}".lower()

    scores: dict[str, float] = {}
    for industry, pattern in INDUSTRY_KEYWORDS.items():
        name_hits = len(re.findall(pattern, name_text, re.I))
        profile_hits = len(re.findall(pattern, profile.lower(), re.I))
        signal_hits = len(re.findall(pattern, " ".join(signal_texts).lower(), re.I))
        score = name_hits * 3.0 + profile_hits * 2.0 + min(signal_hits, 4) * 0.5
        if score:
            scores[industry] = score
    # "technology" and "manufacturing" match almost everything; only win when clearly stronger.
    for broad in ("technology", "manufacturing"):
        if broad in scores and len(scores) > 1:
            scores[broad] *= 0.6
    industry = max(scores, key=scores.get) if scores else "unknown"

    b2c = industry in B2C_INDUSTRIES or bool(B2C_HINTS.search(profile or ""))
    if industry in ("professional_services", "investment_firm", "semiconductors_electronics", "education_public"):
        b2c = False

    large = len(LARGE_HINTS.findall(body)) + (1 if news_count >= 5 else 0) + (1 if major_trigger_count >= 2 else 0)
    small = len(SMALL_HINTS.findall(body)) + (1 if news_count == 0 else 0)
    if large >= 2 and large > small:
        scale = "large"
    elif small >= 2 or (small >= 1 and large == 0 and news_count == 0):
        scale = "small"
    else:
        scale = "mid"

    evidence = f"industry scores {dict(sorted(scores.items(), key=lambda kv: -kv[1])[:3])}; large hints {large}, small hints {small}, news items {news_count}"
    return CompanyRead(industry=industry, b2c=b2c, scale=scale, evidence=evidence)
