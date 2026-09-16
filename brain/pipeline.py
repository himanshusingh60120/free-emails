"""The thinking brain, end to end, for one prospect.

  1. Persona      what the person owns, how senior, how likely to buy
  2. Research     dated company news from the last 90 days (Google News, then Gemini + Google Search)
  3. Signals      what each news item means as a business trigger, and how strong it is
  4. Company read industry, B2B/B2C and rough scale
  5. Decision     score all 16 offering pillars and their services against the evidence
  6. Strategist   LLM picks one candidate, one service and builds the argument (deep mode)
  7. Writer       LLM drafts the 4-email sequence from that brief
  8. Quality gate framework rules, grammar, spelling, facts; editor repair if needed
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

from . import prompts
from .config import BRITISH_SPELLING_COUNTRIES, Settings
from .decision import Candidate, Decision, decide, get_pillar, pillar_from_signal_cell
from .industry import CompanyRead, infer_company
from .llm import FatalLLMError, Gemini, LLMError
from .persona import Persona, analyze_persona
from .quality import Issue, check_draft, issue_score, needs_repair, tidy_draft
from .research import Research, research_company
from .signals import Signal, classify_all
from .text import clean_first_name, company_core, display_company, email_domain


@dataclass
class Prospect:
    fname: str = ""
    lname: str = ""
    email: str = ""
    job_title: str = ""
    company: str = ""
    country: str = ""
    timezone: str = ""


@dataclass
class Colleague:
    title: str
    subject: str
    signal_cell: str = ""

    @property
    def pillar_id(self) -> str:
        return pillar_from_signal_cell(self.signal_cell)


@dataclass
class Knowledge:
    proof: list[str] = field(default_factory=list)
    assets: list[str] = field(default_factory=list)


@dataclass
class RowOptions:
    mode: str = "deep"  # deep | fast
    web_research: str = "fallback"
    skip_low_fit: bool = True
    signoff: str = ""


def next_quarter_label(now: datetime) -> str:
    """Next quarter starting at least 45 days away, e.g. mid-September gives Q1."""
    q = (now.month - 1) // 3
    for k in (1, 2):
        qi = q + k
        start = datetime(now.year + qi // 4, (qi % 4) * 3 + 1, 1, tzinfo=timezone.utc)
        if (start - now).days >= 45:
            return f"Q{qi % 4 + 1}"
    return f"Q{(q + 2) % 4 + 1}"


# ---------------------------------------------------------------------------
# Context builders
# ---------------------------------------------------------------------------

def _signals_block(signals: list[Signal]) -> str:
    if not signals:
        return "(none found in the last 90 days)"
    lines = []
    for i, s in enumerate(signals):
        flags = []
        if s.sensitive:
            flags.append("SENSITIVE: do not use as a hook")
        if s.about_prospect:
            flags.append("about the prospect")
        if s.origin == "web":
            flags.append("from web search")
        lines.append(f"[{i}] {s.date} ({s.age_days} days ago) | {s.title}" + (f" | {s.source}" if s.source else "")
                     + (f" | {s.snippet}" if s.snippet and s.snippet.lower() != s.title.lower() else "")
                     + f" | triggers: {', '.join(s.triggers)} | strength {s.strength}" + (f" | {'; '.join(flags)}" if flags else ""))
    return "\n".join(lines)


def _candidates_block(decision: Decision) -> str:
    lines = []
    for i, c in enumerate(decision.candidates):
        services = "; ".join(f"{s.name} (score {s.score})" for s in c.services)
        lines.append(
            f"[{i}] {c.pillar_name} | score {c.score} | signal_index {c.signal_index}\n"
            f"    services: {services}\n"
            f"    why: {' / '.join(c.why)}\n"
            f"    pillar outcome: {c.pitch}\n"
            f"    asset ideas: {' | '.join(c.assets)}"
        )
    return "\n".join(lines)


def _kb_block(kb: Knowledge) -> str:
    proof = "\n".join(f"- {p}" for p in kb.proof) or "(empty: make no claims about past clients or results)"
    assets = "\n".join(f"- {a}" for a in kb.assets) or "(empty: only offer a small tailored outline or sample)"
    return f"APPROVED_PROOF:\n{proof}\n\nAPPROVED_ASSETS:\n{assets}"


def _colleagues_block(colleagues: list[Colleague]) -> str:
    if not colleagues:
        return "(none)"
    return "\n".join(f"- {c.title} | offer: {c.pillar_id or 'unknown'} | subject: {c.subject}" for c in colleagues)


def build_strategist_prompt(p: Prospect, persona: Persona, company: CompanyRead, research: Research,
                            signals: list[Signal], decision: Decision, kb: Knowledge, colleagues: list[Colleague],
                            now: datetime) -> str:
    return f"""TODAY: {now:%Y-%m-%d}
