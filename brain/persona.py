"""Reads a job title and works out what the person owns, how senior they are and how likely they are to buy research."""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict

# Order matters: the first function whose pattern matches wins, so specific functions sit above broad ones.
FUNCTION_PATTERNS: list[tuple[str, str]] = [
    ("student", r"\b(student|intern|internship|trainee|apprentice|graduate student|phd candidate)\b"),
    ("admin", r"\b(assistant|receptionist|secretary|office manager|administrator|admin|clerk|coordinator|scheduler|data entry)\b"),
    ("research_insights", r"\b(market research|insights?|market intelligence|competitive intelligence|business intelligence|research analyst|research manager|research director|intelligence analyst|market analyst|strategic insights)\b"),
    ("investment", r"\b(private equity|venture capital|venture partner|investment (director|manager|analyst|partner|professional)|portfolio manager|m ?& ?a|mergers|acquisitions|deal team|fund manager)\b"),
    ("strategy", r"\b(strategy|corporate development|corp dev|business planning|strategic planning|strategic initiatives|transformation|chief of staff|planning director|head of planning)\b"),
    ("sustainability", r"\b(sustainability|esg|environment(al)?|climate|decarboni[sz]ation|net zero|ehs|hse)\b"),
    ("legal_compliance", r"\b(legal|counsel|compliance|regulatory|government affairs|public affairs|public policy|policy)\b"),
    ("procurement", r"\b(procurement|purchasing|sourcing|buyer|supply chain|supply|logistics|category manager|vendor management|materials manager)\b"),
    ("finance", r"\b(cfo|chief financial|finance|financial|treasur(y|er)|controller|fp&a|accounting|investor relations)\b"),
    ("product", r"\b(products?|portfolio|category|merchandis\w*)\b"),
    ("marketing", r"\b(cmo|marketing|brand|communications|comms|growth|demand gen|digital marketing|content|pr manager|public relations)\b"),
    ("sales", r"\b(cro|sales|business development|bdm|bdr|sdr|commercial|revenue|account (executive|manager|director)|key account|partnerships?|channel|alliances|export)\b"),
    ("technology", r"\b(cto|cio|ciso|chief technology|chief information|chief digital|technology|it director|it manager|information technology|r ?& ?d|research and development|innovation|engineering|engineer|digital|data|ai|software|architect|scientist|technical)\b"),
    ("operations", r"\b(coo|chief operating|operations|operational|plant|manufacturing|production|factory|site director|site manager|quality|maintenance|facilities)\b"),
    ("hr", r"\b(hr|human resources|people|talent|recruit\w*|chro|learning|payroll)\b"),
    ("executive", r"\b(ceo|chief executive|president|founder|co-?founder|managing director|md|general manager|gm|chair(man|woman|person)?|partner|principal|proprietor|executive director|representative director|representative executive officer|country manager|head of business|board member|director general)\b"),
]

SENIORITY_PATTERNS: list[tuple[str, str]] = [
    ("c_level", r"\b(ceo|cfo|coo|cmo|cto|cio|cro|chro|ciso|chief|president|founder|co-?founder|chair(man|woman|person)?|managing director|executive director|representative director|representative executive officer|proprietor|partner)\b"),
    ("vp", r"\b(vp|vice president|svp|evp|avp|general manager|country manager)\b"),
    ("head", r"\b(head of|head,|global head|director general|principal)\b"),
    ("director", r"\b(director)\b"),
    ("manager", r"\b(manager|lead|leader|supervisor)\b"),
    ("junior", r"\b(junior|associate|assistant|intern|trainee|coordinator|executive)\b"),
    ("senior_ic", r"\b(senior|specialist|analyst|consultant|engineer|architect|scientist|advisor|adviser)\b"),
]

SENIORITY_WEIGHT = {"c_level": 1.0, "vp": 1.0, "head": 0.95, "director": 0.9, "manager": 0.75,
                    "senior_ic": 0.6, "junior": 0.45, "unknown": 0.7}

FUNCTION_BUYER_LIKELIHOOD = {
    "research_insights": 0.95, "strategy": 0.95, "executive": 0.9, "investment": 0.85, "marketing": 0.85,
    "product": 0.8, "sales": 0.8, "procurement": 0.8, "finance": 0.75, "operations": 0.7, "technology": 0.7,
    "sustainability": 0.7, "legal_compliance": 0.6, "other": 0.5, "hr": 0.35, "education_staff": 0.2, "admin": 0.15, "student": 0.0,
}

