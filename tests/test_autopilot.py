from __future__ import annotations

import base64

import httpx

from sentinel.autopilot import Autopilot, clean_topics, roadmap_items, seo_score
from sentinel.bot import Sentinel
from sentinel.github import GitHubError
from tests.test_bot import CFG, NOW, fake_llm
from tests.test_features import MemLedger

SRC = ("def total(items):\n    t = 0\n    for i in items:\n        t = i\n    return t\n"
       + "\n\ndef unused(x):\n    return x\n" * 12)
FIX = {"confident": True, "title": "Total keeps only the last item", "explanation": "The loop overwrites t.",
       "edits": [{"file": "app.py", "start": 4, "end": 4, "old": "        t = i", "new": "        t += i"}]}


class FakeGH:
    def __init__(self, admin: bool = True) -> None:
        self.writes: list[tuple[str, str, dict]] = []
        self.admin = admin
        self.repo = {"name": "app", "full_name": "owner/app", "default_branch": "main", "has_issues": True,
                     "private": False, "description": "x", "topics": [], "homepage": ""}
        self.files = {"app.py": SRC, "README.md": "# App\n\n## Roadmap\n- [ ] Add CSV export of results\n- [x] Done"}
        self.pulls: list[dict] = []
        self.issues: list[dict] = []
        self.refs: set[str] = set()
        self.release: dict | None = None

    def repos(self):
        return [self.repo]

    def paged(self, path, key=None, limit=300, **params):
        return self.pulls if path.endswith("/pulls") else self.issues

    def tree_entries(self, repo, branch):
        return [(p, len(t)) for p, t in self.files.items()]

    def tree(self, repo, branch):
        return list(self.files)

    def raw_file(self, repo, path):
        return self.files.get(path)

    def readme(self, repo):
        return self.files.get("README.md")

    def get(self, path, **params):
        if "/git/ref/heads/" in path:
            ref = path.split("/git/ref/heads/", 1)[1]
            if ref == "main":
                return {"object": {"sha": "headsha"}}
            if ref in self.refs:
                return {"object": {"sha": "x"}}
            raise GitHubError(f"GET {path} -> 404")
        if "/contents/" in path:
            return {"sha": "blobsha"}
        if path.endswith("/releases/latest"):
            if not self.release:
                raise GitHubError("404")
            return self.release
        raise GitHubError(f"GET {path} -> 404")

    def request(self, method, path, **kw):
        if not self.admin and (path == "/repos/owner/app" or path.endswith("/topics")):
            raise GitHubError(f"{method} {path} -> 403: Resource not accessible by integration")
        self.writes.append((method, path, kw.get("json") or {}))
        return {"number": 7} if path.endswith("/pulls") else {}

    def add_labels(self, repo, number, labels):
        self.writes.append(("LABEL", f"{repo}#{number}", {"labels": labels}))


def make(gh, llm, env=None, transport=None):
    bot = Sentinel(gh, CFG, llm=llm, now=NOW, ledger=MemLedger())
    return bot, Autopilot(bot, env or {}, transport=transport)


def test_fixer_opens_a_small_pr_with_a_clean_patch_and_runs_once_a_week():
    gh = FakeGH()
    bot, ap = make(gh, fake_llm(FIX))
    ap.fixer(gh.repo)
    put = [w for w in gh.writes if w[0] == "PUT"][0]
    assert base64.b64decode(put[2]["content"]).decode() == SRC.replace("t = i", "t += i")
    pr = [w for w in gh.writes if w[1] == "/repos/owner/app/pulls"][0][2]
    assert pr["title"].startswith("Fix:") and "+        t += i" in pr["body"] and pr["head"].startswith("sentinel/fix-")
    assert ("LABEL", "owner/app#7", {"labels": ["autofix", "automerge"]}) in gh.writes
    gh.writes.clear()
    ap.fixer(gh.repo)
    assert gh.writes == []


def test_fixer_skips_unconfident_or_non_matching_edits():
    gh = FakeGH()
    bad = {**FIX, "edits": [{**FIX["edits"][0], "old": "        t = 0"}]}
    for reply in ({"confident": False}, bad):
        g = FakeGH()
        _, ap = make(g, fake_llm(reply))
        ap.fixer(g.repo)
        assert not [w for w in g.writes if w[0] in ("PUT", "POST")]
    assert gh.writes == []


