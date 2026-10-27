from __future__ import annotations

import re
from datetime import timedelta

import pytest

from sentinel import growth
from sentinel.bot import Sentinel
from sentinel.ledger import key
from tests.test_bot import CFG, NOW, RECENT, FakeGitHub, fake_llm
from tests.test_features import DRAFT, GOOD_FIX, MemLedger, contrib_setup, gh

OLD = "2026-08-01T10:00:00Z"
URL = "https://github.com/acme/tool/issues/5"
TL = "/repos/acme/tool/issues/5/timeline"
CM = "/repos/acme/tool/issues/5/comments"
BANNED = re.compile(r"claude|anthropic|copilot|\bAI\b|\bLLM\b|openrouter|gpt|gemini|model", re.I)


def run_scout(fake, cfg=CFG, llm=None):
    bot = Sentinel(gh(fake), cfg, llm=llm, now=NOW, ledger=MemLedger())
    bot.growth()
    return [w for w in fake.writes if w[0] == "POST"]


def issue_posts(fake):
    return [w for w in fake.writes if w[1] == "/repos/owner/nexonic-sentinel/issues"]


def foreign_writes(fake):
    return [w for w in fake.writes if not w[1].startswith("/repos/owner/")]


# ------------------------------------------------------------------ scout filters
@pytest.mark.parametrize("change", [
    lambda f: f.search[0].update(assignee={"login": "bob"}),
    lambda f: f.search[0].update(updated_at=OLD),
    lambda f: f.ext_repos["acme/tool"].update(archived=True),
    lambda f: f.ext_repos["acme/tool"].update(stargazers_count=5),
    lambda f: f.ext_repos["acme/tool"].update(pushed_at=OLD),
    lambda f: f.ext_repos["acme/tool"].update(fork=True),
    lambda f: f.ext_timeline.update({TL: [{"event": "cross-referenced",
                                           "source": {"issue": {"pull_request": {}, "state": "open"}}}]}),
    lambda f: f.ext_comments.update({CM: [{"body": "I'm working on this", "created_at": RECENT,
                                           "user": {"login": "bob", "type": "User"}}]}),
    lambda f: f.ext_comments.update({CM: [{"body": "Can I take this one?", "created_at": RECENT,
                                           "user": {"login": "bob", "type": "User"}}]}),
])
def test_scout_skips_taken_stale_or_inactive(change):
    fake = contrib_setup()
    change(fake)
    assert run_scout(fake, llm=fake_llm([DRAFT, GOOD_FIX])) == []


def test_scout_keeps_closed_prs_and_old_claims_and_other_languages_ignored():
    fake = contrib_setup()
    fake.ext_timeline[TL] = [{"event": "cross-referenced", "source": {"issue": {"pull_request": {}, "state": "closed"}}}]
    fake.ext_comments[CM] = [{"body": "I'm working on this", "created_at": OLD, "user": {"login": "b", "type": "User"}},
                             {"body": "thanks, nice catch", "created_at": RECENT, "user": {"login": "c", "type": "User"}}]
    assert len(run_scout(fake, llm=fake_llm([DRAFT, GOOD_FIX]))) == 1


def test_scout_ranks_by_freshness_and_activity_and_caps_picks():
    from datetime import datetime

    from sentinel.contrib import score
    repo = {"stargazers_count": 500, "pushed_at": RECENT}
    fresh = {"updated_at": RECENT, "comments": 0, "labels": [{"name": "good first issue"}], "body": "short"}
    stale = {"updated_at": "2026-10-13T10:00:00Z", "comments": 5, "labels": [{"name": "bug"}], "body": "x" * 2000}
    now = datetime.fromisoformat(NOW.isoformat())
    assert score(repo, fresh, now) > score(repo, stale, now)
    fake = contrib_setup()
    fake.search = [{**fake.search[0], "html_url": f"https://github.com/acme/tool/issues/{n}", "number": n}
                   for n in range(5, 12)]
    body = run_scout(fake, {**CFG, "contrib": {"max_items": 2}})[0][2]["body"]
    assert body.count("#### ") == 2


