"""Free-model answers through OpenRouter, grounded only in the repository's own documentation."""

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


class OpenRouter:
    def __init__(self, api_key: str, models: list[str], transport: httpx.BaseTransport | None = None) -> None:
        self.models = models
        self.http = httpx.Client(base_url=OPENROUTER, transport=transport, timeout=90,
                                 headers={"Authorization": f"Bearer {api_key}",
                                          "X-Title": "Nexonic Sentinel"})
        self.calls = 0

    def answer(self, repo: str, question: str, excerpts: list[tuple[str, str]]) -> dict[str, Any]:
        context = "\n\n".join(f"### {path}\n{text}" for path, text in excerpts)
        messages = [{"role": "system", "content": SYSTEM.format(repo=repo)},
                    {"role": "user", "content": f"Documentation excerpts:\n{context}\n\nQuestion:\n{question}"}]
        last = "no model available"
        for model in self.models:
            self.calls += 1
            try:
                r = self.http.post("/chat/completions", json={"model": model, "messages": messages,
                                                              "temperature": 0.2, "max_tokens": 900})
            except httpx.HTTPError as exc:
                last = f"{model}: {exc.__class__.__name__}"
                continue
            if r.status_code != 200:
                last = f"{model}: HTTP {r.status_code}"
                continue
            try:
                content = r.json()["choices"][0]["message"]["content"] or ""
                return parse_reply(content, {p for p, _ in excerpts})
            except (KeyError, IndexError, ValueError) as exc:
                last = f"{model}: unreadable reply ({exc})"
        raise LLMError(last)


def parse_reply(content: str, known_paths: set[str]) -> dict[str, Any]:
    """Accept the JSON object even when wrapped in a code fence; drop sources that were not provided."""
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        raise ValueError("no JSON object")
    data = json.loads(m.group(0))
    answer = str(data.get("answer") or "").strip()
    sources = [s for s in data.get("sources") or [] if isinstance(s, str) and s in known_paths]
    confident = bool(data.get("confident")) and len(answer) >= 20 and bool(sources)
    return {"confident": confident, "answer": answer, "sources": sources}
