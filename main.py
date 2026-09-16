"""Vercel entrypoint.

Vercel detects FastAPI and loads the instance named `app` from a root-level main.py.
Run locally with:  uvicorn main:app --reload
"""

from __future__ import annotations

import hmac
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests  # noqa: E402
from fastapi import Depends, FastAPI, Header, HTTPException  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from brain import __version__  # noqa: E402
from brain.config import Settings, get_settings  # noqa: E402
from brain.llm import FatalLLMError, Gemini  # noqa: E402
from brain.pipeline import Knowledge, Prospect, RowOptions, run_prospect  # noqa: E402
from brain.sheets import RowFilter, SheetClient, SheetError  # noqa: E402

app = FastAPI(title="Kings Research Outreach Brain", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def authed(x_app_password: str = Header(default="")) -> Settings:
    settings = get_settings()
    if not settings.app_password:
        raise HTTPException(status_code=500, detail="APP_PASSWORD is not set in the Vercel environment variables.")
    if not hmac.compare_digest(x_app_password.encode(), settings.app_password.encode()):
        raise HTTPException(status_code=401, detail="Wrong password.")
    return settings


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class SheetRef(BaseModel):
    sheet: str = Field(..., description="Google Sheets URL or ID")


class InspectRequest(SheetRef):
    tab: str
    header_row: int = Field(1, ge=1)
    start_row: int = Field(0, ge=0)
    end_row: int = Field(0, ge=0)
    only_empty: bool = True
    skip_sent: bool = True


class RowRequest(SheetRef):
    tab: str
    row: int = Field(..., ge=2)
    header_row: int = Field(1, ge=1)
    write: bool = True
    mode: str | None = None
    web_research: str | None = None
    skip_low_fit: bool = True


class ProspectIn(BaseModel):
    fname: str = ""
    lname: str = ""
    email: str = ""
    job_title: str = ""
    company: str
    country: str = ""
    timezone: str = ""


class GenerateRequest(BaseModel):
    prospect: ProspectIn
    mode: str | None = None
    web_research: str | None = None
    skip_low_fit: bool = False
    proof: list[str] = []
    assets: list[str] = []


def _options(settings: Settings, mode: str | None, web: str | None, skip_low_fit: bool) -> RowOptions:
    mode = mode if mode in ("deep", "fast") else settings.brain_mode
    web = web if web in ("fallback", "always", "off") else settings.web_research
    return RowOptions(mode=mode, web_research=web, skip_low_fit=skip_low_fit, signoff=settings.signoff)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    page = ROOT / "public" / "index.html"
    return HTMLResponse(page.read_text(encoding="utf-8") if page.exists() else "<p>UI file missing.</p>")


@app.get("/api/health")
def health(settings: Settings = Depends(authed)) -> dict:
    sa_email, sa_error = "", ""
    try:
        info = settings.service_account_info()
        sa_email = (info or {}).get("client_email", "")
    except RuntimeError as exc:
        sa_error = str(exc)
    return {
        "ok": True,
        "version": __version__,
        "gemini_key": bool(settings.gemini_api_key),
        "service_account_email": sa_email,
        "service_account_error": sa_error,
        "model": settings.model,
        "fallback_model": settings.fallback_model,
        "search_model": settings.search_model,
        "mode": settings.brain_mode,
        "web_research": settings.web_research,
        "row_delay_seconds": settings.row_delay_seconds,
        "max_news_age_days": settings.max_news_age_days,
    }


@app.post("/api/sheet/tabs")
def sheet_tabs(body: SheetRef, settings: Settings = Depends(authed)) -> dict:
    try:
        client = SheetClient(settings)
        return {"tabs": client.tabs(body.sheet), "service_account_email": client.email}
    except SheetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/sheet/inspect")
def sheet_inspect(body: InspectRequest, settings: Settings = Depends(authed)) -> dict:
    try:
        client = SheetClient(settings)
        return client.inspect(body.sheet, body.tab, RowFilter(body.header_row, body.start_row, body.end_row,
                                                              body.only_empty, body.skip_sent))
    except SheetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/row")
def process_row(body: RowRequest, settings: Settings = Depends(authed)) -> JSONResponse:
    try:
        client = SheetClient(settings)
        ws, cols, prospect, colleagues, kb = client.load_row(body.sheet, body.tab, body.row, body.header_row)
    except SheetError as exc:
        return JSONResponse({"row": body.row, "error": str(exc), "fatal": False})

    options = _options(settings, body.mode, body.web_research, body.skip_low_fit)
    try:
        llm = Gemini(settings, requests.Session())
        result = run_prospect(prospect, settings=settings, llm=llm, knowledge=kb, colleagues=colleagues, options=options)
    except FatalLLMError as exc:
        return JSONResponse({"row": body.row, "label": f"{prospect.fname} @ {prospect.company}", "error": str(exc), "fatal": True})
    except Exception as exc:  # one bad row must not stop the run
        traceback.print_exc()
        message = f"{type(exc).__name__}: {str(exc)[:180]}"
        if body.write:
            try:
                client.write_row(ws, cols, body.row, {"status": f"Error: {message}"}, body.header_row)
            except Exception:
                pass
        return JSONResponse({"row": body.row, "label": f"{prospect.fname} @ {prospect.company}", "error": message, "fatal": False})

    result["row"] = body.row
    if body.write:
        values = {"status": result["status"]}
        if not result.get("skipped"):
            values.update({"signal": result["signal"], "e1_subject": result["subject"], "e1_body": result["e1"],
                           "e2_body": result["e2"], "e3_body": result["e3"], "e4_body": result["e4"],
                           "dossier": result["dossier"] if "dossier" in cols else None,
                           "hooks": result["hooks"] if "hooks" in cols else None})
        try:
            client.write_row(ws, cols, body.row, values, body.header_row)
        except Exception as exc:
            result["status"] = f"Generated, but writing to the sheet failed: {str(exc)[:160]}"
            result["write_error"] = True
    return JSONResponse(result)


@app.post("/api/generate")
def generate(body: GenerateRequest, settings: Settings = Depends(authed)) -> JSONResponse:
    """Sheet-free endpoint: send one prospect as JSON, get the decision and the sequence back."""
    options = _options(settings, body.mode, body.web_research, body.skip_low_fit)
    try:
        llm = Gemini(settings, requests.Session())
        result = run_prospect(Prospect(**body.prospect.model_dump()), settings=settings, llm=llm,
                              knowledge=Knowledge(proof=body.proof, assets=body.assets), options=options)
        return JSONResponse(result)
    except FatalLLMError as exc:
        return JSONResponse({"error": str(exc), "fatal": True}, status_code=503)
    except Exception as exc:
        traceback.print_exc()
        return JSONResponse({"error": f"{type(exc).__name__}: {str(exc)[:200]}", "fatal": False}, status_code=500)


if os.environ.get("KR_LOCAL_DEV") == "1":  # uvicorn api.index:app --reload
    print("Kings Research Outreach Brain running locally.")
