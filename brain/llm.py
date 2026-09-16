"""Small, dependable Gemini REST client."""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass

import requests

from .config import Settings

API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"


class LLMError(RuntimeError):
    """A request failed for this row; the next row can still work."""


class FatalLLMError(LLMError):
    """Stop the whole run: bad key, missing model, or every model's daily quota is gone."""


class QuotaExhausted(FatalLLMError):
    pass


@dataclass
class LLMResponse:
    text: str
    raw: dict
    model: str

    @property
    def grounding_chunks(self) -> list[dict]:
        cand = (self.raw.get("candidates") or [{}])[0]
        meta = cand.get("groundingMetadata") or {}
        return [c.get("web", {}) for c in meta.get("groundingChunks", []) if c.get("web")]


class Gemini:
    _lock = threading.Lock()
    _last_call = 0.0

    def __init__(self, settings: Settings, session: requests.Session | None = None):
        if not settings.gemini_api_key:
            raise FatalLLMError("GEMINI_API_KEY is not set in the Vercel environment variables.")
        self.s = settings
        self.http = session or requests.Session()
        self.calls = 0

    # -- pacing -------------------------------------------------------------
    def _pace(self) -> None:
        with Gemini._lock:
            wait = self.s.gemini_min_interval - (time.monotonic() - Gemini._last_call)
            if wait > 0:
                time.sleep(wait)
            Gemini._last_call = time.monotonic()

    # -- core ---------------------------------------------------------------
    def _post(self, model: str, payload: dict, deadline: float | None) -> LLMResponse:
        url = f"{API_ROOT}/{model}:generateContent"
        wait = 5.0
        for _attempt in range(4):
            if deadline and time.monotonic() > deadline:
                raise LLMError("Ran out of time for this row.")
            self._pace()
            self.calls += 1
            try:
                resp = self.http.post(url, json=payload, headers={"x-goog-api-key": self.s.gemini_api_key}, timeout=90)
            except requests.RequestException as exc:
                if _attempt == 3:
                    raise LLMError(f"Network error calling Gemini: {exc}") from exc
                time.sleep(wait)
                wait = min(wait * 2, 30)
                continue

            body = resp.text
            if resp.status_code == 200:
                data = resp.json()
                cands = data.get("candidates") or []
                if not cands:
                    reason = (data.get("promptFeedback") or {}).get("blockReason", "")
                    raise LLMError("Gemini returned no answer" + (f" ({reason})" if reason else ""))
                parts = (cands[0].get("content") or {}).get("parts") or []
                text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                if not text.strip():
                    finish = cands[0].get("finishReason", "")
                    raise LLMError(f"Gemini returned an empty answer ({finish or 'no text'}).")
                return LLMResponse(text=text, raw=data, model=model)

            try:
                message = resp.json().get("error", {}).get("message", body)
            except ValueError:
                message = body
            if resp.status_code == 429:
                if re.search(r"PerDay", body) or re.search(r"per day", message, re.I):
                    raise QuotaExhausted(f"Daily free quota used up for {model}.")
                m = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', body)
                delay = min(float(m.group(1)) + 1, 60) if m else wait
                if deadline and time.monotonic() + delay > deadline:
                    raise LLMError("Gemini is rate limiting and this row is out of time. Increase GEMINI_MIN_INTERVAL_SECONDS.")
                time.sleep(delay)
                wait = min(wait * 2, 60)
                continue
            if resp.status_code >= 500:
                time.sleep(wait)
                wait = min(wait * 2, 60)
                continue
            if resp.status_code == 404:
                raise FatalLLMError(f'Model "{model}" was not found. Set a current model name in the environment variables.')
            if resp.status_code == 400 and re.search(r"api key", message, re.I):
                raise FatalLLMError("The Gemini API key is not valid.")
            if resp.status_code in (401, 403):
                raise FatalLLMError(f"Gemini refused the request: {message[:200]}")
            raise LLMError(f"Gemini error {resp.status_code}: {message[:200]}")
        raise LLMError("Gemini kept failing or rate limiting this request.")

    def generate(self, *, prompt: str, system: str | None = None, schema: dict | None = None,
                 temperature: float = 0.4, models: list[str] | None = None, search: bool = False,
                 deadline: float | None = None) -> LLMResponse:
        payload: dict = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature},
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        if schema and not search:
            payload["generationConfig"]["responseMimeType"] = "application/json"
            payload["generationConfig"]["responseSchema"] = schema
        if search:
            payload["tools"] = [{"google_search": {}}]

        chain: list[str] = []
        for m in models or [self.s.model, self.s.fallback_model]:
            if m and m not in chain:
                chain.append(m)
        last: Exception | None = None
        for model in chain:
            try:
                return self._post(model, payload, deadline)
            except QuotaExhausted as exc:
                last = exc
                continue
        raise QuotaExhausted(
            f"Daily free Gemini quota is used up for {', '.join(chain)}. Finished rows are saved; "
            "run again after the quota resets (midnight Pacific time)."
        ) from last

    def generate_json(self, **kwargs) -> tuple[dict, LLMResponse]:
        resp = self.generate(**kwargs)
        data = parse_json_loose(resp.text)
        if not isinstance(data, dict):
            raise LLMError("Gemini returned text that was not a JSON object.")
        return data, resp


def parse_json_loose(text: str):
    t = (text or "").strip()
    for candidate in (t, re.sub(r"^```(?:json)?\s*|\s*```$", "", t)):
        try:
            return json.loads(candidate)
        except (ValueError, TypeError):
            pass
    a, b = t.find("{"), t.rfind("}")
    if a != -1 and b > a:
        try:
            return json.loads(t[a:b + 1])
        except ValueError:
            return None
    return None