NEXT_QUARTER: {next_quarter_label(now)}

PROSPECT
first_name: {clean_first_name(p.fname) or '(unknown)'}
job_title: {p.job_title or '(unknown)'}
company: {display_company(p.company)}
company_domain: {email_domain(p.email) or '(unknown)'}
country: {p.country or '(unknown)'}

PERSONA (pre-analysed)
function: {persona.function} | seniority: {persona.seniority} | buyer role: {persona.buyer_role} | fit: {persona.fit}
typically measured on: {persona.concerns}

COMPANY READ (pre-analysed, may be imperfect)
industry: {company.industry} | sells to consumers: {'yes' if company.b2c else 'no'} | scale: {company.scale}
profile: {research.profile or '(no verified profile: do not assume specifics about what the company does)'}

SIGNALS (all published on or after {research.cutoff})
{_signals_block(signals)}

CANDIDATES (pre-scored from the evidence, best first)
{_candidates_block(decision)}

COLLEAGUE_ANGLES (other contacts at this company already emailed)
{_colleagues_block(colleagues)}

{_kb_block(kb)}"""


def build_writer_prompt(p: Prospect, persona: Persona, company: CompanyRead, research: Research, signal: Signal | None,
                        brief: dict, pillar_name: str, kb: Knowledge, colleagues: list[Colleague], now: datetime) -> str:
    spelling = "british" if (p.country or "").strip().lower() in BRITISH_SPELLING_COUNTRIES else "american"
    signal_text = (f"{signal.date} | {signal.title}" + (f" | {signal.snippet}" if signal.snippet else "")) if signal else "(none: role-based email, claim no specific event)"
    return f"""COMPANY_NAME: {display_company(p.company)}
SPELLING: {spelling}
NEXT_QUARTER: {next_quarter_label(now)}

PROSPECT
job_title: {p.job_title} ({persona.function}, {persona.seniority})
industry: {company.industry} | scale: {company.scale}
profile: {research.profile or '(none)'}

CHOSEN SIGNAL
{signal_text}

STRATEGY BRIEF
offering: {pillar_name} > {brief['service']}
hook_fact: {brief['hook_fact'] or '(none)'}
role_bottleneck: {brief['role_bottleneck']}
business_stake: {brief['business_stake']}
offer_plain: {brief['offer_plain']}
outcome: {brief['outcome']}
e2_angle: {brief['e2_angle']}
e2_asset: {brief['e2_asset']}
e3_topic: {brief['e3_topic']}
e4_reason: {brief['e4_reason']}
subject_idea: {brief['subject_idea']}

SUBJECTS ALREADY USED AT THIS COMPANY (do not reuse)
{chr(10).join('- ' + c.subject for c in colleagues) or '(none)'}

