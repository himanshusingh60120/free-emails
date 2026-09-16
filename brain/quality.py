"""Deterministic quality gate for drafts: tidy what can be tidied safely, flag everything else."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import LIMITS
from .text import sentences, word_count

EMAIL_KEYS = ("e1_body", "e2_body", "e3_body", "e4_body")


@dataclass
class Issue:
    where: str
    message: str
    severity: str  # hard | soft

    def __str__(self) -> str:
        return f"{self.where}: {self.message}"


# ---------------------------------------------------------------------------
# Safe automatic fixes
# ---------------------------------------------------------------------------

def tidy_body(text: str) -> str:
    t = (text or "").replace("\r", "").strip()
    t = re.sub(r"^(hi|hello|hey|dear)\b[^\n]{0,40},?[ \t]*\n+", "", t, flags=re.I)  # stray greeting
    t = re.sub(r"\n+\s*(best|thanks|thank you|regards|kind regards|cheers|sincerely|warm regards)\b[^\n]*(\n[^\n]{0,40})?\s*$", "", t, flags=re.I)
    t = t.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r" +([,.;:?])", r"\1", t)
    t = re.sub(r"([,;:])(?=[A-Za-z])", r"\1 ", t)
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def tidy_subject(text: str) -> str:
    s = (text or "").strip().strip("\"'").lower()
    s = re.sub(r"\s+", " ", s)
    return re.sub(r"[.!,;:]+$", "", s).strip()


def tidy_e3(text: str) -> str:
    t = tidy_body(text).replace("\n", " ")
    t = re.sub(r"^(hi|hello|hey)\b[^,\n]{0,40}[,\-\u2013]\s*", "", t, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip()
    if t and not t.endswith("?"):
        t = t.rstrip(".") + "?"
    return t


def tidy_draft(d: dict) -> dict:
    out = dict(d)
    out["e1_subject"] = tidy_subject(d.get("e1_subject", ""))
    for k in ("e1_body", "e2_body", "e4_body"):
        out[k] = tidy_body(d.get(k, ""))
    out["e3_body"] = tidy_e3(d.get("e3_body", ""))
    return out


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

BANNED: list[tuple[str, str]] = [
    (r"\b(i )?hope (you|this|all|your)\b", "pleasantry (\"hope...\")"),
    (r"\bjust (following|checking|circling|touching|bumping)\b", "\"just following up\" style bump"),
    (r"\bfollow(ing)?[- ]up\b", "\"following up\" phrasing"),
    (r"\bcircl(e|ing) back\b", "\"circling back\""),
    (r"\btouch(ing)? base\b", "\"touch base\""),
    (r"\bcoffee\b", "coffee ask"),
    (r"\b(\d+|five|ten|fifteen|twenty|thirty)[- ]?min(ute)?s?\b", "asks for minutes of their time"),
    (r"\bcalendar\b", "calendar ask"),
    (r"\b(book|schedule|set up|hop on|jump on|grab|have) (a )?(quick |short |brief )?(call|meeting|chat|demo)\b", "asks for a call or meeting"),
    (r"\b(quick|short|brief|intro|introductory|discovery|exploratory) (call|chat)\b", "asks for a call"),
    (r"\bmeeting\b", "mentions a meeting"),
    (r"\bmy name is\b", "\"my name is\""),
    (r"\breach(ing)? out\b", "\"reach out\""),
    (r"\btried (reaching|to reach|contacting|to contact|getting hold)\b|\bhaven'?t heard back\b", "guilt-trip phrasing"),
    (r"\bleverag", "buzzword \"leverage\""), (r"\bsynerg", "buzzword \"synergy\""), (r"\bunlock", "buzzword \"unlock\""),
    (r"\bcutting[- ]edge\b", "buzzword \"cutting-edge\""), (r"\bgame[- ]?chang", "buzzword \"game-changer\""),
    (r"\brevolutioni[sz]", "buzzword \"revolutionize\""), (r"\bseamless", "buzzword \"seamless\""),
    (r"\brobust\b", "buzzword \"robust\""), (r"\bdelve", "buzzword \"delve\""), (r"\blandscape\b", "buzzword \"landscape\""),
    (r"\bnavigat", "buzzword \"navigate\""), (r"\bempower", "buzzword \"empower\""), (r"\belevat", "buzzword \"elevate\""),
    (r"\bholistic", "buzzword \"holistic\""), (r"\bstreamlin", "buzzword \"streamline\""),
    (r"\bfast[- ]paced\b|\bin today'?s\b", "cliche (\"fast-paced\" / \"in today's\")"),
    (r"\bdear\b", "\"Dear\""),
    (r"https?://|www\.", "contains a link"),
    (r"!", "exclamation mark"),
    (r"\u2014|\u2013| -- ", "em or en dash"),
    (r"\.\.\.|\u2026", "ellipsis"),
    (r"^\s*[-*\u2022]\s", "bullet list"),
    (r"\*\*|__", "markdown formatting"),
]
SUBJECT_BANNED = re.compile(r"(kings research|\breport\b|\bfree\b|\boffer\b|opportunit|^\s*(re|fwd?):|!|\bwebinar\b|\bdiscount\b)", re.I)
FILLER_ADVERBS = re.compile(r"\b(really|very|truly|incredibly|extremely|super)\b", re.I)
CLIENT_CLAIM = re.compile(r"\b(we (recently |just )?helped|a (recent )?client|our clients?|case stud(y|ies)|clients like|companies like yours (have|saw))\b", re.I)
EVENT_CLAIM = re.compile(r"\b(saw|noticed|congrat\w*|read that|(recent(ly)?|just) (announced|launched|opened|acquired|raised|hired|expanded|named|appointed))\b", re.I)

VOWEL_SOUND_EXCEPTIONS_A = re.compile(r"^(uni|use|usu|uti|ure|eu|one|once|ubiq|u[bcdfghjklmnpqrstvwxz][aeiou])", re.I)  # "a unit", "a one-page"
CONSONANT_SOUND_EXCEPTIONS_AN = re.compile(r"^(hour|honest|honou?r|heir|herb)", re.I)  # "an hour"
VOWEL_SOUND_LETTERS = set("AEFHILMNORSX")  # initialisms that start with a vowel sound: an FDA, an M&A, an SKU

AMERICAN_TO_BRITISH = {
    r"\b(organi|priori|optimi|recogni|speciali|customi|utili|minimi|maximi|finali|standardi|categori|summari|capitali|characteri|emphasi|monetis|digiti)z(e|es|ed|ing|ation|ations)\b": "z/s",
    r"\banalyz(e|es|ed|ing)\b": "analyze/analyse",
    r"\b(colo|behavio|favo|labo|neighbo|endeavo|honou?)r(s|ed|ing|able)?\b": "or/our",
    r"\bcent(er|ers)\b": "center/centre",
    r"\bmodeling\b": "modeling/modelling",
    r"\bfulfill(ment|s)?\b": "fulfill/fulfil",
    r"\bdefense\b": "defense/defence",
    r"\bcatalog\b": "catalog/catalogue",
}
BRITISH_TO_AMERICAN = {
    r"\b(organi|priori|optimi|recogni|speciali|customi|utili|minimi|maximi|finali|standardi|categori|summari|capitali|characteri|emphasi|digiti)s(e|es|ed|ing|ation|ations)\b": "s/z",
    r"\banalys(e|ed|ing)\b": "analyse/analyze",
    r"\b(colou|behaviou|favou|labou|neighbou|endeavou)r(s|ed|ing|able)?\b": "our/or",
    r"\bcent(re|res)\b": "centre/center",
    r"\bmodelling\b": "modelling/modeling",
    r"\bdefence\b": "defence/defense",
    r"\bcatalogue\b": "catalogue/catalog",
}

STOPWORDS = set("""a an the and or but for with from into onto over under about after before this that these those its their
their they them you your our we us of to in on at by as is are was were be been being has have had will would can could
should may might new more most less very also than then there here what which who whom whose how when where why not no
plans plan says said announces announced company companies inc ltd llc group""".split())


def _numbers(text: str) -> list[str]:
    return [m.group(2).rstrip(".,").replace(",", "") for m in re.finditer(r"(^|[^A-Za-z0-9])(\d[\d,.]*)", text or "")]


def grammar_issues(text: str, where: str, lower_start_ok: bool = False) -> list[Issue]:
    issues: list[Issue] = []
    if not text:
        return issues
    for m in re.finditer(r"\b(a|an)\s+([A-Za-z][\w&'-]*)", text):
        art, word = m.group(1).lower(), m.group(2)
        if word.isupper() and len(word) >= 2:
            needs_an = word[0] in VOWEL_SOUND_LETTERS
        elif re.match(r"\d", word):
            continue
        else:
            starts_vowel = word[0].lower() in "aeiou"
            needs_an = (starts_vowel and not VOWEL_SOUND_EXCEPTIONS_A.match(word)) or bool(CONSONANT_SOUND_EXCEPTIONS_AN.match(word))
        if art == "a" and needs_an:
            issues.append(Issue(where, f'"a {word}" should be "an {word}"', "hard"))
        elif art == "an" and not needs_an:
            issues.append(Issue(where, f'"an {word}" should be "a {word}"', "hard"))
    for m in re.finditer(r"\b(\w+)\s+\1\b", text, re.I):
        if m.group(1).lower() not in ("that", "had"):
            issues.append(Issue(where, f'repeated word "{m.group(1)}"', "hard"))
    if re.search(r"(^|\s)i(\s|'m\b|'ve\b|'d\b|'ll\b)", text):
        issues.append(Issue(where, 'lowercase "i" used as a pronoun', "hard"))
    for para in [p.strip() for p in text.split("\n\n") if p.strip()]:
        if not re.search(r"[.?]\"?$", para):
            issues.append(Issue(where, f'paragraph does not end with punctuation: "{para[-40:]}"', "hard"))
    for m in re.finditer(r"(?<!\be\.g)(?<!\bi\.e)(?<!\bvs)(?<!\bSt)(?<!\bMr)(?<!\bDr)[.?]\s+([a-z])", text):
        issues.append(Issue(where, f'sentence starts with lowercase "{m.group(1)}"', "hard"))
    if not lower_start_ok and text[:1].islower():
        issues.append(Issue(where, "first sentence starts with a lowercase letter", "hard"))
    if text.count("(") != text.count(")") or text.count('"') % 2:
        issues.append(Issue(where, "unbalanced brackets or quotes", "hard"))
    if re.search(r"\bits'|\bit's (own|products?|customers?|team|plans?|plant|facility|market)\b", text, re.I):
        issues.append(Issue(where, 'check "its" vs "it\'s"', "hard"))
    if re.search(r"\b(could|should|would|must) of\b", text, re.I):
        issues.append(Issue(where, '"could of" style error', "hard"))
    if re.search(r",\s*(however|therefore|so)\s*,?\s+[a-z]+ (is|are|was|were|will)\b", text):
        issues.append(Issue(where, "possible comma splice", "soft"))
    for s in sentences(text):
        if word_count(s) > LIMITS["sentence_max_words"]:
            issues.append(Issue(where, f"sentence over {LIMITS['sentence_max_words']} words", "soft"))
    starts = [s.split()[0].lower() for s in sentences(text) if s.split()]
    if any(a == b for a, b in zip(starts, starts[1:])):
        issues.append(Issue(where, "two sentences in a row start with the same word", "soft"))
    if len(FILLER_ADVERBS.findall(text)) >= 2:
        issues.append(Issue(where, "too many filler adverbs", "soft"))
    return issues


def spelling_issues(text: str, where: str, spelling: str) -> list[Issue]:
    table = AMERICAN_TO_BRITISH if spelling == "british" else BRITISH_TO_AMERICAN
    issues = []
    for pattern, label in table.items():
        m = re.search(pattern, text or "", re.I)
        if m:
            issues.append(Issue(where, f'use {spelling} spelling ("{m.group(0)}", {label})', "soft"))
    return issues


def content_words(text: str, exclude: set[str]) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z0-9-]{3,}", (text or "").lower()) if w not in STOPWORDS and w not in exclude}


def check_draft(draft: dict, *, company_name: str, allowed_text: str, has_proof: bool, signal_title: str,
                signal_used: bool, spelling: str, colleague_subjects: list[str]) -> list[Issue]:
    issues: list[Issue] = []
    subj = draft.get("e1_subject", "")
    if not subj:
        issues.append(Issue("subject", "is empty", "hard"))
    if word_count(subj) > LIMITS["subject_max_words"]:
        issues.append(Issue("subject", f"is over {LIMITS['subject_max_words']} words", "hard"))
    if SUBJECT_BANNED.search(subj):
        issues.append(Issue("subject", "uses a spammy or banned word", "hard"))
    if subj and subj in [c.lower() for c in colleague_subjects]:
        issues.append(Issue("subject", "is identical to one already sent to a colleague", "hard"))

    limits = {"e1_body": (LIMITS["e1_min"], LIMITS["e1_max"]), "e2_body": (LIMITS["e2_min"], LIMITS["e2_max"]),
              "e3_body": (4, LIMITS["e3_max"]), "e4_body": (LIMITS["e4_min"], LIMITS["e4_max"])}
    for key in EMAIL_KEYS:
        body = draft.get(key, "")
        label = key.replace("_body", "")
        if not body:
            issues.append(Issue(label, "is empty", "hard"))
            continue
        lo, hi = limits[key]
        n = word_count(body)
        if n > hi:
            issues.append(Issue(label, f"is {n} words (max {hi})", "hard"))
        elif n < lo:
            issues.append(Issue(label, f"is {n} words (min {lo})", "soft"))
        for pattern, message in BANNED:
            if re.search(pattern, body, re.I | re.M):
                issues.append(Issue(label, message, "hard"))
        issues.extend(grammar_issues(body, label, lower_start_ok=(key == "e3_body")))
        issues.extend(spelling_issues(body, label, spelling))
        if not has_proof and CLIENT_CLAIM.search(body):
            issues.append(Issue(label, "claims past clients or results, but no approved proof exists", "hard"))
        if body.lower().count("kings research") > 1:
            issues.append(Issue(label, 'uses "Kings Research" more than once', "soft"))
        if len(re.findall(r"\bI\b", body)) > 2:
            issues.append(Issue(label, 'too many sentences about "I"', "soft"))
        # company name casing
        core = company_name.strip()
        if core and len(core) > 2:
            for m in re.finditer(r"\b" + re.escape(core) + r"\b", body, re.I):
                if " " not in core and m.group(0).islower():
                    continue  # single-word names that are also ordinary words ("insight", "apex")
                if m.group(0) != core:
                    issues.append(Issue(label, f'write the company name as "{core}"', "hard"))
                    break
        allowed = set(_numbers(allowed_text))
        for n_tok in _numbers(body):
            if re.fullmatch(r"20\d\d", n_tok):
                continue
            if n_tok not in allowed:
                issues.append(Issue(label, f'number "{n_tok}" is not in the sources', "hard"))

    e1 = draft.get("e1_body", "")
    if e1 and "?" not in e1:
        issues.append(Issue("e1", "must end with a soft question", "hard"))
    if draft.get("e2_body") and "?" not in draft["e2_body"]:
        issues.append(Issue("e2", "must ask permission to send the asset", "hard"))
    e3 = draft.get("e3_body", "")
    if len(sentences(e3)) > 1:
        issues.append(Issue("e3", "must be a single sentence", "hard"))
    if e1 and "kings research" not in e1.lower():
        issues.append(Issue("e1", 'should name "Kings Research" once in the offer paragraph', "soft"))

    if signal_used and signal_title:
        company_words = content_words(company_name, set())
        if not (content_words(signal_title, company_words) & content_words(e1, set())):
            issues.append(Issue("e1", "does not clearly reference the chosen news signal", "hard"))
    if not signal_used:
        first_para = e1.split("\n\n")[0] if e1 else ""
        if EVENT_CLAIM.search(first_para):
            issues.append(Issue("e1", "implies a specific recent event, but no news signal was used", "hard"))

    seen: set[str] = set()
    unique: list[Issue] = []
    for i in issues:
        key = str(i)
        if key not in seen:
            seen.add(key)
            unique.append(i)
    return unique


def issue_score(issues: list[Issue]) -> float:
    return sum(3.0 if i.severity == "hard" else 1.0 for i in issues)


def needs_repair(issues: list[Issue]) -> bool:
    return any(i.severity == "hard" for i in issues) or sum(1 for i in issues if i.severity == "soft") >= 2