# ------------------------------------------------------------------ backlinks
LIST_README = """# Awesome Fastapi

## Contents
- [Tools](#tools)

## Tools
- [alpha](https://github.com/x/alpha) - A fast thing.
- [gamma](https://github.com/x/gamma) - Another thing.

## Videos
- [talk](https://youtu.be/x) - A talk.
"""


def backlink_setup(readme=LIST_README):
    fake = FakeGitHub()
    fake.repo.update(topics=["fastapi"], html_url="https://github.com/owner/app", description="Tiny fastapi starter",
                     private=False)
    fake.ext_search_repos = [{"full_name": "acme/awesome-fastapi", "name": "awesome-fastapi", "archived": False,
                              "fork": False, "stargazers_count": 900, "default_branch": "main", "topics": []},
                             {"full_name": "owner/awesome-mine", "name": "awesome-mine", "archived": False,
                              "fork": False, "stargazers_count": 90, "default_branch": "main", "topics": []},
                             {"full_name": "acme/not-a-list", "name": "framework", "archived": False,
                              "fork": False, "stargazers_count": 90, "default_branch": "main", "topics": []}]
    fake.ext_readmes["acme/awesome-fastapi"] = readme
    return fake


CFG_B = {**CFG, "contrib": {"enabled": False}, "backlinks": {"enabled": True}}


def run_back(fake, llm=None, ledger=None, now=NOW):
    led = ledger or MemLedger()
    Sentinel(gh(fake), CFG_B, llm=llm, now=now, ledger=led).growth()
    return led


def test_backlink_entry_copies_list_format_and_section():
    fake = backlink_setup()
    led = run_back(fake)
    body = issue_posts(fake)[0][2]["body"]
    assert "acme/awesome-fastapi" in body and "Section: **Tools**" in body
    assert "- [app](https://github.com/owner/app) - Tiny fastapi starter." in body
    assert "alphabetical order, after alpha" in body
    assert "owner/awesome-mine" not in body and "not-a-list" not in body
    ident = key("backlink", "owner/app", "acme/awesome-fastapi")
    assert f"bl-{ident}" in body and led.get("backlinks")[ident]["s"] == "suggested"


def test_backlink_uses_model_entry_only_when_valid():
    fake = backlink_setup()
    good = {"section": "Tools", "entry": "- [app](https://github.com/owner/app) - Tiny FastAPI starter.", "suitable": True}
    run_back(fake, llm=fake_llm(good))
    assert "Tiny FastAPI starter." in issue_posts(fake)[0][2]["body"]
    fake = backlink_setup()
    bad = {"section": "Made up", "entry": "- [app](https://elsewhere)", "suitable": True}
    run_back(fake, llm=fake_llm(bad))
    assert "Tiny fastapi starter." in issue_posts(fake)[0][2]["body"]
    fake = backlink_setup()
    run_back(fake, llm=fake_llm({"suitable": False}))
    assert issue_posts(fake) == []


def test_backlink_skips_lists_that_mention_the_repo_or_refuse_prs():
    fake = backlink_setup(LIST_README + "- [app](https://github.com/Owner/App) - there already\n")
    run_back(fake)
    assert issue_posts(fake) == []
    fake = backlink_setup()
    fake.ext_files["/repos/acme/awesome-fastapi/contents/CONTRIBUTING.md"] = "We are not accepting pull requests."
    run_back(fake)
    assert issue_posts(fake) == []


def test_backlink_not_repeated_and_declined_or_submitted_are_remembered():
    fake = backlink_setup()
    led = run_back(fake)
    ident = key("backlink", "owner/app", "acme/awesome-fastapi")
    # next week: still within the wait, so not suggested again
    fake.writes.clear()
    run_back(fake, ledger=led, now=NOW + timedelta(days=7))
    assert issue_posts(fake) == []
    # the owner (and only the owner) marks it declined in a comment on the inbox
    fake.home_issues = [{"number": 7, "title": "Growth inbox 2026-W44", "state": "open", "comments": 2,
                         "body": "<!-- nexonic-sentinel:growth -->"}]
    fake.ext_comments["/repos/owner/nexonic-sentinel/issues/7/comments"] = [
        {"body": f"declined bl-{ident}", "user": {"login": "stranger"}},
        {"body": f"declined bl-{ident}", "user": {"login": "owner"}}]
    run_back(fake, ledger=led, now=NOW + timedelta(days=100))
    assert led.get("backlinks")[ident]["s"] == "declined"
    fake.writes.clear()
    fake.home_issues = []
    run_back(fake, ledger=led, now=NOW + timedelta(days=200))
    assert issue_posts(fake) == []  # declined stays declined


