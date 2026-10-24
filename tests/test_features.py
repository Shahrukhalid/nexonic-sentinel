from __future__ import annotations

import json
from datetime import timedelta

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from sentinel import settings as repo_settings
from sentinel.auth import App
from sentinel.bot import Sentinel, marker
from sentinel.github import GitHub
from sentinel.ledger import Ledger, key
from sentinel.llm import LLMQuotaExceeded, OpenRouter, json_from, safe_text
from sentinel.review import annotate_patch, clean_findings, fingerprint, reviewable, scannable
from tests.test_bot import CFG, NOW, RECENT, FakeGitHub, fake_llm, pr

PATCH = ("@@ -10,3 +10,4 @@ def load(path):\n     data = read(path)\n-    return data\n"
         "+    if data:\n+        return data[0]\n     log()")


class MemLedger(Ledger):
    """In-memory ledger for tests."""

    def __init__(self) -> None:
        super().__init__(None, "owner/nexonic-sentinel")


def gh(fake: FakeGitHub) -> GitHub:
    return GitHub("t", transport=httpx.MockTransport(fake.handler))


# ------------------------------------------------------------------ helpers
def test_annotate_patch_numbers_new_lines():
    text, added = annotate_patch(PATCH)
    assert added == {11, 12}
    assert "11 + " in text and "12 + " in text and "    - " in text and "10   " in text


def test_json_from_handles_fences_prose_and_braces_in_strings():
    assert json_from('Sure!\n```json\n{"a": "x { y }", "b": [1]}\n```') == {"a": "x { y }", "b": [1]}
    assert json_from('note {"a": "q\\"}"} trailing') == {"a": 'q"}'}
    with pytest.raises(ValueError):
        json_from("no json here")


def test_safe_text_neutralises_mentions_and_hidden_html():
    out = safe_text("ping @admin <!-- hidden --> <script>x</script> email a@b.com", 200)
    assert "@​admin" in out and "hidden" not in out and "<script>" not in out and "a@b.com" in out


def test_clean_findings_keeps_only_real_lines_and_serious_bugs():
    raw = {"findings": [
        {"file": "app.py", "line": 11, "severity": "high", "title": "Returns first item only",
         "explanation": "The function used to return the whole list; now callers get one element.", "fix": "return data"},
        {"file": "app.py", "line": 99, "severity": "high", "title": "Bad line", "explanation": "x" * 30},
        {"file": "other.py", "line": 11, "severity": "high", "title": "Wrong file", "explanation": "x" * 30},
        {"file": "app.py", "line": 12, "severity": "low", "title": "Style nit", "explanation": "x" * 30},
    ]}
    out = clean_findings(raw, {"app.py": {11, 12}}, "medium", 5)
    assert [f["title"] for f in out] == ["Returns first item only"]
    assert clean_findings("nonsense", None, "medium", 5) == []


def test_file_filters():
    assert reviewable("src/app.ts") and not reviewable("package-lock.json") and not reviewable("dist/app.js")
    assert scannable("src/app.py") and not scannable("tests/test_app.py") and not scannable("src/app.test.ts")


# ------------------------------------------------------------------ settings
def test_repository_settings_overlay_and_public_defaults():
    base = repo_settings.home_defaults(CFG)
    assert base["ai"] and base["dependabot"] == ["patch", "minor"]
    out = repo_settings.parse('ai = false\npr_review = "yes"\ndependabot = ["patch", "bogus"]\nunknown = 1', base)
    assert out["ai"] is False and out["pr_review"] is True and out["dependabot"] == ["patch"]
    assert repo_settings.parse("not = [valid", base) == base
    assert repo_settings.PUBLIC_DEFAULTS["ai"] is False and repo_settings.PUBLIC_DEFAULTS["dependabot"] == []


