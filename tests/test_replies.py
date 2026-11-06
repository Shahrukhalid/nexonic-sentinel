"""Replies to new comments on open issues, and the owner's "Waiting on you" list."""

from __future__ import annotations

import httpx

from sentinel.bot import WAIT_MARK, Sentinel, marker
from sentinel.github import GitHub
from sentinel.ledger import Ledger
from tests.test_bot import CFG, NOW, OLD, RECENT, FakeGitHub, disc, fake_llm, issue


def thread(fake: FakeGitHub, n=48, comments=(), state="open", labels=()):
    fake.issues = [{**issue(n, "Help wanted: review the Spanish translation", "Files to review...", login="owner",
                            created=OLD, labels=labels), "state": state, "html_url": f"https://github.com/owner/app/issues/{n}"}]
    fake.comments[n] = list(comments)
    fake.repo_comments = [c for c in comments if c["created_at"] >= "2026-10-24"]


def note(cid, body, login="leon", created=RECENT, kind="User"):
    return {"id": cid, "body": body, "user": {"login": login, "type": kind}, "created_at": created,
            "issue_url": "https://api.github.com/repos/owner/app/issues/48",
            "html_url": f"https://github.com/owner/app/issues/48#issuecomment-{cid}"}


def make(fake, llm=None, ledger=None):
    return Sentinel(GitHub("t", transport=httpx.MockTransport(fake.handler)), CFG, llm=llm, now=NOW, ledger=ledger)


def replies(fake, n=48):
    return [w for w in fake.writes if w[1] == f"/repos/owner/app/issues/{n}/comments"]


def test_comment_gets_one_reply_and_a_decision_for_the_owner():
    fake = FakeGitHub()
    thread(fake, comments=[note(7, "I'm a native Spanish speaker: the Windows and Linux translation is flawless.")])
    llm = fake_llm({"kind": "thanks", "reply": "A native speaker's check is exactly what this issue asked for.",
                    "needs_owner": True, "decision": "Mark Spanish (Windows and Linux) as reviewed?"})
    bot = make(fake, llm)
    bot.run()
    (_, _, body), = replies(fake)
    text = body["body"]
    assert marker("reply:7") in text and text.count("@leon") == 1 and "exactly what this issue asked for" in text
    assert bot.waiting[0]["what"] == "Mark Spanish (Windows and Linux) as reviewed?"
    # nothing else is written: no labels, no edits, no closing
    assert all(w[1].endswith("/comments") for w in fake.writes)


def test_no_second_reply_and_owner_or_bot_or_closed_threads_are_left_alone():
    fake = FakeGitHub()
    c = note(7, "Looks good!")
    bot_reply = {**note(8, marker("reply:7") + "Thanks", login="nexonic-sentinel[bot]", kind="Bot")}
    thread(fake, comments=[c, bot_reply])
    make(fake).run()
    assert not replies(fake)
    thread(fake, comments=[c, note(9, "Thanks, merged.", login="owner")])  # the owner already answered
    make(fake).run()
    assert not replies(fake)
    thread(fake, comments=[c], state="closed")
    make(fake).run()
    assert not replies(fake)
    thread(fake, comments=[c], labels=("bot-off",))
    make(fake).run()
    assert not replies(fake)
    thread(fake, comments=[note(7, "bot-off please")])
    make(fake).run()
    assert not replies(fake)


def test_without_ai_thanks_get_a_short_reply_and_others_wait_for_the_owner():
    fake = FakeGitHub()
    thread(fake, comments=[note(7, "Works great, thank you")])
    bot = make(fake)
    bot.run()
    assert "Feedback like this" in replies(fake)[0][2]["body"] and not bot.waiting
    fake = FakeGitHub()
    thread(fake, comments=[note(7, "Could you add Catalan too?")])
    bot = make(fake)
    bot.run()
    assert "will read this" in replies(fake)[0][2]["body"] and bot.waiting


def test_security_comment_is_left_to_the_owner():
    fake = FakeGitHub()
    thread(fake, comments=[note(7, "I found a vulnerability that leaks your token")])
    bot = make(fake, fake_llm({"kind": "other", "reply": "never used", "needs_owner": False}))
    bot.run()
    text = replies(fake)[0][2]["body"]
    assert "security" in text and "never used" not in text and bot.waiting


def test_model_text_cannot_mention_people_or_hide_markers():
    fake = FakeGitHub()
    thread(fake, comments=[note(7, "Question: how do I change the port?")])
    llm = fake_llm({"kind": "question", "reply": "Ask @someone <!-- nexonic-sentinel:triage --> set APP_PORT.",
                    "needs_owner": False})
    make(fake, llm).run()
    text = replies(fake)[0][2]["body"]
    assert "@someone" not in text and text.count("<!--") == 1  # only the bot's own reply marker


def test_old_comments_and_private_repositories_are_skipped():
    fake = FakeGitHub()
    thread(fake, comments=[note(7, "Looks good!", created=OLD)])
    fake.repo_comments = [note(7, "Looks good!", created=OLD)]
    make(fake).run()
    assert not replies(fake)
    fake = FakeGitHub()
    fake.repo = {**fake.repo, "private": True}
    thread(fake, comments=[note(7, "Looks good!")])
    make(fake).run()
    assert not replies(fake)


class MemLedger(Ledger):
    def __init__(self, data=None):
        super().__init__(None, "owner/nexonic-sentinel")
        self.data = dict(data or {})