# What this person is typically measured on. Used to frame the problem in their language.
FUNCTION_CONCERNS = {
    "executive": "growth, margin and where to place the next big bet",
    "strategy": "which markets and moves deserve investment, with numbers the board will accept",
    "marketing": "positioning, demand and understanding buyers better than competitors do",
    "sales": "pipeline, win rates and knowing which accounts to prioritise",
    "product": "building what customers will pay for and beating competitor roadmaps",
    "procurement": "input costs, supplier options and supply risk",
    "operations": "capacity, cost and resilience across sites",
    "finance": "capital allocation, margin and defensible business cases",
    "investment": "deal flow, diligence speed and conviction on market assumptions",
    "technology": "which technologies and vendors to back, and when",
    "sustainability": "meeting regulatory and customer sustainability requirements without hurting margin",
    "legal_compliance": "staying ahead of regulatory change and its commercial impact",
    "research_insights": "delivering more intelligence to stakeholders with a stretched team",
    "hr": "hiring and talent availability",
    "admin": "day-to-day coordination",
    "student": "study",
    "education_staff": "student services",
    "other": "commercial performance",
}


@dataclass
class Persona:
    title: str
    function: str
    seniority: str
    buyer_role: str  # decider | influencer | user | non_buyer
    fit: float  # 0..1
    concerns: str

    def to_dict(self) -> dict:
        return asdict(self)


def _normalise(title: str) -> str:
    t = (title or "").lower()
    t = t.replace("&amp;", "&")
    t = re.sub(r"[\u2013\u2014/|,;()]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def detect_function(title: str) -> str:
    t = _normalise(title)
    if not t:
        return "other"
    # "Chief X Officer" is an executive of X: map the X first.
    chief = re.search(r"\bchief (\w+)(?: \w+)? officer\b", t)
    if chief:
        word = chief.group(1)
        mapping = {"executive": "executive", "operating": "operations", "financial": "finance", "marketing": "marketing",
                   "technology": "technology", "information": "technology", "digital": "technology", "revenue": "sales",
                   "commercial": "sales", "strategy": "strategy", "procurement": "procurement", "sustainability": "sustainability",
                   "product": "product", "people": "hr", "legal": "legal_compliance", "compliance": "legal_compliance",
                   "data": "technology", "growth": "marketing", "supply": "procurement", "investment": "investment"}
        if word in mapping:
            return mapping[word]
    if re.search(r"^(co-?|business |franchise )?owner\b", t) or re.search(r"\b(and|&) (co-?)?owner$", t):
        return "executive"
    if re.search(r"\b(area|regional|territory) vice president\b|\b(area|regional) vp\b", t):
        return "sales"
    if re.search(r"\b(student|intern|trainee|apprentice)\b", t) and re.search(r"\b(director|dean|head|manager|coordinator|officer|advisor|adviser|counselor|counsellor|services)\b", t):
        return "education_staff"
    if re.search(r"\bstrategic (accounts?|partnerships?|sales|alliances)\b", t):
        return "sales"
    if re.search(r"\bstrategic marketing\b", t):
        return "marketing"
    for name, pattern in FUNCTION_PATTERNS:
        if re.search(pattern, t):
            # "Executive Assistant to the CEO" is admin, not executive; "Account Executive" is sales.
            if name == "admin" and re.search(r"\b(director|head|vp|vice president|chief)\b", t) and not re.search(r"\bassistant\b", t):
                continue
            return name
    return "other"


def detect_seniority(title: str) -> str:
    t = _normalise(title)
    if re.search(r"^(co-?|business |franchise )?owner\b", t) or re.search(r"\b(and|&) (co-?)?owner$", t):
        return "c_level"
    if re.search(r"\bassistant to\b|\bexecutive assistant\b", t):
        return "junior"
    if re.search(r"\baccount executive\b|\bsales executive\b", t):
        return "senior_ic"
    for name, pattern in SENIORITY_PATTERNS:
        if re.search(pattern, t):
            if name == "c_level" and re.search(r"\bvice president\b", t):
                return "vp"
            if name == "junior" and re.search(r"\bassociate (director|vice president|vp|partner)\b", t):
                continue
            return name
    return "unknown"


def analyze_persona(title: str) -> Persona:
    function = detect_function(title)
    seniority = detect_seniority(title)
    base = FUNCTION_BUYER_LIKELIHOOD.get(function, 0.5)
    fit = round(base * SENIORITY_WEIGHT.get(seniority, 0.7), 2)
    if function in ("student",):
        buyer_role = "non_buyer"
    elif (seniority in ("c_level", "vp", "head") and fit >= 0.6) or (seniority == "director" and fit >= 0.72):
        buyer_role = "decider"
    elif fit >= 0.45:
        buyer_role = "influencer"
    elif fit >= 0.25:
        buyer_role = "user"
    else:
        buyer_role = "non_buyer"
    return Persona(title=title or "", function=function, seniority=seniority, buyer_role=buyer_role,
                   fit=fit, concerns=FUNCTION_CONCERNS.get(function, FUNCTION_CONCERNS["other"]))