def test_untracked_or_stranger_commands_are_ignored():
    state = {"abc123abc123": {"s": "suggested", "at": "2026-10-01"}}
    comments = [{"body": "submitted bl-abc123abc123", "user": {"login": "x"}},
                {"body": "submitted bl-ffffffffffff", "user": {"login": "owner"}}]
    assert growth.apply_commands(state, comments, "owner", NOW) == 0
    assert state["abc123abc123"]["s"] == "suggested"


# ------------------------------------------------------------------ discussions
def discussion(**over):
    d = {"id": "D1", "number": 3, "title": "How do I change the port?", "body": "Running tool serve uses 8000.",
         "createdAt": RECENT, "url": "https://github.com/acme/tool/discussions/3", "isAnswered": False,
         "locked": False, "author": {"login": "newbie", "__typename": "User"},
         "category": {"name": "Q&A", "isAnswerable": True},
         "comments": {"totalCount": 0, "nodes": []},
         "repository": {"nameWithOwner": "acme/tool", "stargazerCount": 900, "pushedAt": RECENT, "isArchived": False,
                        "isFork": False, "hasDiscussionsEnabled": True, "defaultBranchRef": {"name": "main"},
                        "owner": {"login": "acme"}}}
    d.update(over)
    return d


CFG_D = {**CFG, "contrib": {"enabled": False}, "answers": {"enabled": True}}


def disc_setup(*items):
    fake = FakeGitHub()
    fake.repo.update(topics=["fastapi"], private=False)
    fake.disc_search = list(items)
    return fake


def run_disc(fake, llm=None):
    Sentinel(gh(fake), CFG_D, llm=llm, now=NOW, ledger=MemLedger()).growth()
    return issue_posts(fake)


def test_discussion_draft_uses_the_projects_docs_and_is_only_a_suggestion():
    fake = disc_setup(discussion())
    fake.ext_files["/repos/acme/tool/contents/README.md"] = "# Tool\n\n## Port\n\nPass `--port 9000` to change the port.\n"
    # the fake tree for foreign repos lists README.md and src/parser.py
    llm = fake_llm({"confident": True, "answer": "Pass `--port 9000` when you start the server.", "sources": ["README.md"]})
    (post,) = run_disc(fake, llm)
    body = post[2]["body"]
    assert "How do I change the port?" in body and "Draft answer" in body and "--port 9000" in body
    assert "https://github.com/acme/tool/blob/main/README.md" in body
    assert foreign_writes(fake) == [] and not any(w[0] == "GRAPHQL" for w in fake.writes)


def test_discussion_without_docs_falls_back_to_a_labelled_draft():
    fake = disc_setup(discussion())
    llm = fake_llm({"confident": True, "answer": "Use the --port flag when starting the server process."})
    (post,) = run_disc(fake, llm)
    assert "from the question only, verify it" in post[2]["body"]


@pytest.mark.parametrize("over", [
    {"isAnswered": True}, {"locked": True}, {"category": {"name": "Ideas", "isAnswerable": False}},
    {"author": {"login": "owner", "__typename": "User"}}, {"author": {"login": "x[bot]", "__typename": "Bot"}},
    {"comments": {"totalCount": 5, "nodes": []}},
    {"comments": {"totalCount": 1, "nodes": [{"author": {"login": "owner"}}]}},
    {"createdAt": OLD}, {"title": "Security leak in token handling"},
    {"repository": {**discussion()["repository"], "stargazerCount": 3}},
    {"repository": {**discussion()["repository"], "isArchived": True}},
    {"repository": {**discussion()["repository"], "pushedAt": OLD}},
    {"repository": {**discussion()["repository"], "owner": {"login": "owner"}}},
])
def test_discussion_filters(over):
    assert run_disc(disc_setup(discussion(**over))) == []


