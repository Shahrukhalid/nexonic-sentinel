"""GitHub App authentication: a short-lived app JWT lists the installations, then one token per installation."""

from __future__ import annotations

import time
from typing import Any

import httpx
import jwt

API = "https://api.github.com"


class App:
    def __init__(self, client_id: str, private_key: str, transport: httpx.BaseTransport | None = None,
                 api: str = API) -> None:
        self.client_id, self.private_key = client_id, private_key
        self.http = httpx.Client(base_url=api, transport=transport, timeout=30,
                                 headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                                          "User-Agent": "nexonic-sentinel"})

    def _jwt(self) -> str:
        now = int(time.time())
        # The client ID is accepted as the issuer; iat is backdated for clock drift, exp stays under 10 minutes.
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": self.client_id}, self.private_key, algorithm="RS256")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._jwt()}"}

    def installations(self) -> list[dict[str, Any]]:
        out, page = [], 1
        while True:
            r = self.http.get("/app/installations", params={"per_page": 100, "page": page}, headers=self._headers())
            r.raise_for_status()
            items = r.json()
            out.extend(i for i in items if not i.get("suspended_at"))
            if len(items) < 100:
                return out
            page += 1

    def token(self, installation_id: int) -> str:
        r = self.http.post(f"/app/installations/{installation_id}/access_tokens", headers=self._headers())
        r.raise_for_status()
        return r.json()["token"]