def test_other_installations_send_nothing_to_ai_and_merge_nothing_by_default():
    fake = FakeGitHub()
    fake.pulls = [pr(13, "Bump next from 16.4.0 to 16.4.1")]
    fake.pull_detail[13] = {"mergeable": True, "mergeable_state": "clean"}
    fake.checks["abc"] = [{"status": "completed", "conclusion": "success"}]
    fake.discussions = [{"id": "D1", "number": 1, "title": "How do I change the port?", "body": "", "createdAt": RECENT,
                         "url": "u", "isAnswered": False, "locked": False, "author": {"login": "bob", "__typename": "User"},
                         "category": {"name": "Q&A", "isAnswerable": True}, "comments": {"nodes": []}}]
    calls = []
    llm = OpenRouter("k", ["m"], transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(500)))
    bot = Sentinel(gh(fake), {**CFG, "owner": "someone"}, llm=llm, now=NOW, home=False)
    rep = bot.run()
    assert calls == [] and not any(w[1].endswith("/merge") for w in fake.writes)
    assert rep.private


def test_repository_can_opt_out_entirely():
    fake = FakeGitHub()
    fake.files[".github/nexonic-sentinel.toml"] = "enabled = false"
    fake.issues = [{"number": 1, "title": "App crashes", "body": "", "user": {"login": "alice", "type": "User"},
                    "created_at": RECENT, "labels": []}]
    Sentinel(gh(fake), CFG, now=NOW).run()
    assert fake.writes == []


# ------------------------------------------------------------------ pull request review
def review_pr(n=21, login="carol"):
    return {**pr(n, "Return first item", login=login, assoc="CONTRIBUTOR", sha="abc123def4567"),
            "updated_at": RECENT, "body": "Small change, tests pass."}


def test_pull_request_review_posts_checked_findings_once():
    fake = FakeGitHub()
    fake.pulls = [review_pr()]
    fake.pr_files[21] = [{"filename": "app.py", "status": "modified", "patch": PATCH},
                         {"filename": "package-lock.json", "status": "modified", "patch": "@@ -1 +1 @@\n-a\n+b"}]
    fake.checks["abc123def4567"] = [{"name": "tests", "status": "completed", "conclusion": "failure", "html_url": "h"}]
    prompts: list[str] = []
    llm = fake_llm({"findings": [
        {"file": "app.py", "line": 11, "severity": "high", "title": "Only the first item is returned",
         "explanation": "Callers expected the full list; now they get a single element and iterate over its characters.",
         "fix": "return data"},
        {"file": "app.py", "line": 3, "severity": "high", "title": "Hallucinated line", "explanation": "x" * 40}]}, prompts)
    ledger = MemLedger()
    Sentinel(gh(fake), CFG, llm=llm, now=NOW, ledger=ledger).run()
    posts = [w for w in fake.writes if w[1].endswith("/pulls/21/reviews")]
    assert len(posts) == 1
    body = posts[0][2]["body"]
    assert posts[0][2]["event"] == "COMMENT" and posts[0][2]["commit_id"] == "abc123def4567"
    assert marker("review:abc123def456") in body and "Only the first item is returned" in body
    assert "Hallucinated" not in body and "Failing checks" in body and "app.py#L11" in body
    assert "package-lock" not in prompts[0] and "11 + " in prompts[0]
    fake.writes.clear()
    Sentinel(gh(fake), CFG, llm=llm, now=NOW, ledger=ledger).run()  # same head commit: not reviewed again
    assert not any(w[1].endswith("/reviews") for w in fake.writes)


def test_large_or_bot_pull_requests_are_not_reviewed():
    fake = FakeGitHub()
    big = "@@ -1 +1,2 @@\n" + "\n".join("+x = 1" for _ in range(6000))
    fake.pulls = [review_pr(), {**review_pr(22, login="renovate[bot]"), "user": {"login": "renovate[bot]", "type": "Bot"}}]
    fake.pr_files[21] = [{"filename": "app.py", "status": "modified", "patch": big}]
    fake.pr_files[22] = [{"filename": "app.py", "status": "modified", "patch": PATCH}]
    calls = []
    llm = OpenRouter("k", ["m"], transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(500)))
    Sentinel(gh(fake), CFG, llm=llm, now=NOW, ledger=MemLedger()).run()
    assert calls == []


