"""Text helpers shared across the brain."""

from __future__ import annotations

import re

LEGAL_SUFFIX_RE = re.compile(
    r"[\s,]+(inc|incorporated|llc|l\.l\.c|ltd|limited|corp|corporation|co|company|plc|gmbh|pvt|private|pte|llp|lp|"
    r"s\.?a|ag|b\.?v|n\.?v|oy|ab|a/s|as|s\.?p\.?a|s\.?r\.?l|k\.?k|holdings?|group)\.?$",
    re.I,
)
FREE_EMAIL_DOMAINS = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com", "aol.com",
                      "proton.me", "protonmail.com", "live.com", "msn.com", "yandex.com", "zoho.com"}


def strip_legal_suffix(name: str) -> str:
    n = (name or "").strip().rstrip(",.")
    for _ in range(3):
        nxt = LEGAL_SUFFIX_RE.sub("", n).strip().rstrip(",.")
        if not nxt or nxt == n:
            break
        n = nxt
    return n or (name or "").strip()


def normalise_text(t: str) -> str:
    t = (t or "").lower().replace("&", " and ")
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def company_core(name: str) -> str:
    return normalise_text(strip_legal_suffix(name))


def display_company(name: str) -> str:
    """Natural short name for use in email copy: trims whitespace and legal suffixes, keeps casing."""
    return strip_legal_suffix(re.sub(r"\s+", " ", (name or "").strip()))


def clean_first_name(raw: str) -> str:
    n = (raw or "").strip().split()[0] if (raw or "").strip() else ""
    n = re.sub(r"[^A-Za-zÀ-ÖØ-öø-ÿ'\-]", "", n)
    if len(n) < 2:
        return ""
    if n.isupper() or n.islower():
        n = "-".join(part[:1].upper() + part[1:].lower() for part in n.split("-"))
    return n


def email_domain(email: str) -> str:
    m = re.search(r"@([a-z0-9.-]+\.[a-z]{2,})\s*$", (email or "").lower())
    if not m or m.group(1) in FREE_EMAIL_DOMAINS:
        return ""
    return m.group(1)


def word_count(t: str) -> int:
    return len((t or "").split())


def sentences(t: str) -> list[str]:
    parts = re.split(r"(?<=[.?!])\s+(?=[A-Z0-9\"'])", (t or "").strip())
    return [p.strip() for p in parts if p.strip()]
