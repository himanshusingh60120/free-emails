"""Deterministic reasoning layer.

Scores every pillar against the evidence (signals, persona, industry, company scale, colleagues already
contacted), then picks the best services inside the top pillars. The result is an explained shortlist
the LLM strategist chooses from, so the model never picks an offering the evidence does not support.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict

from .industry import CompanyRead
from .offerings import (INDUSTRY_FIT, PERSONA_FIT, PILLAR_BY_ID, PILLARS, SCALE_ORDER, SCALE_PENALTY,
                        SMALL_COMPANY_ADJUST, TRIGGER_FIT, Pillar)
from .persona import Persona
from .signals import Signal


@dataclass
class ServicePick:
    name: str
    score: float
    matched: list[str] = field(default_factory=list)


@dataclass
class Candidate:
    pillar_id: str
    pillar_name: str
    score: float
    services: list[ServicePick]
    signal_index: int  # index into the classified signal list, -1 for role-based
    why: list[str]
    pitch: str
    assets: list[str]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["services"] = [asdict(s) for s in self.services]
        return d


@dataclass
class Decision:
    candidates: list[Candidate]
    all_scores: dict[str, float]
    notes: list[str]

    def to_dict(self) -> dict:
        return {"candidates": [c.to_dict() for c in self.candidates], "all_scores": self.all_scores, "notes": self.notes}


def _pillar_signal_score(pillar: Pillar, signals: list[Signal]) -> tuple[float, int, str]:
    """Best single signal contribution plus a small bonus when several signals point the same way."""
    best, best_idx, best_trigger, total = 0.0, -1, "", 0.0
    for i, sig in enumerate(signals):
        contribution, trig = 0.0, ""
        for t in sig.triggers:
            weight = TRIGGER_FIT.get(t, {}).get(pillar.id, 0.0)
            # secondary triggers count, but less than the headline's main meaning
            value = sig.strength * weight * (1.0 if t == sig.primary else 0.7)
            if value > contribution:
                contribution, trig = value, t
        total += contribution
        if contribution > best:
            best, best_idx, best_trigger = contribution, i, trig
    corroboration = min(0.15, max(0.0, total - best) * 0.25)
    return best + corroboration, best_idx, best_trigger


def _service_scores(pillar: Pillar, persona: Persona, signal: Signal | None, all_text: str) -> list[ServicePick]:
    picks: list[ServicePick] = []
    sig_text = f"{signal.title} {signal.snippet}".lower() if signal else ""
    for order, svc in enumerate(pillar.services):
        score = 0.0
        matched: list[str] = []
        for kw in svc.keywords:
            if kw and kw in sig_text:
                score += 1.0
                matched.append(kw)
            elif kw and kw in all_text:
                score += 0.35
        if signal and any(t in svc.triggers for t in signal.triggers):
            score += 1.2
            matched.append("trigger:" + next(t for t in signal.triggers if t in svc.triggers))
        if persona.function in svc.functions:
            score += 0.9
            matched.append("role:" + persona.function)
        score += max(0.0, 0.3 - order * 0.02)  # portfolio order as a gentle tie-breaker
        picks.append(ServicePick(name=svc.name, score=round(score, 3), matched=matched))
    picks.sort(key=lambda p: p.score, reverse=True)
    return picks


def decide(persona: Persona, company: CompanyRead, signals: list[Signal], colleague_pillars: list[str],
           top_n: int = 3) -> Decision:
    notes: list[str] = []
    persona_fit = PERSONA_FIT.get(persona.function, PERSONA_FIT["other"])
    industry_fit = INDUSTRY_FIT.get(company.industry, {})
    usable = [s for s in signals if not s.sensitive] or []
    if len(usable) < len(signals):
        notes.append(f"ignored {len(signals) - len(usable)} sensitive news item(s) as hooks")
    all_text = " ".join(f"{s.title} {s.snippet}" for s in usable).lower()

    scored: list[tuple[float, Pillar, int, list[str]]] = []
    all_scores: dict[str, float] = {}
    for pillar in PILLARS:
        why: list[str] = []
        if pillar.b2c_only and not company.b2c:
            all_scores[pillar.id] = -1.0
            continue

        sig_score, sig_idx, trig = _pillar_signal_score(pillar, usable)
        role_score = persona_fit.get(pillar.id, 0.2)
        ind_adj = industry_fit.get(pillar.id, 0.0)
        if ind_adj <= -1.0:
            all_scores[pillar.id] = -1.0
            continue

        # Evidence from news dominates when it exists; otherwise the role and industry carry the decision.
        if usable:
            score = 0.55 * sig_score + 0.30 * role_score + 0.15 * (0.5 + ind_adj) + pillar.prior
        else:
            score = 0.65 * role_score + 0.35 * (0.5 + ind_adj) + pillar.prior

        if sig_idx >= 0 and sig_score > 0:
            why.append(f"signal '{usable[sig_idx].title[:80]}' reads as {trig} (strength {usable[sig_idx].strength})")
        why.append(f"{persona.function} role fit {role_score:.2f}")
        if ind_adj:
            why.append(f"{company.industry} industry adjustment {ind_adj:+.2f}")

        if company.scale == "small" and SMALL_COMPANY_ADJUST.get(pillar.id):
            score += SMALL_COMPANY_ADJUST[pillar.id]
            why.append(f"small-company practicality {SMALL_COMPANY_ADJUST[pillar.id]:+.2f}")

        need, have = SCALE_ORDER[pillar.min_scale], SCALE_ORDER[company.scale]
        if have < need:
            penalty = SCALE_PENALTY.get((company.scale, pillar.min_scale), 0.2)
            score -= penalty
            why.append(f"stretch for a {company.scale} company (-{penalty})")

        if pillar.id in colleague_pillars:
            score -= 0.35
            why.append("already pitched to a colleague (-0.35)")

        all_scores[pillar.id] = round(score, 3)
        # Map the local signal index back to the full classified list.
        full_idx = signals.index(usable[sig_idx]) if sig_idx >= 0 and sig_score >= 0.12 else -1
        scored.append((score, pillar, full_idx, why))

    scored.sort(key=lambda x: x[0], reverse=True)
    candidates: list[Candidate] = []
    for score, pillar, sig_idx, why in scored[:top_n]:
        sig = signals[sig_idx] if sig_idx >= 0 else None
        services = _service_scores(pillar, persona, sig, all_text)[:3]
        candidates.append(Candidate(
            pillar_id=pillar.id, pillar_name=pillar.name, score=round(score, 3), services=services,
            signal_index=sig_idx, why=why, pitch=pillar.pitch, assets=list(pillar.assets),
        ))
    if not usable:
        notes.append("no usable recent news: decision based on role, industry and company scale")
    return Decision(candidates=candidates, all_scores=dict(sorted(all_scores.items(), key=lambda kv: -kv[1])), notes=notes)


def pillar_from_signal_cell(cell: str) -> str:
    """Recovers the pillar used for a colleague from the Signal column ('offer: Pillar > Service')."""
    m = re.search(r"offer: ([^>|]+)", cell or "")
    if not m:
        return ""
    name = m.group(1).strip().lower()
    for p in PILLARS:
        if p.name.lower() == name:
            return p.id
    return ""


def get_pillar(pillar_id: str) -> Pillar:
    return PILLAR_BY_ID[pillar_id]