def test_merge_only_own_labelled_prs_with_green_checks():
    gh = FakeGH()
    bot, ap = make(gh, fake_llm(FIX))
    pr = {"number": 3, "labels": [{"name": "autofix"}, {"name": "automerge"}], "head": {"ref": "sentinel/fix-ab",
          "sha": "s1"}, "body": "<!-- nexonic-sentinel:autofix -->"}
    gh.pulls = [pr, {**pr, "number": 4, "labels": [*pr["labels"], {"name": "hold"}]},
                {**pr, "number": 5, "head": {"ref": "feature", "sha": "s2"}}]
    bot._check_runs = lambda repo, sha: [{"status": "completed", "conclusion": "success"}]
    bot._checks_green = lambda repo, sha: True
    ap.merge_ready(gh.repo)
    assert [w[1] for w in gh.writes] == ["/repos/owner/app/pulls/3/merge"]
    gh.writes.clear()
    bot._check_runs = lambda repo, sha: []  # no CI: never merged
    ap.merge_ready(gh.repo)
    assert gh.writes == []


def test_seo_report_applies_or_suggests():
    seo = {"description": "A small app that totals numbers for data teams, with CSV export and a simple API.",
           "topics": ["Python", "data tools", "bad topic!!", "csv"], "readme_tips": ["Add a usage example"]}
    gh = FakeGH()
    _, ap = make(gh, fake_llm(seo))
    ap.seo(gh.repos())
    assert ("PUT", "/repos/owner/app/topics", {"names": ["python", "data-tools", "bad-topic", "csv"]}) in gh.writes
    report = [w for w in gh.writes if w[1] == "/repos/owner/nexonic-sentinel/issues"][0][2]
    assert report["labels"] == ["seo"] and "| [app]" in report["body"] and "description, topics" in report["body"]
    gh2 = FakeGH(admin=False)
    _, ap2 = make(gh2, fake_llm(seo))
    ap2.seo(gh2.repos())
    body = [w for w in gh2.writes if w[1].endswith("/issues")][0][2]["body"]
    assert "suggested description" in body and "`data-tools`" in body


def test_contributor_issue_from_roadmap_once():
    gh = FakeGH()
    _, ap = make(gh, fake_llm({"title": "Add CSV export of results", "level": "good first issue",
                               "body": "Why: users asked.\n\n- [ ] export button"}))
    ap.contributors(gh.repo)
    issue = [w for w in gh.writes if w[1] == "/repos/owner/app/issues"][0][2]
    assert issue["labels"] == ["help wanted", "good first issue"] and "<!-- roadmap:" in issue["body"]
    assert roadmap_items(gh.files["README.md"]) == ["Add CSV export of results"]


def test_release_announced_on_configured_accounts_only():
    sent = []

    def handler(req):
        sent.append(str(req.url))
        if req.url.path.endswith("createSession"):
            return httpx.Response(200, json={"accessJwt": "j", "did": "did:x"})
        return httpx.Response(200, json={})
    gh = FakeGH()
    gh.release = {"id": 1, "tag_name": "v1.0", "published_at": "2026-10-25T10:00:00Z", "body": "New: CSV export",
                  "html_url": "https://github.com/owner/app/releases/tag/v1.0"}
    env = {"BLUESKY_HANDLE": "me.bsky.social", "BLUESKY_APP_PASSWORD": "p"}
    _, ap = make(gh, fake_llm({"text": "App v1.0 is out with CSV export."}), env, httpx.MockTransport(handler))
    ap.promote(gh.repo)
    assert any("createRecord" in u for u in sent) and not any("statuses" in u for u in sent)
    sent.clear()
    ap.promote(gh.repo)  # announced once
    assert sent == []


def test_helpers():
    assert clean_topics(["A B", "a-b", "x" * 60, "ok"]) == ["a-b", "x" * 50, "ok"]
    score, missing = seo_score({"description": "short"}, "# T\n", {"LICENSE"})
    assert score == 20 and "at least 5 topics" in missing