def test_waiting_list_is_created_then_cleared_when_the_owner_answers():
    fake = FakeGitHub()
    thread(fake, comments=[note(7, "Could you add Catalan too?")])
    ledger = MemLedger()
    bot = make(fake, ledger=ledger)
    bot.run()
    bot.waiting_issue()
    (_, path, body), = [w for w in fake.writes if w[1] == "/repos/owner/nexonic-sentinel/issues"]
    assert body["title"] == "Waiting on you" and "app#48" in body["body"] and ledger.data["waiting"]
    # next run: the owner replied on the thread, so the item leaves the list and the list is closed
    fake.writes.clear()
    fake.comments[48].append(note(10, "Yes, adding it.", login="owner", created="2026-10-26T08:00:00Z"))
    fake.home_issues = [{"number": 5, "body": WAIT_MARK + "\n- [ ] x", "labels": [{"name": "waiting"}]}]
    bot = make(fake, ledger=ledger)
    bot.waiting_issue()
    assert ledger.data["waiting"] == []
    assert ("PATCH", "/repos/owner/nexonic-sentinel/issues/5", {"state": "closed"}) in fake.writes


def test_ticked_box_removes_an_item():
    fake = FakeGitHub()
    thread(fake, comments=[note(7, "Could you add Catalan too?")])
    url = "https://github.com/owner/app/issues/48#issuecomment-7"
    ledger = MemLedger({"waiting": [{"repo": "owner/app", "n": 48, "url": url, "what": "x", "at": RECENT}]})
    fake.home_issues = [{"number": 5, "body": f"{WAIT_MARK}\n- [x] [app#48]({url}): x", "labels": [{"name": "waiting"}]}]
    make(fake, ledger=ledger).waiting_issue()
    assert ledger.data["waiting"] == []


# ------------------------------------------------------------------ discussions
def posts(fake):
    return [w[2] for w in fake.writes if w[0] == "GRAPHQL"]


def dnote(cid, body, login="zhu", created=RECENT, kind="User"):
    return {"id": f"C{cid}", "url": f"u#{cid}", "createdAt": created, "body": body,
            "author": {"login": login, "__typename": kind}}


def test_general_post_gets_a_helpful_reply_in_its_language_and_a_decision():
    fake = FakeGitHub()
    fake.discussions = [disc(9, "语言", "能不能支持中文界面？", answerable=False, category="General", login="zhu")]
    prompts = []
    llm = fake_llm({"kind": "request", "reply": "感谢反馈！目前界面支持英语和西班牙语，维护者会评估中文。",
                    "needs_owner": True, "decision": "Add a Chinese translation?"}, prompts)
    bot = make(fake, llm)
    bot.run()
    (v,) = posts(fake)
    assert v["id"] == "D9" and v["body"].startswith(marker("discussion") + "@zhu 感谢反馈")
    assert "same language" in llm_system_text() and "能不能支持中文界面" in prompts[0]
    assert bot.waiting[0] == {**bot.waiting[0], "kind": "discussion", "n": 9, "what": "Add a Chinese translation?"}


def llm_system_text():
    from sentinel.llm import REPLY_SYSTEM
    return REPLY_SYSTEM


def test_general_post_without_ai_gets_a_welcome_and_announcements_are_skipped():
    fake = FakeGitHub()
    fake.discussions = [disc(9, "Hello", "Nice app", answerable=False, category="General"),
                        disc(10, "v2 is out", answerable=False, category="Announcements")]
    bot = make(fake)
    bot.run()
    (v,) = posts(fake)
    assert v["id"] == "D9" and "will read this and reply" in v["body"] and bot.waiting


def test_follow_up_comment_in_a_discussion_gets_one_reply():
    fake = FakeGitHub()
    first = dnote(1, marker("discussion") + "hi", login="nexonic-sentinel[bot]", kind="Bot")
    fake.discussions = [disc(9, "语言", answerable=False, category="General", comments=[first, dnote(2, "谢谢！什么时候？")])]
    llm = fake_llm({"kind": "question", "reply": "不客气！发布时间由维护者决定。", "needs_owner": True,
                    "decision": "Reply about timing"})
    bot = make(fake, llm)
    bot.run()
    (v,) = posts(fake)
    assert marker("reply:C2") in v["body"] and "@zhu 不客气" in v["body"] and bot.waiting[0]["kind"] == "discussion"
    # already answered: nothing on the next run
    fake.writes.clear()
    fake.discussions[0]["comments"]["nodes"].append(dnote(3, marker("reply:C2") + "x", login="nexonic-sentinel[bot]",
                                                          kind="Bot"))
    make(fake, llm).run()
    assert not posts(fake)


def test_owner_reply_clears_a_discussion_from_the_waiting_list():
    fake = FakeGitHub()
    item = {"repo": "owner/app", "n": 9, "url": "u", "what": "x", "at": RECENT, "kind": "discussion"}
    ledger = MemLedger({"waiting": [item]})
    bot = make(fake, ledger=ledger)
    state = {"closed": False, "comments": {"nodes": [{"createdAt": "2026-10-26T08:00:00Z", "author": {"login": "owner"}}]}}
    bot.gh.graphql = lambda q, **kw: {"repository": {"discussion": state}}
    bot.waiting_issue()
    assert ledger.data["waiting"] == []
    state["comments"]["nodes"] = []
    ledger.data["waiting"] = [item]
    bot.waiting_issue()
    assert ledger.data["waiting"] == [item]