# ------------------------------------------------------------------ weekly code health scan
def test_code_scan_reports_new_findings_once_a_week():
    fake = FakeGitHub()
    code = "def add(a, b):\n    return a - b\n" + "# padding\n" * 30
    fake.files["src/calc.py"] = code
    llm = fake_llm({"findings": [{"line": 2, "severity": "high", "title": "add subtracts",
                                  "explanation": "add() returns a - b, so every caller gets the wrong sum.",
                                  "fix": "return a + b"}]})
    ledger = MemLedger()
    Sentinel(gh(fake), CFG, llm=llm, now=NOW, ledger=ledger).run()
    posts = [w for w in fake.writes if w[1] == "/repos/owner/app/issues" and w[0] == "POST"]
    assert len(posts) == 1 and posts[0][2]["labels"] == ["code-health"]
    assert "add subtracts" in posts[0][2]["body"] and "src/calc.py#L2" in posts[0][2]["body"]
    assert ledger.get(key("scan", "owner/app")) == NOW.strftime("%G-W%V")
    fake.writes.clear()
    Sentinel(gh(fake), CFG, llm=llm, now=NOW, ledger=ledger).run()
    assert not [w for w in fake.writes if w[1] == "/repos/owner/app/issues"]


def test_code_scan_skips_findings_already_reported():
    fake = FakeGitHub()
    fake.files["src/calc.py"] = "def add(a, b):\n    return a - b\n" + "# padding\n" * 30
    fp = fingerprint("src/calc.py", "add subtracts")
    fake.issues = [{"number": 9, "title": "Code health report", "body": f"<!-- fp:{fp} -->",
                    "user": {"login": "nexonic-sentinel[bot]", "type": "Bot"}, "created_at": RECENT, "labels": []}]
    llm = fake_llm({"findings": [{"line": 2, "severity": "high", "title": "Add subtracts!",
                                  "explanation": "add() returns a - b, so every caller gets the wrong sum."}]})
    Sentinel(gh(fake), CFG, llm=llm, now=NOW, ledger=MemLedger()).run()
    assert not [w for w in fake.writes if w[1] == "/repos/owner/app/issues"]


# ------------------------------------------------------------------ contribution shortlist
PARSER = "def parse(text):\n    parts = text.split(',')\n    return parts[0]\n"


def contrib_setup():
    fake = FakeGitHub()
    fake.search = [{"html_url": "https://github.com/acme/tool/issues/5", "number": 5, "title": "Parser crashes on empty input",
                    "body": "Steps...", "comments": 1, "labels": [{"name": "good first issue"}],
                    "repository_url": "https://api.github.com/repos/acme/tool"}]
    fake.ext_repos["acme/tool"] = {"full_name": "acme/tool", "stargazers_count": 900, "pushed_at": RECENT,
                                   "archived": False, "fork": False, "language": "Python", "default_branch": "main",
                                   "description": "A tool"}
    fake.ext_files["/repos/acme/tool/contents/src/parser.py"] = PARSER
    return fake


DRAFT = {"difficulty": "easy", "summary": "Empty input raises IndexError.", "approach": ["Guard empty input"],
         "files": ["src/parser.py", "made/up.py"], "test": "Parse an empty string", "worth_it": True}
GOOD_FIX = {"confident": True, "explanation": "Return None for empty input.", "test": "assert parse('') is None",
            "edits": [{"file": "src/parser.py", "start": 1, "end": 1, "old": "def parse(text):",
                       "new": "def parse(text):\n    if not text:\n        return None"}]}


