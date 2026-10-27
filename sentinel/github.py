"""Small GitHub REST + GraphQL client used by the bot (installation token from the GitHub App)."""

from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import quote

import httpx

API = "https://api.github.com"
READ_METHODS = {"GET", "HEAD", "OPTIONS"}
MAX_WAIT = 60  # longest rate-limit wait (seconds) before giving up on a request; the next run picks it up


class GitHubError(RuntimeError):
    pass


class GitHub:
    def __init__(self, token: str, transport: httpx.BaseTransport | None = None, api: str = API) -> None:
        self.http = httpx.Client(
            base_url=api, transport=transport, timeout=30,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "nexonic-sentinel"})

    # ------------------------------------------------------------------ low level
    @staticmethod
    def _rate_limit_wait(r: httpx.Response) -> float | None:
        """Seconds to wait when GitHub says we hit a primary or secondary rate limit, else None."""
        if r.status_code not in (403, 429):
            return None
        retry = r.headers.get("retry-after")
        if retry and retry.isdigit():
            return float(retry)
        if r.headers.get("x-ratelimit-remaining") == "0":
            reset = r.headers.get("x-ratelimit-reset", "")
            return max(1.0, float(reset) - time.time()) if reset.isdigit() else float(MAX_WAIT)
        if r.status_code == 429 or "secondary rate limit" in r.text.lower():
            return float(MAX_WAIT)
        return None

    def request(self, method: str, path: str, *, read_only: bool = False, **kw) -> Any:
        """Retries cover reads freely. A write is replayed only when the request provably never left (connection
        failures); after a timeout or a 5xx GitHub may already have applied it, and a replay would duplicate it."""
        reads = read_only or method.upper() in READ_METHODS
        for attempt in range(3):
            try:
                r = self.http.request(method, path, **kw)
            except httpx.TransportError as exc:
                never_sent = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))
                if attempt < 2 and (reads or never_sent):
                    time.sleep(2 * (attempt + 1))
                    continue
                raise GitHubError(f"{method} {path} -> {exc.__class__.__name__}") from exc
            if r.status_code in (502, 503, 504) and attempt < 2 and reads:
                time.sleep(2 * (attempt + 1))
                continue
            wait = self._rate_limit_wait(r)
            if wait is not None and wait <= MAX_WAIT and attempt < 2:
                time.sleep(wait)
                continue
            if r.status_code >= 400:
                raise GitHubError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
            return r.json() if r.content else None
        raise GitHubError(f"{method} {path} failed")

    def get(self, path: str, **params) -> Any:
        return self.request("GET", path, params=params or None)

    def paged(self, path: str, key: str | None = None, limit: int = 300, **params) -> list[dict]:
        out: list[dict] = []
        page = 1
        while len(out) < limit:
            data = self.get(path, per_page=100, page=page, **params)
            items = data.get(key, []) if key else data
            out.extend(items)
            if len(items) < 100:
                break
            page += 1
        return out[:limit]

    def graphql(self, query: str, **variables) -> dict:
        read_only = not re.search(r"\bmutation\b", query, re.I)  # anything unclear counts as a write
        data = self.request("POST", "/graphql", read_only=read_only, json={"query": query, "variables": variables})
        if data.get("errors"):
            raise GitHubError(f"GraphQL: {data['errors'][0].get('message')}")
        return data["data"]

    # ------------------------------------------------------------------ helpers
    def repos(self) -> list[dict]:
        return [r for r in self.paged("/installation/repositories", key="repositories")
                if not r.get("archived") and not r.get("fork")]

    def raw_file(self, repo: str, path: str) -> str | None:
        """File text, or None only when the file genuinely does not exist (404). Rate limits, permission errors and
        outages raise, so callers never mistake "could not read" for "no such file" (e.g. missing settings)."""
        url = f"/repos/{repo}/contents/{quote(path)}"
        try:
            r = self.http.get(url, headers={"Accept": "application/vnd.github.raw+json"})
        except httpx.TransportError as exc:
            raise GitHubError(f"GET {url} -> {exc.__class__.__name__}") from exc
        if r.status_code == 200:
            return r.text
        if r.status_code == 404:
            return None
        raise GitHubError(f"GET {url} -> {r.status_code}")

    def readme(self, repo: str) -> str | None:
        """The repository's README text (any file name), or None when it has none. Read-only."""
        url = f"/repos/{repo}/readme"
        try:
            r = self.http.get(url, headers={"Accept": "application/vnd.github.raw+json"})
        except httpx.TransportError as exc:
            raise GitHubError(f"GET {url} -> {exc.__class__.__name__}") from exc
        if r.status_code == 200:
            return r.text
        if r.status_code == 404:
            return None
        raise GitHubError(f"GET {url} -> {r.status_code}")

    def tree_entries(self, repo: str, branch: str) -> list[tuple[str, int]]:
        try:
            data = self.get(f"/repos/{repo}/git/trees/{branch}", recursive=1)
        except GitHubError:
            return []
        return [(t["path"], int(t.get("size") or 0)) for t in data.get("tree", []) if t.get("type") == "blob"]

    def tree(self, repo: str, branch: str) -> list[str]:
        return [p for p, _ in self.tree_entries(repo, branch)]

    def comment(self, repo: str, number: int, body: str) -> dict:
        return self.request("POST", f"/repos/{repo}/issues/{number}/comments", json={"body": body})

    def add_labels(self, repo: str, number: int, labels: list[str]) -> None:
        if labels:
            self.request("POST", f"/repos/{repo}/issues/{number}/labels", json={"labels": labels})

    def discussion_comment(self, discussion_id: str, body: str) -> None:
        self.graphql("mutation($id:ID!,$body:String!){addDiscussionComment(input:{discussionId:$id,body:$body})"
                     "{comment{id}}}", id=discussion_id, body=body)