def test_discussion_not_suggested_twice():
    fake = disc_setup(discussion())
    led = MemLedger()
    Sentinel(gh(fake), CFG_D, now=NOW, ledger=led).growth()
    assert len(issue_posts(fake)) == 1
    fake.writes.clear()
    Sentinel(gh(fake), CFG_D, now=NOW + timedelta(days=7), ledger=led).growth()
    assert issue_posts(fake) == []


# ------------------------------------------------------------------ safety
def full_setup():
    fake = contrib_setup()
    fake.repo.update(topics=["fastapi"], html_url="https://github.com/owner/app", description="d", private=False)
    fake.ext_search_repos = backlink_setup().ext_search_repos
    fake.ext_readmes["acme/awesome-fastapi"] = LIST_README
    fake.disc_search = [discussion()]
    return fake


CFG_ALL = {**CFG, "backlinks": {"enabled": True}, "answers": {"enabled": True}}


def test_everything_goes_into_one_inbox_and_nothing_is_written_to_foreign_repos():
    fake = full_setup()
    llm = fake_llm([DRAFT, GOOD_FIX, {"suitable": True, "section": "Tools",
                                       "entry": "- [app](https://github.com/owner/app) - d."},
                    {"confident": True, "answer": "Pass the port flag when starting the server."}])
    Sentinel(gh(fake), CFG_ALL, llm=llm, now=NOW, ledger=MemLedger()).growth()
    assert [w[1] for w in fake.writes] == ["/repos/owner/nexonic-sentinel/issues"]
    body = fake.writes[0][2]["body"]
    assert "## Scout" in body and "## Backlinks" in body and "## Discussions" in body
    assert foreign_writes(fake) == []
    # no GraphQL mutation, comment, reaction or star anywhere
    assert not any(w[0] == "GRAPHQL" or "/reactions" in w[1] or "/user/starred" in w[1] or "/comments" in w[1]
                   for w in fake.writes)


def test_posted_text_never_names_tools_or_models():
    fake = full_setup()
    llm = fake_llm([DRAFT, GOOD_FIX, {"suitable": True, "section": "Tools",
                                       "entry": "- [app](https://github.com/owner/app) - d."},
                    {"confident": True, "answer": "Pass the port flag when starting the server."}])
    Sentinel(gh(fake), CFG_ALL, llm=llm, now=NOW, ledger=MemLedger()).growth()
    posted = " ".join(str(w[2]) for w in fake.writes)
    assert not BANNED.search(posted), BANNED.search(posted)


def test_read_only_wrapper_blocks_writes():
    ro = growth.ReadOnly(gh(FakeGitHub()))
    for name in ("comment", "add_labels", "request", "discussion_comment", "http"):
        with pytest.raises(PermissionError):
            getattr(ro, name)
    with pytest.raises(PermissionError):
        ro.graphql("mutation { addStar(input:{starrableId:\"x\"}) { clientMutationId } }")


def test_kill_switches_and_dry_run():
    fake = full_setup()
    for cfg in ({**CFG_ALL, "enabled": False}, {**CFG_ALL, "growth": {"enabled": False}}):
        Sentinel(gh(fake), cfg, llm=fake_llm(DRAFT), now=NOW, ledger=MemLedger()).growth()
        assert fake.writes == []
    # each feature is off unless switched on; public installs never run it
    only_scout = {**CFG, "contrib": {"enabled": False}}
    Sentinel(gh(fake), only_scout, now=NOW, ledger=MemLedger()).growth()
    assert fake.writes == []
    Sentinel(gh(fake), CFG_ALL, now=NOW, ledger=MemLedger(), home=False).growth()
    assert fake.writes == []
    Sentinel(gh(fake), CFG_ALL, llm=fake_llm(DRAFT), now=NOW, ledger=MemLedger(), dry_run=True).growth()
    assert fake.writes == []


def test_failed_sections_do_not_use_up_the_week():
    fake = contrib_setup()
    fake.search = None  # makes the scout search fail
    led = MemLedger()
    Sentinel(gh(fake), CFG, now=NOW, ledger=led).growth()
    assert fake.writes == [] and led.get("growth_week") is None
