"""Google Sheets access through a service account (share the sheet with its email as Editor)."""

from __future__ import annotations

import re
from dataclasses import dataclass

import gspread
from gspread.utils import rowcol_to_a1

from .config import Settings
from .pipeline import Colleague, Knowledge, Prospect
from .text import company_core

COLS = {
    "fname": ["fname", "firstname"],
    "lname": ["lname", "lastname"],
    "email": ["email", "emailaddress", "workemail"],
    "job_title": ["jobtitle", "designation", "title", "position"],
    "company": ["company", "companyname", "organization", "organisation", "account"],
    "country": ["country"],
    "timezone": ["timezone"],
    "signal": ["signal"],
    "e1_subject": ["e1subject"],
    "e1_body": ["e1body"],
    "e2_body": ["e2body"],
    "e3_body": ["e3body"],
    "e4_body": ["e4body"],
    "status": ["status"],
    "dossier": ["dossier"],
    "hooks": ["hooks"],
}
REQUIRED_INPUT = ["fname", "job_title", "company"]
OUTPUT_KEYS = ["signal", "e1_subject", "e1_body", "e2_body", "e3_body", "e4_body", "status"]
OPTIONAL_OUTPUT_KEYS = ["dossier", "hooks"]  # written only if the tab already has these columns
OUTPUT_HEADERS = {"signal": "Signal", "e1_subject": "E1 Subject", "e1_body": "E1 Body", "e2_body": "E2 Body",
                  "e3_body": "E3 Body", "e4_body": "E4 Body", "status": "Status"}
PROOF_TAB = "KR_Proof"
CELL_LIMIT = 49000  # Google Sheets cell limit is 50,000 characters


class SheetError(RuntimeError):
    pass


def spreadsheet_id(url_or_id: str) -> str:
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url_or_id or "")
    if m:
        return m.group(1)
    if re.fullmatch(r"[a-zA-Z0-9-_]{20,}", (url_or_id or "").strip()):
        return url_or_id.strip()
    raise SheetError("That doesn't look like a Google Sheets link or ID.")


def norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(h or "").lower())