def test_contribution_hunt_drafts_a_verified_fix_and_runs_hourly():
    cfg = {**CFG, "contrib": {"every_hours": 1}}
    fake = contrib_setup()
    ledger = MemLedger()
    bot = Sentinel(gh(fake), cfg, llm=fake_llm([DRAFT, GOOD_FIX]), now=NOW, ledger=ledger)
    bot.contributions()
    post = [w for w in fake.writes if w[1] == "/repos/owner/nexonic-sentinel/issues"][0][2]
    body = post["body"]
    assert post["labels"] == ["contribute"] and "Parser crashes on empty input" in body and "★ 900" in body
    assert "`src/parser.py`" in body and "made/up.py" not in body
    assert "```diff" in body and "+    if not text:" in body and "--- a/src/parser.py" in body
    fake.writes.clear()
    Sentinel(gh(fake), cfg, llm=fake_llm(DRAFT), now=NOW + timedelta(minutes=30), ledger=ledger).contributions()
    assert fake.writes == []  # not due yet
    fake.home_issues = [{"number": 7, "body": marker("contribute"), "labels": [{"name": "contribute"}]}]
    Sentinel(gh(fake), cfg, llm=fake_llm(DRAFT), now=NOW + timedelta(hours=1), ledger=ledger).contributions()
    assert fake.writes == []  # due, but the only issue was already shown: nothing new to post
    fake.search[0] = {**fake.search[0], "html_url": "https://github.com/acme/tool/issues/6", "number": 6,
                      "title": "Second bug"}
    Sentinel(gh(fake), cfg, llm=fake_llm({**DRAFT, "worth_it": True}), now=NOW + timedelta(hours=2),
             ledger=ledger).contributions()
    (method, path, data), = fake.writes
    assert path.endswith("/issues/7/comments") and "Second bug" in data["body"]


def test_fix_rejected_when_it_does_not_match_the_real_file_or_breaks_syntax():
    from sentinel.contrib import apply_edits, check_syntax
    src = {"src/parser.py": PARSER}
    wrong_old = [{"file": "src/parser.py", "start": 2, "end": 2, "old": "parts = text.split(';')", "new": "x"}]
    assert apply_edits(src, wrong_old) is None
    assert apply_edits(src, [{"file": "other.py", "start": 1, "end": 1, "old": "", "new": ""}]) is None
    assert apply_edits(src, [{"file": "src/parser.py", "start": 9, "end": 9, "old": "", "new": ""}]) is None
    ok = apply_edits(src, GOOD_FIX["edits"])
    assert ok and ok["src/parser.py"].startswith("def parse(text):\n    if not text:")
    assert not check_syntax("a.py", "def f(:\n")
    fake = contrib_setup()
    broken = {**GOOD_FIX, "edits": [{**GOOD_FIX["edits"][0], "new": "def parse(text:"}]}
    Sentinel(gh(fake), CFG, llm=fake_llm([DRAFT, broken]), now=NOW, ledger=MemLedger()).contributions()
    body = [w for w in fake.writes if w[1] == "/repos/owner/nexonic-sentinel/issues"][0][2]["body"]
    assert "Guard empty input" in body and "```diff" not in body


def test_all_free_models_are_discovered_and_rate_limited_ones_skipped():
    from sentinel.llm import pick_free
    catalog = [{"id": "big/model:free", "context_length": 262000, "pricing": {"prompt": "0", "completion": "0"}},
               {"id": "paid/model", "context_length": 999999, "pricing": {"prompt": "0.000001", "completion": "0.000002"}},
               {"id": "small/model:free", "context_length": 8000, "pricing": {"prompt": "0", "completion": "0"}},
               {"id": "img/model:free", "context_length": 9000, "architecture": {"output_modalities": ["image"]}},
               {"id": "music/model", "context_length": 9000, "pricing": {"prompt": "0", "completion": "0"},
                "architecture": {"output_modalities": ["text", "audio"]}},
               {"id": "openrouter/free", "context_length": 9000000, "pricing": {"prompt": "0", "completion": "0"}}]
    assert pick_free(catalog) == ["big/model:free", "small/model:free"]
    assert pick_free(catalog, exclude=["small"]) == ["big/model:free"]
    tried = []

    def handler(req):
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": catalog})
        model = json.loads(req.content)["model"]
        tried.append(model)
        if model == "big/model:free":
            return httpx.Response(429)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    llm = OpenRouter("k", ["all-free"], transport=httpx.MockTransport(handler))
    assert llm.complete("s", "u") == "ok" and llm.complete("s", "u") == "ok"
    assert tried == ["big/model:free", "small/model:free", "small/model:free"]


