from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from sentinel.bot import Sentinel, bump_kind, marker
from sentinel.github import GitHub
from sentinel.knowledge import best_excerpts, doc_paths
from sentinel.llm import OpenRouter, parse_reply

NOW = datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc)
RECENT = "2026-10-25T10:00:00Z"
OLD = "2026-09-01T10:00:00Z"
CFG = {"owner": "owner", "lookback_hours": 72, "max_llm_calls_per_run": 3, "models": ["m1", "m2"],
       "automerge": {"dependabot": ["patch", "minor"]}, "skip_repos": ["owner"], "home_repo": "nexonic-sentinel"}


class FakeGitHub:
    """Just enough of the GitHub API, recording every write."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, str, dict]] = []
        self.repo = {"name": "app", "full_name": "owner/app", "default_branch": "main", "has_issues": True,
                     "has_discussions": True, "archived": False, "fork": False, "stargazers_count": 3}
        self.issues: list[dict] = []
        self.comments: dict[int, list[dict]] = {}
        self.pulls: list[dict] = []
        self.pull_detail: dict[int, dict] = {}
        self.checks: dict[str, list[dict]] = {}
        self.discussions: list[dict] = []
        self.labels = [{"name": "bug"}, {"name": "question"}, {"name": "enhancement"}]
        self.reviews: dict[int, list[dict]] = {}
        self.pr_files: dict[int, list[dict]] = {}
        self.search: list[dict] = []
        self.ext_repos: dict[str, dict] = {}
        self.home_issues: list[dict] = []
        self.files = {"README.md": "# App\n\n## Install\n\nRun `pip install app` then `app serve --port 8600`.\n",
                      "docs/FAQ.md": "# FAQ\n\n## Change the port\n\nSet APP_PORT=9000 in .env to change the port.\n"}

    def handler(self, req: httpx.Request) -> httpx.Response:
        path, method = req.url.path, req.method
        if method != "GET" and path != "/graphql":
            self.writes.append((method, path, json.loads(req.content or b"{}")))
            return httpx.Response(201, json={"ok": True, "number": 99})
        if path == "/graphql":
            body = json.loads(req.content)
            if "mutation" in body["query"]:
                self.writes.append(("GRAPHQL", "addDiscussionComment", body["variables"]))
                return httpx.Response(200, json={"data": {"addDiscussionComment": {"comment": {"id": "c"}}}})
            return httpx.Response(200, json={"data": {"repository": {
                "hasDiscussionsEnabled": True, "discussions": {"nodes": self.discussions}}}})
        if path == "/installation/repositories":
            return httpx.Response(200, json={"repositories": [self.repo, {**self.repo, "name": "owner",
                                                                          "full_name": "owner/owner"}]})
        if path == "/repos/owner/app/labels":
            return httpx.Response(200, json=self.labels)
        if path == "/repos/owner/app/issues":
            return httpx.Response(200, json=self.issues)
        if path.startswith("/repos/owner/app/issues/") and path.endswith("/comments"):
            return httpx.Response(200, json=self.comments.get(int(path.split("/")[-2]), []))
        if path == "/repos/owner/app/pulls":
            return httpx.Response(200, json=self.pulls)
        if path.startswith("/repos/owner/app/pulls/") and path.endswith("/reviews"):
            return httpx.Response(200, json=self.reviews.get(int(path.split("/")[-2]), []))
        if path.startswith("/repos/owner/app/pulls/") and path.endswith("/files"):
            return httpx.Response(200, json=self.pr_files.get(int(path.split("/")[-2]), []))
        if path.startswith("/repos/owner/app/pulls/"):
            return httpx.Response(200, json=self.pull_detail[int(path.rsplit("/", 1)[1])])
        if path == "/repos/owner/app/commits/main":
            return httpx.Response(200, json={"sha": "headsha0000000"})
        if path == "/search/issues":
            return httpx.Response(200, json={"items": self.search})
        if path in {f"/repos/{k}" for k in self.ext_repos}:
            return httpx.Response(200, json=self.ext_repos[path[len("/repos/"):]])
        if path.startswith("/repos/") and "/git/trees/" in path and not path.startswith("/repos/owner/app/"):
            return httpx.Response(200, json={"tree": [{"path": "src/parser.py", "type": "blob", "size": 900},
                                                      {"path": "README.md", "type": "blob", "size": 300}]})
        if path.endswith("/check-runs"):
            return httpx.Response(200, json={"check_runs": self.checks.get(path.split("/")[-2], [])})
        if path.endswith("/status"):
            return httpx.Response(200, json={"total_count": 0, "state": "pending"})
        if path == "/repos/owner/app/git/trees/main":
            paths = [*self.files, "src/x.py"]
            return httpx.Response(200, json={"tree": [{"path": p, "type": "blob", "size": len(self.files.get(p, "x" * 50))}
                                                      for p in paths]})
        if path.startswith("/repos/owner/app/contents/"):
            p = path.split("/contents/", 1)[1]
            return httpx.Response(200, text=self.files[p]) if p in self.files else httpx.Response(404)
        if path == "/repos/owner/nexonic-sentinel/issues":
            return httpx.Response(200, json=self.home_issues)
        return httpx.Response(404, json={"message": f"not faked: {path}"})


def make(fake: FakeGitHub, llm=None, dry=False) -> Sentinel:
    return Sentinel(GitHub("t", transport=httpx.MockTransport(fake.handler)), CFG, llm=llm, dry_run=dry, now=NOW)


def issue(n, title, body="", login="alice", created=RECENT, labels=()):
    return {"number": n, "title": title, "body": body, "user": {"login": login, "type": "User"},
            "created_at": created, "labels": [{"name": x} for x in labels]}


# ------------------------------------------------------------------ issues
def test_new_issue_gets_labels_and_one_triage_comment():
    fake = FakeGitHub()
    fake.issues = [issue(1, "App crashes when I change the port", "Traceback ...")]
    make(fake).run()
    assert ("POST", "/repos/owner/app/issues/1/labels", {"labels": ["bug"]}) in fake.writes
    comments = [w for w in fake.writes if w[1].endswith("/1/comments")]
    assert len(comments) == 1
    body = comments[0][2]["body"]
    assert marker("triage") in body and "@alice" in body and "the maintainer" in body.lower() and "docs/FAQ.md" in body
    assert "@owner" not in body  # no notification email to the owner by default
    assert "steps" in body  # short bug report: asks for reproduction details


def test_issue_skipped_when_already_triaged_or_owner_or_bot_or_old_or_bot_off():
    fake = FakeGitHub()
    fake.issues = [issue(1, "bug"), issue(2, "mine", login="owner"), issue(3, "auto", login="renovate[bot]"),
                   issue(4, "old bug", created=OLD), issue(5, "bug", labels=["bot-off"])]
    fake.comments[1] = [{"body": marker("triage")}]
    make(fake).run()
    assert fake.writes == []


def test_security_issue_points_to_private_reporting():
    fake = FakeGitHub()
    fake.issues = [issue(7, "Possible security vulnerability in login")]
    rep = make(fake).run()
    body = [w for w in fake.writes if w[1].endswith("/7/comments")][0][2]["body"]
    assert "private vulnerability reporting" in body and any("security" in f for f in rep.flagged)


def test_dry_run_writes_nothing():
    fake = FakeGitHub()
    fake.issues = [issue(1, "App crashes")]
    rep = make(fake, dry=True).run()
    assert fake.writes == [] and all(a.startswith("[dry run]") for a in rep.actions)


# ------------------------------------------------------------------ pull requests
def pr(n, title, login="dependabot[bot]", assoc="NONE", sha="abc"):
    return {"number": n, "title": title, "user": {"login": login, "type": "Bot" if "[bot]" in login else "User"},
            "author_association": assoc, "created_at": RECENT, "head": {"sha": sha}, "labels": [], "draft": False}


@pytest.mark.parametrize("title, kind", [
    ("Bump next from 16.4.0 to 16.4.1 in /apps/web", "patch"),
    ("Bump vite from 6.4.5 to 6.5.0", "minor"),
    ("Bump vite from 6.4.5 to 7.0.0", "major"),
    ("Bump x from 0.3.1 to 0.4.0", "major"),
    ("Bump actions/checkout from 4 to 7", "major"),
    ("Bump actions/cache from 4.1 to 4.2", "minor"),
    ("Update some things", None),
])
def test_bump_kind(title, kind):
    assert bump_kind(title) == kind


def test_green_patch_update_is_merged():
    fake = FakeGitHub()
    fake.pulls = [pr(13, "Bump next from 16.4.0 to 16.4.1")]
    fake.pull_detail[13] = {"mergeable": True, "mergeable_state": "clean"}
    fake.checks["abc"] = [{"status": "completed", "conclusion": "success"}]
    make(fake).run()
    assert ("PUT", "/repos/owner/app/pulls/13/merge", {"merge_method": "squash", "sha": "abc"}) in fake.writes


def test_update_with_failing_or_running_checks_is_not_merged():
    fake = FakeGitHub()
    fake.pulls = [pr(13, "Bump next from 16.4.0 to 16.4.1")]
    fake.pull_detail[13] = {"mergeable": True, "mergeable_state": "blocked"}
    fake.checks["abc"] = [{"status": "completed", "conclusion": "failure"}]
    make(fake).run()
    assert not any(w[1].endswith("/merge") for w in fake.writes)
    assert any(w[2].get("body", "").startswith("@dependabot rebase") for w in fake.writes)  # asks once to rebase
    fake.writes.clear()
    fake.checks["abc"] = [{"status": "in_progress", "conclusion": None}]
    make(fake).run()
    assert fake.writes == []


def test_major_update_waits_for_owner():
    fake = FakeGitHub()
    fake.pulls = [pr(14, "Bump vite from 6.4.5 to 7.0.0")]
    rep = make(fake).run()
    body = [w for w in fake.writes if w[1].endswith("/14/comments")][0][2]["body"]
    assert "major" in body and "@owner" not in body and not any(w[1].endswith("/merge") for w in fake.writes)
    assert any("#14" in f for f in rep.flagged)


def test_first_time_contributor_is_welcomed_once():
    fake = FakeGitHub()
    fake.pulls = [pr(9, "Add Hindi translation", login="newbie", assoc="FIRST_TIME_CONTRIBUTOR")]
    make(fake).run()
    assert len([w for w in fake.writes if w[1].endswith("/9/comments")]) == 1
    fake.writes.clear()
    fake.comments[9] = [{"body": marker("welcome")}]
    make(fake).run()
    assert fake.writes == []


# ------------------------------------------------------------------ discussions
def disc(n, title, body="", answerable=True, comments=(), login="bob", category="Q&A"):
    return {"id": f"D{n}", "number": n, "title": title, "body": body, "createdAt": RECENT, "url": "u",
            "isAnswered": False, "locked": False, "author": {"login": login, "__typename": "User"},
            "category": {"name": category, "isAnswerable": answerable}, "comments": {"nodes": list(comments)}}


def fake_llm(reply, prompts=None):
    """A model that always returns `reply` (a dict, or a list of dicts used in turn)."""
    replies = reply if isinstance(reply, list) else None

    def handler(req):
        body = json.loads(req.content)
        if prompts is not None:
            prompts.append(body["messages"][1]["content"])
        out = replies.pop(0) if replies else reply
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(out)}}]})
    return OpenRouter("k", ["m1"], transport=httpx.MockTransport(handler))


def test_question_answered_from_docs_with_sources():
    fake = FakeGitHub()
    fake.discussions = [disc(4, "How do I change the port?")]
    llm = fake_llm({"confident": True, "answer": "Set `APP_PORT=9000` in `.env`, then restart.",
                    "sources": ["docs/FAQ.md", "made/up.md"]})
    make(fake, llm=llm).run()
    (_, _, v), = [w for w in fake.writes if w[0] == "GRAPHQL"]
    assert v["id"] == "D4" and "APP_PORT=9000" in v["body"] and "docs/FAQ.md" in v["body"]
    assert "made/up.md" not in v["body"] and "mark it as the answer" in v["body"]


def test_unsure_answer_flags_the_owner_instead():
    fake = FakeGitHub()
    fake.discussions = [disc(5, "How do I change the port?")]
    rep = make(fake, llm=fake_llm({"confident": False, "answer": "", "sources": []})).run()
    (_, _, v), = [w for w in fake.writes if w[0] == "GRAPHQL"]
    assert "flagged this for the maintainer" in v["body"] and any("#5" in f for f in rep.flagged)


def test_discussions_skipped_when_owner_replied_or_already_handled_or_sensitive_not_sent_to_llm():
    fake = FakeGitHub()
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(500)

    fake.discussions = [disc(1, "q", comments=[{"body": "hi", "author": {"login": "owner"}}]),
                        disc(2, "q", comments=[{"body": marker("discussion"), "author": {"login": "x"}}]),
                        disc(3, "Is there a security vulnerability in the token handling?")]
    make(fake, llm=OpenRouter("k", ["m"], transport=httpx.MockTransport(handler))).run()
    graph = [w for w in fake.writes if w[0] == "GRAPHQL"]
    assert len(graph) == 1 and graph[0][2]["id"] == "D3" and "personally" in graph[0][2]["body"]
    assert calls == []


def test_llm_cap_per_run():
    fake = FakeGitHub()
    fake.discussions = [disc(i, "How do I change the port?") for i in range(1, 6)]
    llm = fake_llm({"confident": True, "answer": "Set APP_PORT=9000 in .env and restart.", "sources": ["docs/FAQ.md"]})
    llm.budget = 3  # the run's AI budget (set from max_llm_calls_per_run and the daily allowance)
    make(fake, llm=llm).run()
    assert llm.calls == 3
    bodies = [w[2]["body"] for w in fake.writes if w[0] == "GRAPHQL"]
    assert sum("APP_PORT" in b for b in bodies) == 3 and sum("flagged" in b for b in bodies) == 2


def test_disabled_config_does_nothing():
    fake = FakeGitHub()
    fake.issues = [issue(1, "bug")]
    s = Sentinel(GitHub("t", transport=httpx.MockTransport(fake.handler)), {**CFG, "enabled": False}, now=NOW)
    s.run()
    assert fake.writes == []


def test_digest_issue():
    fake = FakeGitHub()
    s = make(fake)
    s.run()
    s.digest()
    (method, path, body), = [w for w in fake.writes if w[1] == "/repos/owner/nexonic-sentinel/issues"]
    assert body["title"] == "Weekly digest 2026-W44" and "| [app]" in body["body"] and body["labels"] == ["digest"]


# ------------------------------------------------------------------ helpers
def test_parse_reply_requires_known_sources_and_real_text():
    assert parse_reply('```json\n{"confident": true, "answer": "Use the --port flag to choose a port.", '
                       '"sources": ["README.md"]}\n```', {"README.md"})["confident"]
    assert not parse_reply('{"confident": true, "answer": "Yes.", "sources": ["README.md"]}', {"README.md"})["confident"]
    assert not parse_reply('{"confident": true, "answer": "Use the --port flag please.", "sources": ["x.md"]}',
                           {"README.md"})["confident"]


def test_knowledge_picks_relevant_doc():
    docs = {"README.md": "# App\nInstall with pip.", "docs/FAQ.md": "## Change the port\nSet APP_PORT in .env"}
    assert best_excerpts("how to change the port", docs)[0][0] == "docs/FAQ.md"
    assert doc_paths(["README.md", "src/a.py", "docs/x.md", "SECURITY.md"]) == ["README.md", "docs/x.md", "SECURITY.md"]


def test_owner_can_trigger_the_bot_with_the_test_tag():
    fake = FakeGitHub()
    fake.issues = [issue(1, "[sentinel-test] App crashes on start", login="owner"), issue(2, "crash", login="owner")]
    fake.discussions = [disc(3, "[sentinel-test] How do I change the port?", login="owner")]
    make(fake).run()
    assert any(w[1].endswith("/1/comments") for w in fake.writes)
    assert not any(w[1].endswith("/2/comments") for w in fake.writes)
    assert [w[2]["id"] for w in fake.writes if w[0] == "GRAPHQL"] == ["D3"]


GROUPED = ("Bumps react and @types/react. These dependencies needed to be updated together.\n"
           "Updates `react` from 19.0.0 to 19.3.1\n- [Release notes](https://example.com)\n"
           "Updates `@types/react` from 19.0.3 to 19.3.0\n\nRelease notes\nfrom 1.0.0 to 9.0.0 in some changelog text")


def test_grouped_update_reads_versions_from_the_body():
    assert bump_kind("Bump react and @types/react", GROUPED) == "minor"
    assert bump_kind("Bump the npm group with 2 updates",
                     "Updates `a` from 1.2.3 to 1.2.4\nUpdates `b` from 2.0.0 to 3.0.0") == "major"
    assert bump_kind("Bump things", "no versions here") is None


def test_green_grouped_minor_update_is_merged():
    fake = FakeGitHub()
    fake.pulls = [{**pr(5, "Bump react and @types/react"), "body": GROUPED}]
    fake.pull_detail[5] = {"mergeable": True, "mergeable_state": "clean"}
    fake.checks["abc"] = [{"status": "completed", "conclusion": "success"}]
    make(fake).run()
    assert ("PUT", "/repos/owner/app/pulls/5/merge", {"merge_method": "squash", "sha": "abc"}) in fake.writes


def test_owner_mention_is_opt_in():
    fake = FakeGitHub()
    fake.issues = [issue(1, "App crashes")]
    Sentinel(GitHub("t", transport=httpx.MockTransport(fake.handler)), {**CFG, "mention_owner": True}, now=NOW).run()
    body = [w for w in fake.writes if w[1].endswith("/1/comments")][0][2]["body"]
    assert "@owner will take a look" in body


def test_welcome_skips_testing_reminder_when_pr_describes_tests():
    fake = FakeGitHub()
    fake.pulls = [{**pr(9, "Add theme", login="newbie", assoc="FIRST_TIME_CONTRIBUTOR"), "body": "Testing: npm test passed"},
                  {**pr(10, "Fix typo", login="other", assoc="FIRST_TIME_CONTRIBUTOR"), "body": "Small fix"}]
    make(fake).run()
    b9 = [w for w in fake.writes if w[1].endswith("/9/comments")][0][2]["body"]
    b10 = [w for w in fake.writes if w[1].endswith("/10/comments")][0][2]["body"]
    assert "describe how you tested" not in b9 and "describe how you tested" in b10 and "@owner" not in b9