{_kb_block(kb)}"""


# ---------------------------------------------------------------------------
# Brief validation
# ---------------------------------------------------------------------------

def _match_service(name: str, candidate: Candidate) -> str:
    pillar = get_pillar(candidate.pillar_id)
    wanted = (name or "").strip().lower()
    for s in pillar.services:
        if s.name.lower() == wanted:
            return s.name
    tokens = set(re.findall(r"[a-z]{3,}", wanted))
    best, best_overlap = candidate.services[0].name, 0
    for s in pillar.services:
        overlap = len(tokens & set(re.findall(r"[a-z]{3,}", s.name.lower())))
        if overlap > best_overlap:
            best, best_overlap = s.name, overlap
    return best


def _clip(v, n: int) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()[:n]


def normalise_brief(raw: dict, decision: Decision, signals: list[Signal], persona: Persona) -> dict:
    try:
        ci = int(raw.get("candidate_index", 0))
    except (TypeError, ValueError):
        ci = 0
    if not 0 <= ci < len(decision.candidates):
        ci = 0
    cand = decision.candidates[ci]
    try:
        si = int(raw.get("signal_index", cand.signal_index))
    except (TypeError, ValueError):
        si = cand.signal_index
    if not (-1 <= si < len(signals)) or (si >= 0 and signals[si].sensitive):
        si = cand.signal_index if cand.signal_index >= 0 and not signals[cand.signal_index].sensitive else -1

    hook = _clip(raw.get("hook_fact"), 240) if si >= 0 else ""
    if si >= 0:
        source_numbers = set(re.findall(r"\d[\d.,]*", f"{signals[si].title} {signals[si].snippet}"))
        if not hook or any(n not in source_numbers for n in re.findall(r"\d[\d.,]*", hook)):
            hook = signals[si].title
    try:
        confidence = max(0.0, min(1.0, float(raw.get("confidence", 0.5))))
    except (TypeError, ValueError):
        confidence = 0.5
    pillar = get_pillar(cand.pillar_id)
    return {
        "candidate_index": ci,
        "pillar_id": cand.pillar_id,
        "pillar_name": cand.pillar_name,
        "service": _match_service(raw.get("service", ""), cand),
        "signal_index": si,
        "hook_fact": hook,
        "role_bottleneck": _clip(raw.get("role_bottleneck"), 260) or f"For this role the pressure is usually {persona.concerns}.",
        "business_stake": _clip(raw.get("business_stake"), 140),
        "offer_plain": _clip(raw.get("offer_plain"), 220) or pillar.pitch,
        "outcome": _clip(raw.get("outcome"), 160),
        "e2_angle": _clip(raw.get("e2_angle"), 240),
        "e2_asset": _clip(raw.get("e2_asset"), 160) or pillar.assets[0],
        "e3_topic": _clip(raw.get("e3_topic"), 90) or cand.services[0].name.lower(),
        "e4_reason": _clip(raw.get("e4_reason"), 200),
        "subject_idea": _clip(raw.get("subject_idea"), 60),
        "confidence": round(confidence, 2),
        "reasoning": _clip(raw.get("reasoning"), 420),
    }


def fallback_brief(decision: Decision, signals: list[Signal], persona: Persona, why: str) -> dict:
    return normalise_brief({"reasoning": f"Fallback to the top-scored candidate ({why}).", "candidate_index": 0,
                            "confidence": 0.4}, decision, signals, persona)


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def assemble(p: Prospect, draft: dict, signoff: str) -> dict:
    name = clean_first_name(p.fname)
    hi = f"Hi {name}," if name else "Hi there,"
    sign = "\n\n" + signoff.replace("\\n", "\n").strip() if signoff.strip() else ""
    e3 = draft["e3_body"]
    if name:
        e3 = re.sub(r"^(Is|Are|Would|Does|Do|Should|Has|Have|Will|Can|Could|What|When|How)\b", lambda m: m.group(0).lower(), e3)
        e3 = f"Hi {name}, {e3}"
    else:
        e3 = e3[:1].upper() + e3[1:]
    return {
        "subject": draft["e1_subject"],
        "e1": f"{hi}\n\n{draft['e1_body']}{sign}",
        "e2": f"{hi}\n\n{draft['e2_body']}{sign}",
        "e3": e3,
        "e4": f"{hi}\n\n{draft['e4_body']}{sign}",
    }


def signal_cell(signal: Signal | None, research: Research, brief: dict, persona: Persona) -> str:
    parts = []
    if signal:
        parts.append(f"{signal.date} | {signal.title}" + (f" ({signal.source})" if signal.source else "")
                     + (" | found via web search, verify before sending" if signal.origin == "web" else "")
                     + (f" | {signal.url}" if signal.url else ""))
    elif research.signals:
        parts.append("News found, but none was a strong or appropriate hook; role-based angle")
    else:
        parts.append(f"No company news since {research.cutoff}; role-based angle")
    parts.append(f"offer: {brief['pillar_name']} > {brief['service']}")
    parts.append(f"fit: {persona.buyer_role} {persona.fit:.2f}")
    if research.notes:
        parts.append("note: " + ", ".join(research.notes))
    return " | ".join(parts)


def dossier_text(persona: Persona, company: CompanyRead, decision: Decision, brief: dict, status: str) -> str:
    lines = [
        f"DECISION: {brief['pillar_name']} > {brief['service']} (confidence {brief['confidence']})",
        f"WHY: {brief['reasoning']}",
        f"PERSONA: {persona.title} | {persona.function}, {persona.seniority}, {persona.buyer_role}, fit {persona.fit:.2f}",
        f"COMPANY: {company.industry}, {'B2C' if company.b2c else 'B2B'}, {company.scale}",
        f"BOTTLENECK: {brief['role_bottleneck']}",
        f"STAKE: {brief['business_stake']} | OUTCOME: {brief['outcome']}",
        "SHORTLIST:",
    ]
    for i, c in enumerate(decision.candidates, 1):
        lines.append(f"  {i}. {c.pillar_name} ({c.score}): {c.services[0].name}; {' / '.join(c.why)}")
    if decision.notes:
        lines.append("NOTES: " + "; ".join(decision.notes))
    lines.append(f"QA: {status}")
    return "\n".join(lines)


def hooks_text(signals: list[Signal]) -> str:
    if not signals:
        return ""
    return "\n".join(
        f"{s.date} [{s.primary}, strength {s.strength}{', SENSITIVE' if s.sensitive else ''}{', web' if s.origin == 'web' else ''}] {s.title}"
        + (f" ({s.source})" if s.source else "") + (f" {s.url}" if s.url else "")
        for s in signals
    )


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

def run_prospect(p: Prospect, *, settings: Settings, llm: Gemini, http: requests.Session | None = None,
                 knowledge: Knowledge | None = None, colleagues: list[Colleague] | None = None,
                 options: RowOptions | None = None, now: datetime | None = None) -> dict:
    started = time.monotonic()
    deadline = started + settings.row_time_budget
    now = now or datetime.now(timezone.utc)
    http = http or requests.Session()
    kb = knowledge or Knowledge()
    colleagues = colleagues or []
    options = options or RowOptions(mode=settings.brain_mode, web_research=settings.web_research, signoff=settings.signoff)
    timings: dict[str, float] = {}

    def mark(stage: str) -> None:
        timings[stage] = round(time.monotonic() - started, 1)

    label = f"{clean_first_name(p.fname) or p.fname} @ {display_company(p.company)}".strip()
    if not p.company.strip():
        return {"label": label, "skipped": True, "status": "Skipped: no company on this row"}

    persona = analyze_persona(p.job_title)
    if options.skip_low_fit and persona.fit < settings.min_fit:
        return {"label": label, "skipped": True, "persona": persona.to_dict(),
                "status": f"Skipped: low-fit role ({persona.function}, fit {persona.fit:.2f})"}

    research = research_company(p.company, email_domain(p.email), p.country, llm, settings, http,
                                deadline=deadline, web_mode=options.web_research)
    mark("research")
    full_name = f"{p.fname} {p.lname}".strip()
    signals = classify_all(research.signals, company_core(p.company), full_name, settings.max_news_age_days)
    major = sum(1 for s in signals if s.primary in ("m_and_a", "funding", "expansion_geo", "facility", "financial_results"))
    company = infer_company(p.company, email_domain(p.email), research.profile,
                            [f"{s.title} {s.snippet}" for s in signals], len(research.signals), major)
    colleague_pillars = [c.pillar_id for c in colleagues if c.pillar_id]
    decision = decide(persona, company, signals, colleague_pillars)
    mark("decision")

    kb_has_proof = bool(kb.proof)
    brief: dict
    draft: dict | None = None
    if options.mode == "fast":
        try:
            raw, _ = llm.generate_json(system=prompts.FAST_SYSTEM, schema=prompts.FAST_SCHEMA, temperature=0.6,
                                       prompt=build_strategist_prompt(p, persona, company, research, signals, decision, kb, colleagues, now)
                                       + f"\n\nCOMPANY_NAME: {display_company(p.company)}\nSPELLING: "
                                       + ("british" if (p.country or "").strip().lower() in BRITISH_SPELLING_COUNTRIES else "american"),
                                       deadline=deadline)
            brief = normalise_brief(raw, decision, signals, persona)
            draft = {k: raw.get(k, "") for k in prompts.EMAIL_FIELDS}
        except FatalLLMError:
            raise
        except LLMError as exc:
            brief = fallback_brief(decision, signals, persona, str(exc)[:60])
    else:
        try:
            raw, _ = llm.generate_json(system=prompts.STRATEGIST_SYSTEM, schema=prompts.BRIEF_SCHEMA, temperature=0.3,
                                       prompt=build_strategist_prompt(p, persona, company, research, signals, decision, kb, colleagues, now),
                                       deadline=deadline)
            brief = normalise_brief(raw, decision, signals, persona)
        except FatalLLMError:
            raise
        except LLMError as exc:
            brief = fallback_brief(decision, signals, persona, str(exc)[:60])
    mark("strategy")

    signal = signals[brief["signal_index"]] if brief["signal_index"] >= 0 else None
    writer_prompt = build_writer_prompt(p, persona, company, research, signal, brief, brief["pillar_name"], kb, colleagues, now)
    if draft is None:
        raw_draft, _ = llm.generate_json(system=prompts.WRITER_SYSTEM, schema=prompts.EMAIL_SCHEMA, temperature=0.6,
                                         prompt=writer_prompt, deadline=deadline)
        draft = {k: raw_draft.get(k, "") for k in prompts.EMAIL_FIELDS}
    draft = tidy_draft(draft)
    mark("write")

    spelling = "british" if (p.country or "").strip().lower() in BRITISH_SPELLING_COUNTRIES else "american"
    allowed_text = " ".join([research.profile, brief["hook_fact"], display_company(p.company), p.job_title,
                             email_domain(p.email)] + [f"{s.title} {s.snippet} {s.date}" for s in signals]
                            + kb.proof + kb.assets)
    check_kwargs = dict(company_name=display_company(p.company), allowed_text=allowed_text, has_proof=kb_has_proof,
                        signal_title=(f"{signal.title} {brief['hook_fact']}" if signal else ""), signal_used=bool(signal),
                        spelling=spelling, colleague_subjects=[c.subject for c in colleagues])
    issues = check_draft(draft, **check_kwargs)

    repairs = 0
    while needs_repair(issues) and repairs < settings.max_repairs and deadline - time.monotonic() > 40:
        repairs += 1
        edit_prompt = (f"{writer_prompt}\n\nDRAFT (JSON):\n{json.dumps(draft, ensure_ascii=False, indent=1)}\n\n"
                       "PROBLEMS TO FIX:\n" + "\n".join(f"- {i}" for i in issues))
        try:
            fixed_raw, _ = llm.generate_json(system=prompts.EDITOR_SYSTEM, schema=prompts.EMAIL_SCHEMA, temperature=0.2,
                                             prompt=edit_prompt, deadline=deadline)
        except FatalLLMError:
            raise
        except LLMError:
            break
        fixed = tidy_draft({k: fixed_raw.get(k, "") for k in prompts.EMAIL_FIELDS})
        fixed_issues = check_draft(fixed, **check_kwargs)
        if issue_score(fixed_issues) <= issue_score(issues):
            draft, issues = fixed, fixed_issues
    mark("quality")

    hard = [i for i in issues if i.severity == "hard"]
    soft = [i for i in issues if i.severity == "soft"]
    if hard:
        status = "Needs review: " + "; ".join(str(i) for i in (hard + soft)[:4])
    elif soft:
        status = "Ready (minor: " + "; ".join(str(i) for i in soft[:2]) + ")"
    else:
        status = "Ready"

    out = assemble(p, draft, options.signoff)
    return {
        "label": label,
        "status": status,
        "signal": signal_cell(signal, research, brief, persona),
        "subject": out["subject"], "e1": out["e1"], "e2": out["e2"], "e3": out["e3"], "e4": out["e4"],
        "dossier": dossier_text(persona, company, decision, brief, status),
        "hooks": hooks_text(signals),
        "brief": brief,
        "persona": persona.to_dict(),
        "company": company.to_dict(),
        "decision": decision.to_dict(),
        "signals": [s.to_dict() for s in signals],
        "issues": [str(i) for i in issues],
        "repairs": repairs,
        "llm_calls": llm.calls,
        "timings": timings,
    }