def map_headers(header_row: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for i, h in enumerate(header_row):
        n = norm_header(h)
        if not n:
            continue
        for key, aliases in COLS.items():
            if key not in mapping and n in aliases:
                mapping[key] = i
    return mapping


@dataclass
class RowFilter:
    header_row: int = 1
    start_row: int = 0
    end_row: int = 0
    only_empty: bool = True
    skip_sent: bool = True


class SheetClient:
    def __init__(self, settings: Settings):
        info = settings.service_account_info()
        if not info:
            raise SheetError("GOOGLE_SERVICE_ACCOUNT_JSON is not set in the Vercel environment variables.")
        self.email = info.get("client_email", "")
        self.gc = gspread.service_account_from_dict(info)

    def open(self, url_or_id: str) -> gspread.Spreadsheet:
        try:
            return self.gc.open_by_key(spreadsheet_id(url_or_id))
        except (gspread.exceptions.SpreadsheetNotFound, PermissionError) as exc:
            raise SheetError(f"Can't open the sheet. Share it with {self.email} as Editor.") from exc
        except gspread.exceptions.APIError as exc:
            status = getattr(exc.response, "status_code", 0)
            if status == 403:
                raise SheetError(f"Permission denied. Share the sheet with {self.email} as Editor, "
                                 "and make sure the Google Sheets API is enabled for the service account's project.") from exc
            raise SheetError(f"Google Sheets error: {exc}") from exc

    def tabs(self, url_or_id: str) -> list[dict]:
        ss = self.open(url_or_id)
        return [{"title": ws.title, "rows": ws.row_count} for ws in ss.worksheets() if ws.title != PROOF_TAB]

    @staticmethod
    def worksheet(ss: gspread.Spreadsheet, tab: str) -> gspread.Worksheet:
        try:
            return ss.worksheet(tab)
        except gspread.exceptions.WorksheetNotFound as exc:
            raise SheetError(f'Tab "{tab}" was not found.') from exc

    # -- reading ----------------------------------------------------------------
    @staticmethod
    def _values(ws: gspread.Worksheet) -> list[list[str]]:
        return ws.get_all_values()

    def inspect(self, url_or_id: str, tab: str, f: RowFilter) -> dict:
        ss = self.open(url_or_id)
        ws = self.worksheet(ss, tab)
        values = self._values(ws)
        if len(values) < f.header_row:
            return {"ok": False, "message": "This tab is empty.", "eligible_rows": [], "total_rows": 0, "missing_output": []}
        header = values[f.header_row - 1]
        cols = map_headers(header)
        missing = [k for k in REQUIRED_INPUT if k not in cols]
        missing_output = [OUTPUT_HEADERS[k] for k in OUTPUT_KEYS if k not in cols]
        eligible = [] if missing else eligible_rows(values, cols, f)
        return {
            "ok": not missing,
            "message": f"Missing required columns in row {f.header_row}: {', '.join(missing)}" if missing else "",
            "eligible_rows": eligible,
            "total_rows": max(0, len(values) - f.header_row),
            "missing_output": missing_output,
            "has_dossier": "dossier" in cols,
            "has_hooks": "hooks" in cols,
        }

    def load_row(self, url_or_id: str, tab: str, row: int, header_row: int) -> tuple[gspread.Worksheet, dict, Prospect, list[Colleague], Knowledge]:
        ss = self.open(url_or_id)
        ws = self.worksheet(ss, tab)
        values = self._values(ws)
        if row <= header_row or row > len(values):
            raise SheetError(f"Row {row} is outside the data in this tab.")
        cols = map_headers(values[header_row - 1])
        missing = [k for k in REQUIRED_INPUT if k not in cols]
        if missing:
            raise SheetError(f"Missing required columns: {', '.join(missing)}")
        r = values[row - 1]

        def get(key: str) -> str:
            i = cols.get(key)
            return r[i].strip() if i is not None and i < len(r) else ""

        prospect = Prospect(fname=get("fname"), lname=get("lname"), email=get("email"), job_title=get("job_title"),
                            company=get("company"), country=get("country"), timezone=get("timezone"))
        colleagues = collect_colleagues(values, cols, header_row, row, prospect.company)
        return ws, cols, prospect, colleagues, self.knowledge(ss)

    @staticmethod
    def knowledge(ss: gspread.Spreadsheet) -> Knowledge:
        try:
            ws = ss.worksheet(PROOF_TAB)
        except gspread.exceptions.WorksheetNotFound:
            return Knowledge()
        kb = Knowledge()
        for r in ws.get_all_values()[1:]:
            kind = (r[0] if r else "").strip().lower()
            text = (r[1] if len(r) > 1 else "").strip()
            applies = (r[2] if len(r) > 2 else "").strip()
            if not text:
                continue
            item = text + (f" (applies to: {applies})" if applies else "")
            (kb.assets if "asset" in kind else kb.proof).append(item)
        return kb

    # -- writing ----------------------------------------------------------------
    def ensure_output_columns(self, ws: gspread.Worksheet, cols: dict, header_row: int) -> dict:
        missing = [k for k in OUTPUT_KEYS if k not in cols]
        if not missing:
            return cols
        header = ws.row_values(header_row)
        next_col = len(header) + 1
        needed = next_col + len(missing) - 1
        if needed > ws.col_count:
            ws.add_cols(needed - ws.col_count)
        updates = []
        for k in missing:
            updates.append({"range": rowcol_to_a1(header_row, next_col), "values": [[OUTPUT_HEADERS[k]]]})
            cols[k] = next_col - 1
            next_col += 1
        ws.batch_update(updates, raw=True)
        return cols

    def write_row(self, ws: gspread.Worksheet, cols: dict, row: int, values: dict, header_row: int) -> None:
        cols = self.ensure_output_columns(ws, cols, header_row)
        updates = []
        for key, value in values.items():
            if key not in cols or value is None:
                continue
            text = str(value)[:CELL_LIMIT]
            updates.append({"range": rowcol_to_a1(row, cols[key] + 1), "values": [[text]]})
        if updates:
            ws.batch_update(updates, raw=True)  # raw: text is never parsed as a formula


def eligible_rows(values: list[list[str]], cols: dict, f: RowFilter) -> list[int]:
    first = max(f.header_row + 1, f.start_row or 0)
    last = min(f.end_row, len(values)) if f.end_row else len(values)
    out = []
    for row in range(first, last + 1):
        r = values[row - 1]

        def cell(key: str) -> str:
            i = cols.get(key)
            return r[i].strip() if i is not None and i < len(r) else ""

        if not cell("company"):
            continue
        if f.only_empty and cell("e1_body"):
            continue
        if f.skip_sent and re.search(r"\bsent\b", cell("status"), re.I):
            continue
        out.append(row)
    return out


def collect_colleagues(values: list[list[str]], cols: dict, header_row: int, row: int, company: str) -> list[Colleague]:
    if "e1_subject" not in cols:
        return []
    target = company_core(company)
    out: list[Colleague] = []
    for idx in range(header_row, len(values)):
        if idx + 1 == row:
            continue
        r = values[idx]

        def cell(key: str) -> str:
            i = cols.get(key)
            return r[i].strip() if i is not None and i < len(r) else ""

        if company_core(cell("company")) != target or not cell("e1_subject"):
            continue
        out.append(Colleague(title=cell("job_title"), subject=cell("e1_subject"), signal_cell=cell("signal")))
    return out[:8]
