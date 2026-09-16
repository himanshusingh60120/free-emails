"""Offline tests. No API keys or network needed.

Run:  pip install -r requirements.txt pytest httpx  &&  python -m pytest -q
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain import research as research_mod  # noqa: E402
from brain.config import Settings  # noqa: E402
from brain.decision import decide  # noqa: E402
from brain.industry import infer_company  # noqa: E402
from brain.persona import analyze_persona  # noqa: E402
from brain.pipeline import Colleague, Knowledge, Prospect, RowOptions, assemble, next_quarter_label, run_prospect  # noqa: E402
from brain.quality import check_draft, tidy_draft  # noqa: E402
from brain.research import filter_news, parse_news_rss  # noqa: E402
from brain.signals import Signal, classify_all  # noqa: E402

NOW = datetime.now(timezone.utc)


def days_ago(n: int) -> str:
    return (NOW - timedelta(days=n)).strftime("%a, %d %b %Y %H:%M:%S GMT")


def iso_days_ago(n: int) -> str:
    return (NOW - timedelta(days=n)).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeResp:
    def __init__(self, text: str, status: int = 200):
        self.text, self.status_code = text, status


class FakeHTTP:
    def __init__(self, feeds: dict[str, str]):
        self.feeds = feeds

    def get(self, url, timeout=None, headers=None):
        for needle, xml in self.feeds.items():
            if needle.replace(" ", "%20") in url:
                return FakeResp(xml)
        return FakeResp("<rss><channel></channel></rss>")


class FakeLLMResponse:
    def __init__(self, chunks):
        self.grounding_chunks = chunks


class FakeGemini:
    """Returns scripted JSON per stage, recording prompts."""

    def __init__(self, strategist=None, writer=None, editor=None, web=None):
        self.strategist, self.writer, self.editor, self.web = strategist, writer, editor or [], web
        self.calls, self.prompts = 0, []

    def generate_json(self, *, prompt, system=None, schema=None, search=False, **_):
        self.calls += 1
        self.prompts.append((system or "")[:40] + " :: " + prompt)
        if search:
            return self.web or {"profile": "", "events": []}, FakeLLMResponse([{"uri": "https://x", "title": "example.com"}])
        if system and system.startswith("You are a senior engagement partner"):
            return self.strategist, None
        if system and system.startswith("You are a meticulous copy editor"):
            return (self.editor.pop(0) if self.editor else self.writer), None
        return self.writer, None


def settings(**over) -> Settings:
    os.environ.setdefault("GEMINI_API_KEY", "test")
    s = Settings()
    for k, v in over.items():
        object.__setattr__(s, k, v)
    return s


def rss(items):
    body = "".join(
        f"<item><title>{t} - Trade Weekly</title><link>https://news.google.com/rss/articles/{i}</link>"
        f"<pubDate>{days_ago(age)}</pubDate><description>{t}</description><source url=\"https://tw.com\">Trade Weekly</source></item>"
        for i, (t, age) in enumerate(items)
    )
    return f"<rss><channel>{body}</channel></rss>"


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

def test_persona_reads_real_titles():
    assert analyze_persona("President").function == "executive"
    assert analyze_persona("VP Strategic Marketing").function == "marketing"
    assert analyze_persona("Strategic Account Manager").function == "sales"
    assert analyze_persona("Production Manager").function == "operations"
    assert analyze_persona("Procurement Manager").function == "procurement"
    assert analyze_persona("Marketing Intern").buyer_role == "non_buyer"
    assert analyze_persona("Assistant Director of Student Conduct and Outreach").fit < 0.3
    assert analyze_persona("Chief Sustainability Officer").buyer_role == "decider"


def test_news_filter_enforces_90_days_and_relevance():
    xml = rss([("Acme Widgets opens new plant in Monterrey", 12),
               ("Acme Widgets wins award", 140),
               ("Some Fund Buys 12,000 Shares of Acme Widgets", 3),
               ("Acme Widgets opens new plant in Monterrey", 12),
               ("Widgets market outlook", 5)])
    items = parse_news_rss(xml)
    assert len(items) == 5 and items[0].source == "Trade Weekly"
    kept = filter_news(items, "Acme Widgets Inc.", NOW - timedelta(days=90), NOW)
    assert [k.title for k in kept] == ["Acme Widgets opens new plant in Monterrey"]


def test_signal_classification_and_sensitivity():
    sigs = classify_all([
        Signal(date=iso_days_ago(10), title="Acme Widgets acquires Beta Components"),
        Signal(date=iso_days_ago(5), title="Acme Widgets announces layoffs at Ohio site"),
        Signal(date=iso_days_ago(20), title="Acme Widgets named Jane Doe as Chief Strategy Officer"),
    ], "acme widgets", "Jane Doe")
    by_title = {s.title: s for s in sigs}
    assert by_title["Acme Widgets acquires Beta Components"].primary == "m_and_a"
    assert by_title["Acme Widgets announces layoffs at Ohio site"].sensitive
    assert by_title["Acme Widgets named Jane Doe as Chief Strategy Officer"].about_prospect
    assert sigs[0].strength >= sigs[-1].strength


def test_decision_tariff_signal_to_procurement_picks_supply_chain():
    persona = analyze_persona("Head of Procurement")
    sigs = classify_all([Signal(date=iso_days_ago(15), title="Acme Widgets shifts sourcing to Vietnam amid new tariffs")], "acme widgets")
    company = infer_company("Acme Widgets", "acmewidgets.com", "Acme Widgets is an industrial components manufacturer.", [s.title for s in sigs], 1, 0)
    d = decide(persona, company, sigs, [])
    assert d.candidates[0].pillar_id == "supply_chain"
    assert d.candidates[0].signal_index == 0
    names = [s.name for s in d.candidates[0].services]
    assert any(n in names for n in ("Localization / Nearshoring Assessment", "Trade Flow Analysis", "Supplier Identification"))


def test_decision_product_launch_to_marketing():
    persona = analyze_persona("VP Product Marketing")
    sigs = classify_all([Signal(date=iso_days_ago(8), title="Nova Analytics launches AI forecasting platform for retailers")], "nova analytics")
    company = infer_company("Nova Analytics", "novaanalytics.io", "Nova Analytics sells SaaS analytics software.", [s.title for s in sigs], 1, 0)
    top = [c.pillar_id for c in decide(persona, company, sigs, []).candidates]
    assert top[0] in ("product_portfolio", "pricing", "competitive_intel", "customer_voc")
    assert "consumer_brand" not in top  # B2B software is not a consumer brand


def test_decision_small_company_no_news_avoids_big_ticket_pillars():
    persona = analyze_persona("President")
    company = infer_company("Elite Printing and Packaging", "eliteprintingandpackaging.com",
                            "A family-owned commercial printer making folding cartons and labels.", [], 0, 0)
    assert company.industry == "packaging_printing" and company.scale == "small"
    d = decide(persona, company, [], [])
    ids = [c.pillar_id for c in d.candidates]
    assert "m_and_a" not in ids and "investment_strategic" not in ids and "monitoring" not in ids
    assert all(c.signal_index == -1 for c in d.candidates)


def test_colleague_penalty_changes_the_pick():
    persona = analyze_persona("Head of Procurement")
    sigs = classify_all([Signal(date=iso_days_ago(15), title="Acme Widgets shifts sourcing to Vietnam amid new tariffs")], "acme widgets")
    company = infer_company("Acme Widgets", "", "industrial components manufacturer", [s.title for s in sigs], 1, 0)
    d = decide(persona, company, sigs, ["supply_chain"])
    assert d.candidates[0].pillar_id != "supply_chain"


def test_quality_gate_catches_framework_and_grammar_errors():
    bad = tidy_draft({
        "e1_subject": "Kings Research offer for Acme!",
        "e1_body": "I hope you're well! I wanted to reach out because we leverage data. We helped a client cut costs 22%. Can we book a quick 15-minute call? https://kingsresearch.com",
        "e2_body": "Just following up on my email",
        "e3_body": "Is this a priority? Or should I check back later.",
        "e4_body": "Dear Michael, i've tried reaching you. It's an unique chance.",
    })
    issues = [str(i) for i in check_draft(bad, company_name="Acme Widgets", allowed_text="", has_proof=False,
                                          signal_title="", signal_used=False, spelling="american", colleague_subjects=[])]
    text = " | ".join(issues)
    for needle in ("spammy", "pleasantry", "minutes", "link", "exclamation", "not in the sources", "past clients",
                   "following up", "single sentence", "an unique", "lowercase \"i\"", "guilt"):
        assert needle in text, f"missing check: {needle}\n{text}"


def good_draft():
    return {
        "e1_subject": "vietnam sourcing move",
        "e1_body": ("Saw Acme Widgets is shifting sourcing to Vietnam as the new tariffs take effect.\n\n"
                    "The hard part for procurement is usually visibility. Knowing which Vietnamese suppliers already ship your components, "
                    "and at what landed cost, decides whether the move protects margin or just moves the risk.\n\n"
                    "Kings Research maps trade flows and supplier options by origin, so your team can shortlist suppliers on evidence before contracts are signed.\n\n"
                    "Open to seeing how we would approach it?"),
        "e2_body": ("Another angle on the same move: tariff exposure often follows the component, not the country, so a new origin can still carry the old duties.\n\n"
                    "I can put together a sample view of the trade flows we would map for your main component categories.\n\n"
                    "Want me to send it over?"),
        "e3_body": "is getting a clear read on Vietnam supplier options a priority for your team right now, or should I check back in Q1?",
        "e4_body": ("I'll assume the Vietnam move is already covered, so this is my last note.\n\n"
                    "If landed costs start drifting from plan once volumes ramp, reply here and we can pick it up."),
    }


def test_quality_gate_passes_a_clean_draft():
    d = tidy_draft(good_draft())
    issues = check_draft(d, company_name="Acme Widgets", allowed_text="Acme Widgets shifts sourcing to Vietnam amid new tariffs",
                         has_proof=False, signal_title="Acme Widgets shifts sourcing to Vietnam amid new tariffs",
                         signal_used=True, spelling="american", colleague_subjects=[])
    assert [str(i) for i in issues if i.severity == "hard"] == []


def test_assemble_greetings_and_e3_shape():
    out = assemble(Prospect(fname="MICHAEL", company="Acme"), tidy_draft(good_draft()), "")
    assert out["e1"].startswith("Hi Michael,\n\n")
    assert out["e3"].startswith("Hi Michael, is getting")
    assert out["subject"] == "vietnam sourcing move"


def test_next_quarter_label():
    assert next_quarter_label(datetime(2026, 9, 16, tzinfo=timezone.utc)) == "Q1"
    assert next_quarter_label(datetime(2026, 5, 1, tzinfo=timezone.utc)) == "Q3"
    assert next_quarter_label(datetime(2026, 6, 1, tzinfo=timezone.utc)) == "Q4"


# ---------------------------------------------------------------------------
# End-to-end pipeline with fakes
# ---------------------------------------------------------------------------

STRATEGIST = {
    "reasoning": "Tariff-driven sourcing shift lands directly on procurement; trade flow data answers the supplier question fastest.",
    "candidate_index": 0, "service": "Trade Flow Analysis", "signal_index": 0,
    "hook_fact": "Acme Widgets is shifting sourcing to Vietnam amid new tariffs.",
    "role_bottleneck": "Procurement has to pick Vietnamese suppliers without visibility of who already ships these components.",
    "business_stake": "landed cost and supply continuity", "offer_plain": "trade flow and supplier mapping by origin for their components",
    "outcome": "a supplier shortlist based on evidence", "e2_angle": "Tariff exposure follows the component, not just the country.",
    "e2_asset": "a sample view of the trade flows for their main component categories", "e3_topic": "Vietnam supplier options",
    "e4_reason": "landed costs drift from plan once volumes ramp", "subject_idea": "vietnam sourcing move", "confidence": 0.84,
}


def test_pipeline_end_to_end_with_repair():
    research_mod._CACHE.clear()
    http = FakeHTTP({"Acme": rss([("Acme Widgets shifts sourcing to Vietnam amid new tariffs", 15), ("Acme Widgets wins award", 200)])})
    broken = dict(good_draft(), e2_body="Just following up. Want the sample?")
    llm = FakeGemini(strategist=STRATEGIST, writer=broken, editor=[good_draft()])
    result = run_prospect(Prospect(fname="jane", lname="Doe", email="jane@acmewidgets.com", job_title="Head of Procurement",
                                   company="Acme Widgets", country="United States"),
                          settings=settings(gemini_min_interval=0), llm=llm, http=http, knowledge=Knowledge(),
                          options=RowOptions(mode="deep", web_research="fallback"))
    assert result["status"].startswith("Ready"), result["status"] + " " + json.dumps(result["issues"])
    assert result["repairs"] == 1
    assert result["brief"]["pillar_id"] == "supply_chain" and result["brief"]["service"] == "Trade Flow Analysis"
    assert "Vietnam" in result["signal"] and "offer: Supply Chain & Procurement Intelligence > Trade Flow Analysis" in result["signal"]
    assert all("wins award" not in p for p in llm.prompts), "news older than 90 days reached the LLM"
    assert result["e1"].startswith("Hi Jane,")
    assert llm.calls == 3  # strategist, writer, editor (news was found, so no web research)


def test_pipeline_rejects_invented_signal_and_numbers_in_brief():
    research_mod._CACHE.clear()
    http = FakeHTTP({})
    web = {"profile": "Elite Printing and Packaging is a family-owned US printer of folding cartons and labels.",
           "events": [{"date": iso_days_ago(200), "headline": "Old expansion", "detail": "too old", "source": "example.com"}]}
    strategist = dict(STRATEGIST, candidate_index=0, service="Raw Material Analysis", signal_index=3,
                      hook_fact="Elite grew revenue 40% last year.")
    writer = {
        "e1_subject": "board and ink costs?",
        "e1_body": ("For a folding carton and label printer, input costs are usually the hardest number to plan around.\n\n"
                    "When board, ink or freight prices move mid-year, quotes signed months earlier start eating margin before anyone flags it.\n\n"
                    "Kings Research compares suppliers, raw material prices and freight costs, so you know where the savings are before the next contract round.\n\n"
                    "Worth a look?"),
        "e2_body": ("One more angle: printers often benchmark suppliers against each other, but rarely against what freight and lead times cost by region.\n\n"
                    "I can put together a short outline of the supplier and cost comparison we would build for Elite Printing and Packaging.\n\n"
                    "Want me to send it?"),
        "e3_body": "is getting ahead of board and ink costs a priority for you right now, or should I check back in Q1?",
        "e4_body": ("I'll assume input costs aren't the priority right now, so this is my last note.\n\n"
                    "If a supplier price change starts squeezing quotes, reply here and we can pick it up."),
    }
    llm = FakeGemini(strategist=strategist, writer=writer, web=web)
    result = run_prospect(Prospect(fname="Michael", email="msloan@eliteprintingandpackaging.com", job_title="President",
                                   company=" Elite Printing and Packaging", country="United States"),
                          settings=settings(gemini_min_interval=0), llm=llm, http=http, options=RowOptions())
    assert result["brief"]["signal_index"] == -1 and result["brief"]["hook_fact"] == ""
    assert "No company news since" in result["signal"]
    assert result["company"]["scale"] == "small"
    assert result["brief"]["pillar_id"] not in ("m_and_a", "investment_strategic", "monitoring")
    assert result["status"].startswith("Ready"), result["issues"]


def test_low_fit_is_skipped_without_llm_calls():
    llm = FakeGemini()
    result = run_prospect(Prospect(fname="Sam", job_title="Marketing Intern", company="Acme"), settings=settings(), llm=llm,
                          http=FakeHTTP({}), options=RowOptions(skip_low_fit=True))
    assert result["skipped"] and llm.calls == 0


def test_api_generate_endpoint(monkeypatch):
    os.environ["APP_PASSWORD"] = "pw"
    os.environ["GEMINI_API_KEY"] = "test"
    from fastapi.testclient import TestClient
    import main as index

    research_mod._CACHE.clear()
    fake = FakeGemini(strategist=STRATEGIST, writer=good_draft())
    monkeypatch.setattr(index, "Gemini", lambda s, session=None: fake)
    monkeypatch.setattr(research_mod, "fetch_google_news", lambda *a, **k: [
        Signal(date=iso_days_ago(15), title="Acme Widgets shifts sourcing to Vietnam amid new tariffs", source="Trade Weekly")])
    client = TestClient(index.app)
    assert client.get("/api/health").status_code == 401
    r = client.post("/api/generate", headers={"x-app-password": "pw"},
                    json={"prospect": {"fname": "Jane", "job_title": "Head of Procurement", "company": "Acme Widgets", "country": "United States"}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["subject"] == "vietnam sourcing move" and body["status"].startswith("Ready"), body.get("issues")
    assert client.get("/").status_code == 200


def test_sheet_helpers():
    from brain.sheets import RowFilter, collect_colleagues, eligible_rows, map_headers, spreadsheet_id
    header = ["fname", "lname", "email", "job_title", "company", "country", "time_zone", "Signal", " E1 Subject", "E1 Body",
              "E2 Body", "E3 Body", "E4 Body", "Status", "Dossier", "Hooks"]
    cols = map_headers(header)
    assert cols["timezone"] == 6 and cols["e1_subject"] == 8 and cols["dossier"] == 14
    values = [header,
              ["Michael", "Sloan", "m@e.com", "President", " Elite Printing and Packaging", "United States", "", "", "", "", "", "", "", "", "", ""],
              ["Steve", "K", "s@u.com", "President", "Utah PaperBox", "United States", "", "", "", "", "", "", "", "Sent", "", ""],
              ["Kyle", "D", "k@6.com", "Director of Digital Marketing", "6sense", "United States", "",
               "x | offer: Competitive Intelligence > Competitor Profiling", "6sense + ai agents", "Hi", "", "", "", "Ready", "", ""],
              ["Jason", "Z", "j@6.com", "CEO", "6sense", "United States", "", "", "", "", "", "", "", "", "", ""]]
    assert eligible_rows(values, cols, RowFilter()) == [2, 5]
    col = collect_colleagues(values, cols, 1, 5, "6sense")
    assert len(col) == 1 and col[0].pillar_id == "competitive_intel"
    assert spreadsheet_id("https://docs.google.com/spreadsheets/d/1TgV7UeioAQNzVtLT0A5eOJywU3ZdkRaS5XpobieLLxw/edit#gid=1") == "1TgV7UeioAQNzVtLT0A5eOJywU3ZdkRaS5XpobieLLxw"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
