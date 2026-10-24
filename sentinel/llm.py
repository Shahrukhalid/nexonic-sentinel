"""Free-model calls through OpenRouter. Every caller asks for JSON and validates what comes back."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

OPENROUTER = "https://openrouter.ai/api/v1"

SYSTEM = """You are Nexonic Sentinel, the maintainer assistant bot for the open-source repository {repo}.
Answer the user's question ONLY from the documentation excerpts provided. Rules:
- If the excerpts do not clearly answer the question, set "confident" to false. Never guess.
- Never give security, legal, medical or financial advice; set "confident" to false for those.
- Be concise and friendly, use Markdown, include commands or file paths from the excerpts when useful.
- Do not invent features, versions, URLs or people.
Reply with JSON only: {{"confident": true|false, "answer": "...", "sources": ["path", ...]}}"""


class LLMError(RuntimeError):
    pass


class LLMQuotaExceeded(LLMError):
    """Every model answered 429: the free daily quota is used up, so stop asking for this run."""


ALL_FREE = "all-free"
MAX_TRIES = 4  # models tried for one prompt before giving up on it


def pick_free(catalog: list[dict[str, Any]], limit: int = 12, exclude: list[str] | None = None) -> list[str]:
    """Every text-only model OpenRouter offers at no cost, largest context window first."""
    skip = [e.lower() for e in exclude or []]
    out = []
    for m in catalog:
        mid = str(m.get("id", ""))
        price = m.get("pricing") or {}
        free = mid.endswith(":free") or (str(price.get("prompt")) in {"0", "0.0"}
                                         and str(price.get("completion")) in {"0", "0.0"})
        mods = (m.get("architecture") or {}).get("output_modalities") or ["text"]
        if not free or list(mods) != ["text"] or mid.startswith("openrouter/") or any(e in mid.lower() for e in skip):
            continue
        out.append((int(m.get("context_length") or 0), mid))
    out.sort(key=lambda t: (-t[0], t[1]))
    return [mid for _, mid in out[:limit]]


class OpenRouter:
    def __init__(self, api_key: str, models: list[str], transport: httpx.BaseTransport | None = None,
                 budget: int | None = None, free_limit: int = 12, exclude: list[str] | None = None) -> None:
        self.models = list(models)
        self.free_limit, self.exclude = free_limit, exclude or []
        self.resolved = ALL_FREE not in self.models
        self.http = httpx.Client(base_url=OPENROUTER, transport=transport, timeout=120,
                                 headers={"Authorization": f"Bearer {api_key}",
                                          "X-Title": "Nexonic Sentinel"})
        self.calls = 0
        self.budget = budget  # total calls allowed in this run (None = unlimited)
        self.exhausted = False
        self.limited: set[str] = set()  # models that hit their rate limit during this run

    def _resolve(self) -> None:
        """Expand "all-free" into the live list of free models (fetched once per run)."""
        if self.resolved:
            return
        self.resolved = True
        rest = [m for m in self.models if m != ALL_FREE]
        try:
            r = self.http.get("/models")
            r.raise_for_status()
            free = pick_free(r.json().get("data", []), self.free_limit, self.exclude)
        except (httpx.HTTPError, ValueError, AttributeError):
            free = []
        self.models = rest + [m for m in free if m not in rest]

    def key_info(self) -> dict[str, Any]:
        """OpenRouter's view of the key: is_free_tier is false once the account has bought credits."""
        try:
            r = self.http.get("/key")
            r.raise_for_status()
            return r.json().get("data") or {}
        except (httpx.HTTPError, ValueError):
            return {}

    def available(self) -> bool:
        return not self.exhausted and (self.budget is None or self.calls < self.budget)

    def complete(self, system: str, user: str, max_tokens: int = 1200) -> str:
        """Try each model in order and return the first non-empty reply."""
        if not self.available():
            raise LLMQuotaExceeded("AI budget for this run is used up")
        self._resolve()
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        last, tries = "no model available", 0
        for model in [m for m in self.models if m not in self.limited]:
            if tries >= MAX_TRIES or not self.available():
                break
            tries += 1
            self.calls += 1
            try:
                r = self.http.post("/chat/completions", json={"model": model, "messages": messages,
                                                              "temperature": 0.1, "max_tokens": max_tokens})
            except httpx.HTTPError as exc:
                last = f"{model}: {exc.__class__.__name__}"
                continue
            if r.status_code == 429:
                self.limited.add(model)
                last = f"{model}: HTTP 429"
                continue
            if r.status_code != 200:
                last = f"{model}: HTTP {r.status_code}"
                continue
            try:
                content = r.json()["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError, ValueError):
                last = f"{model}: unreadable reply"
                continue
            if content.strip():
                if model != self.models[0]:  # keep using the model that works
                    self.models.remove(model)
                    self.models.insert(0, model)
                return content
            last = f"{model}: empty reply"
        if self.models and all(m in self.limited for m in self.models):
            self.exhausted = True
            raise LLMQuotaExceeded("free AI quota reached (HTTP 429 from every model)")
        raise LLMError(last)

    def answer(self, repo: str, question: str, excerpts: list[tuple[str, str]]) -> dict[str, Any]:
        context = "\n\n".join(f"### {path}\n{text}" for path, text in excerpts)
        content = self.complete(SYSTEM.format(repo=repo), f"Documentation excerpts:\n{context}\n\nQuestion:\n{question}", 900)
        try:
            return parse_reply(content, {p for p, _ in excerpts})
        except ValueError as exc:
            raise LLMError(f"unreadable answer ({exc})") from exc


def json_from(content: str) -> Any:
    """Return the first JSON object in a reply, even when it is wrapped in a code fence or prose."""
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", content, re.S)
    text = fenced.group(1) if fenced else content
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("unterminated JSON object")


def parse_reply(content: str, known_paths: set[str]) -> dict[str, Any]:
    """Accept the JSON object even when wrapped in a code fence; drop sources that were not provided."""
    data = json_from(content)
    answer = str(data.get("answer") or "").strip()
    sources = [s for s in data.get("sources") or [] if isinstance(s, str) and s in known_paths]
    confident = bool(data.get("confident")) and len(answer) >= 20 and bool(sources)
    return {"confident": confident, "answer": answer, "sources": sources}


def safe_text(value: Any, limit: int) -> str:
    """Model output goes into public comments: no @mentions (no pings), no hidden HTML, bounded length."""
    text = str(value or "").replace("\r", "").strip()
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S).replace("<!--", "").replace("-->", "")
    text = re.sub(r"(?<![\w`])@(?=[A-Za-z0-9])", "@​", text)
    text = re.sub(r"</?(script|iframe|img|details|summary|style)[^>]*>", "", text, flags=re.I)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
