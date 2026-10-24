"""Small GitHub REST + GraphQL client used by the bot (installation token from the GitHub App)."""

from __future__ import annotations

import time
from typing import Any

import httpx

API = "https://api.github.com"


class GitHubError(RuntimeError):
    pass


class GitHub:
    def __init__(self, token: str, transport: httpx.BaseTransport | None = None, api: str = API) -> None:
        self.http = httpx.Client(
            base_url=api, transport=transport, timeout=30,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "nexonic-sentinel"})

    # ------------------------------------------------------------------ low level
    def request(self, method: str, path: str, **kw) -> Any:
        for attempt in range(3):
            r = self.http.request(method, path, **kw)
            if r.status_code in (502, 503, 504) and attempt < 2:
                time.sleep(2 * (attempt + 1))
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
        data = self.request("POST", "/graphql", json={"query": query, "variables": variables})
        if data.get("errors"):
            raise GitHubError(f"GraphQL: {data['errors'][0].get('message')}")
        return data["data"]

    # ------------------------------------------------------------------ helpers
    def repos(self) -> list[dict]:
        return [r for r in self.paged("/installation/repositories", key="repositories")
                if not r.get("archived") and not r.get("fork")]

    def raw_file(self, repo: str, path: str) -> str | None:
        r = self.http.get(f"/repos/{repo}/contents/{path}", headers={"Accept": "application/vnd.github.raw+json"})
        return r.text if r.status_code == 200 else None

    def tree(self, repo: str, branch: str) -> list[str]:
        try:
            data = self.get(f"/repos/{repo}/git/trees/{branch}", recursive=1)
        except GitHubError:
            return []
        return [t["path"] for t in data.get("tree", []) if t.get("type") == "blob"]

    def comment(self, repo: str, number: int, body: str) -> dict:
        return self.request("POST", f"/repos/{repo}/issues/{number}/comments", json={"body": body})

    def add_labels(self, repo: str, number: int, labels: list[str]) -> None:
        if labels:
            self.request("POST", f"/repos/{repo}/issues/{number}/labels", json={"labels": labels})

    def discussion_comment(self, discussion_id: str, body: str) -> None:
        self.graphql("mutation($id:ID!,$body:String!){addDiscussionComment(input:{discussionId:$id,body:$body})"
                     "{comment{id}}}", id=discussion_id, body=body)
