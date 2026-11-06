"""Proposals: the bot prepares changes as pull requests for the owner, and never merges them."""

from __future__ import annotations

import base64

import httpx

from sentinel import proposals
from sentinel.bot import Sentinel
from sentinel.github import GitHub
from tests.test_bot import CFG, NOW, FakeGitHub, fake_llm
from tests.test_replies import MemLedger, note, thread

FAQ = "# FAQ\n\n## Change the port\n\nSet APP_PORT=9000 in .env to change the port.\n"


class RepoFake(FakeGitHub):
    """Adds the endpoints used to open a pull request."""

    def __init__(self) -> None:
        super().__init__()
        self.pr_state: dict[int, dict] = {}

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/owner/app/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": "base000"}})
        if req.method == "GET" and path.startswith("/repos/owner/app/contents/") and "ref" in req.url.params:
            return httpx.Response(200, json={"sha": "filesha"})
        if req.method == "GET" and path.startswith("/repos/owner/app/pulls/") and path.rsplit("/", 1)[1].isdigit():
            return httpx.Response(200, json=self.pr_state.get(int(path.rsplit("/", 1)[1]), {"state": "open"}))
        return super().handler(req)


def make(fake, llm=None, ledger=None):
    return Sentinel(GitHub("t", transport=httpx.MockTransport(fake.handler)), CFG, llm=llm, now=NOW, ledger=ledger)


REPLY = {"kind": "request", "reply": "Thanks, that's a useful correction.", "needs_owner": True,
         "decision": "Fix the FAQ port text?"}
PLAN = {"action": "change", "files": ["docs/FAQ.md"], "summary": "Correct the port setting name in the FAQ",
        "reason": "The docs name the wrong variable"}
EDIT = {"confident": True, "title": "Fix the port setting name in the FAQ", "explanation": "APP_PORT is wrong.",
        "edits": [{"file": "docs/FAQ.md", "start": 5, "end": 5, "old": "Set APP_PORT=9000 in .env to change the port.",
                   "new": "Set NEXONIC_PORT=9000 in .env to change the port."}]}


def test_request_becomes_a_pull_request_waiting_for_the_owner():
    fake = RepoFake()
    fake.files["docs/FAQ.md"] = FAQ
    thread(fake, comments=[{**note(7, "The FAQ says APP_PORT but it's NEXONIC_PORT now."), "user": {
        "login": "leon", "type": "User", "id": 42}}])
    ledger = MemLedger()
    bot = make(fake, fake_llm([REPLY, PLAN, EDIT]), ledger)
    bot.run()
    writes = {(m, p) for m, p, _ in fake.writes}
    assert ("POST", "/repos/owner/app/git/refs") in writes and ("POST", "/repos/owner/app/pulls") in writes
    (put,) = [b for m, p, b in fake.writes if m == "PUT" and p == "/repos/owner/app/contents/docs/FAQ.md"]
    assert "NEXONIC_PORT" in base64.b64decode(put["content"]).decode()
    assert "Co-authored-by: leon <42+leon@users.noreply.github.com>" in put["message"]
    assert ("POST", "/repos/owner/app/issues/99/labels", {"labels": ["needs-approval"]}) in fake.writes
    (pr,) = [b for m, p, b in fake.writes if p == "/repos/owner/app/pulls"]
    assert "merge this pull request to approve" in pr["body"] and "never merge" in pr["body"]
    reply = [b for m, p, b in fake.writes if p == "/repos/owner/app/issues/48/comments"][0]["body"]
    assert "for the maintainer to approve: #99" in reply
    assert [w["kind"] for w in bot.waiting] == ["pr"] and ledger.data["proposals"][0]["pr"] == 99
    assert not any("/merge" in p for _, p, _ in fake.writes)  # never merges


def test_forbidden_files_and_bad_edits_are_never_proposed():
    fake = RepoFake()
    fake.files["docs/FAQ.md"] = FAQ
    thread(fake, comments=[note(7, "Please change the CI workflow")])
    plan = {**PLAN, "files": [".github/workflows/ci.yml", "LICENSE"]}
    bot = make(fake, fake_llm([REPLY, plan]))
    bot.run()
    assert not any(p.endswith("/pulls") for _, p, _ in fake.writes) and bot.waiting[0].get("kind") != "pr"
    fake = RepoFake()
    fake.files["docs/FAQ.md"] = FAQ
    thread(fake, comments=[note(7, "Fix the FAQ")])
    wrong = {**EDIT, "edits": [{**EDIT["edits"][0], "old": "text that is not in the file"}]}
    make(fake, fake_llm([REPLY, PLAN, wrong])).run()
    assert not any(p.endswith("/pulls") for _, p, _ in fake.writes)


def test_missing_information_gets_questions_and_no_owner_item():
    fake = RepoFake()
    thread(fake, comments=[note(7, "It crashes")])
    ask = {"action": "ask", "questions": ["Which version are you using?", "What were you doing when it crashed?"]}
    bot = make(fake, fake_llm([{**REPLY, "kind": "problem"}, ask]))
    bot.run()
    reply = [b for m, p, b in fake.writes if p == "/repos/owner/app/issues/48/comments"][0]["body"]
    assert "- Which version are you using?" in reply and not bot.waiting


def test_owner_decision_is_reported_back_to_the_asker():
    fake = RepoFake()
    fake.pr_state = {99: {"state": "closed", "merged": True}, 100: {"state": "closed", "merged": False},
                     101: {"state": "open"}}
    ledger = MemLedger({"proposals": [{"repo": "owner/app", "thread": 48, "pr": 99, "who": "leon"},
                                      {"repo": "owner/app", "thread": 50, "pr": 100, "who": "ana"},
                                      {"repo": "owner/app", "thread": 51, "pr": 101, "who": "bo"}]})
    make(fake, ledger=ledger).proposal_follow_ups()
    texts = {p: b["body"] for m, p, b in fake.writes}
    assert "approved and merged" in texts["/repos/owner/app/issues/48/comments"]
    assert "decided not to make this change" in texts["/repos/owner/app/issues/50/comments"]
    assert [i["pr"] for i in ledger.data["proposals"]] == [101]


def test_candidate_files_prefer_matching_paths_and_skip_vendor():
    entries = [("src/app.py", 500), ("node_modules/x/index.js", 10), ("windows/Core/Locales/es.json", 900),
               ("docs/TRANSLATIONS.md", 800), ("logo.png", 100)]
    files = proposals.candidate_files(entries, "The Spanish es translation is good")
    assert set(files[:2]) == {"windows/Core/Locales/es.json", "docs/TRANSLATIONS.md"}
    assert "node_modules/x/index.js" not in files and "logo.png" not in files