# ------------------------------------------------------------------ ledger, budget, auth
def test_ledger_round_trip_creates_then_updates():
    fake = FakeGitHub()
    led = Ledger(gh(fake), "owner/nexonic-sentinel")
    led.set("contrib", "2026-10-26")
    led.save()
    created = [w for w in fake.writes if w[0] == "POST"][0][2]
    assert created["labels"] == ["ledger"] and '"contrib": "2026-10-26"' in created["body"]
    fake.home_issues = [{"number": 3, "body": created["body"], "labels": [{"name": "ledger"}]}]
    again = Ledger(gh(fake), "owner/nexonic-sentinel")
    assert again.get("contrib") == "2026-10-26"
    fake.writes.clear()
    again.save()  # unchanged: no write
    assert fake.writes == []


def test_quota_exhaustion_stops_ai_for_the_run():
    llm = OpenRouter("k", ["m1", "m2"], transport=httpx.MockTransport(lambda r: httpx.Response(429)))
    with pytest.raises(LLMQuotaExceeded):
        llm.complete("s", "u")
    assert llm.exhausted and not llm.available()
    capped = fake_llm({"ok": True})
    capped.budget = 1
    capped.complete("s", "u")
    assert not capped.available()


def test_app_auth_signs_jwt_and_mints_installation_tokens():
    pem = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem_text = pem.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                 serialization.NoEncryption()).decode()
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        token = req.headers["Authorization"].split()[1]
        claims = jwt.decode(token, pem.public_key(), algorithms=["RS256"])
        seen.append(claims["iss"])
        if req.url.path == "/app/installations":
            return httpx.Response(200, json=[{"id": 1, "account": {"login": "owner"}},
                                             {"id": 2, "account": {"login": "x"}, "suspended_at": "2026-01-01"}])
        return httpx.Response(201, json={"token": "ghs_test"})

    app = App("Iv23test", pem_text, transport=httpx.MockTransport(handler))
    assert [i["id"] for i in app.installations()] == [1]
    assert app.token(1) == "ghs_test" and seen == ["Iv23test", "Iv23test"]


def test_example_settings_file_parses():
    out = repo_settings.parse(repo_settings.EXAMPLE, repo_settings.PUBLIC_DEFAULTS)
    assert out == {**repo_settings.PUBLIC_DEFAULTS, "dependabot": []}
    assert json.dumps(out)


def test_quota_error_during_review_is_retried_next_run():
    fake = FakeGitHub()
    fake.pulls = [review_pr()]
    fake.pr_files[21] = [{"filename": "app.py", "status": "modified", "patch": PATCH}]
    ledger = MemLedger()
    limited = OpenRouter("k", ["m"], transport=httpx.MockTransport(lambda r: httpx.Response(429)))
    rep = Sentinel(gh(fake), CFG, llm=limited, now=NOW, ledger=ledger).run()
    assert any("AI paused" in f for f in rep.flagged)
    assert not any(k for k in ledger.data if k == key("review", "owner/app", "abc123def4567"))
    ok = fake_llm({"findings": []})
    Sentinel(gh(fake), CFG, llm=ok, now=NOW, ledger=ledger).run()
    body = [w for w in fake.writes if w[1].endswith("/pulls/21/reviews")][0][2]["body"]
    assert "didn't spot likely bugs" in body
