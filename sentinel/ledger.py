"""A tiny state store kept in one issue of the bot's own repository.

The bot runs on a schedule without a database. Weekly and daily jobs (code scans, the contribution shortlist,
the AI call budget) record what they did here so they do not repeat. Repository names are stored only as short
hashes, so installations on other accounts are never revealed in this public repository.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .github import GitHub

TITLE = "Nexonic Sentinel ledger"
MARK = "<!-- nexonic-sentinel:ledger -->"
BLOCK = re.compile(r"```json\n(.*?)\n```", re.S)


def key(*parts: str) -> str:
    return hashlib.sha256("/".join(parts).lower().encode()).hexdigest()[:12]


class Ledger:
    def __init__(self, gh: GitHub | None, home: str, dry_run: bool = False) -> None:
        self.gh, self.home, self.dry = gh, home, dry_run
        self.data: dict[str, Any] = {}
        self.number: int | None = None
        self.loaded_text = ""
        if gh is not None:
            self._load()

    def _load(self) -> None:
        for issue in self.gh.paged(f"/repos/{self.home}/issues", state="all", labels="ledger", limit=10):
            if MARK in (issue.get("body") or ""):
                self.number = issue["number"]
                m = BLOCK.search(issue["body"])
                if m:
                    try:
                        self.data = json.loads(m.group(1))
                    except ValueError:
                        self.data = {}
                break
        self.loaded_text = json.dumps(self.data, sort_keys=True)

    def get(self, k: str, default: Any = None) -> Any:
        return self.data.get(k, default)

    def set(self, k: str, value: Any) -> None:
        self.data[k] = value

    def save(self) -> None:
        text = json.dumps(self.data, sort_keys=True)
        if self.gh is None or self.dry or text == self.loaded_text:
            return
        body = (f"{MARK}\nState for scheduled jobs. Written by the bot; repository names are hashed.\n\n"
                f"```json\n{json.dumps(self.data, indent=1, sort_keys=True)}\n```")
        if self.number is None:
            issue = self.gh.request("POST", f"/repos/{self.home}/issues",
                                    json={"title": TITLE, "body": body, "labels": ["ledger"]})
            self.number = issue["number"]
            self.gh.request("PATCH", f"/repos/{self.home}/issues/{self.number}", json={"state": "closed"})
        else:
            self.gh.request("PATCH", f"/repos/{self.home}/issues/{self.number}", json={"body": body})
        self.loaded_text = text
