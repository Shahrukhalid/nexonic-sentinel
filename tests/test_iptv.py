from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timezone

import httpx
import pytest
import yaml

from sentinel import iptv
from sentinel.bot import Report, Sentinel
from sentinel.github import GitHub
from sentinel.ledger import Ledger

REPO = "owner/iptv"
MONDAY = datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc)
WEEK = "2026-W44"
BANNED = re.compile(r"cl[a]ude|anthr[o]pic|copil[o]t|\bAI\b|\bLLM\b|openrouter|gpt|gemini|co-authored", re.I)

PENDING_QA = """# Candidates awaiting review for QA. See docs/SOURCE_POLICY.md before editing.
channels:
- id: qatar-tv-2
  name: Qatar Television 2
  broadcaster: Qatar Media Corporation
  country: qa
  languages:
  - ar
  categories:
  - general
  website: https://www.qmc.qa
  stream:
    url: https://qatartv.akamaized.net/hls/live/20000611/qtv2/master.m3u8
    protocol: hls
    quality: unknown
  source:
    reference_url: https://github.com/Free-TV/IPTV/tree/master/lists
    evidence_url: https://www.qmc.qa/
    evidence_summary: 'Pending: need an official page on a QMC domain that plays this stream (same open question as Qatar
      Television).'
    evidence_checked: null
    notes: ''
  epg_id: QatarTelevision2.qa
  logo: null
  geo:
    restricted: unknown
    regions: []
  is_nsfw: false
  first_discovered: '2026-10-27'
  review:
    status: pending
    reviewer: initial-curation (maintainer confirmation pending)
    reviewed_at: '2026-10-27'
    authorization_basis: none
    adult_screening: pass
    notes: 'Pending: need an official page on a QMC domain that plays this stream (it''s the same open question).'
    rejection_reason: ''
- id: qatar-tv-quran
  name: Qatar Quran
  broadcaster: Qatar Media Corporation
  country: qa
  languages:
  - ar
  categories:
  - culture
  website: "https://www.qmc.qa"
  stream:
    url: "https://qatartv.akamaized.net/hls/live/20000612/q/master.m3u8"
    protocol: hls
    quality: unknown
  source:
    reference_url: https://github.com/Free-TV/IPTV/tree/master/lists
    evidence_url: https://www.qmc.qa/
    evidence_summary: Hand edited record with other quoting.
    evidence_checked: null
    notes: ''
  epg_id: null
  logo: null
  geo:
    restricted: unknown
    regions: []
  is_nsfw: false
  first_discovered: '2026-10-27'
  review:
    status: pending
    reviewer: initial-curation
    reviewed_at: '2026-10-27'
    authorization_basis: official-cdn-stream
    adult_screening: pass
    notes: Needs a look.
    rejection_reason: ''
"""
APPROVED_QA = """# Approved channels for QA. See docs/SOURCE_POLICY.md before editing.
channels:
- id: al-jazeera-english
  name: Al Jazeera English
  broadcaster: Al Jazeera Media Network
  country: qa
  languages:
  - en
  categories:
  - news
  website: https://www.aljazeera.com
  stream:
    url: https://live-hls-apps-aje-fa.getaj.net/AJE/index.m3u8
    protocol: hls
    quality: unknown
  source:
    reference_url: https://example.org
    evidence_url: https://www.aljazeera.com/live
    evidence_summary: Al Jazeera streams free on aljazeera.com.
    evidence_checked: null
    notes: ''
  epg_id: null
  logo: null
  geo:
    restricted: false
    regions: []
  is_nsfw: false
  first_discovered: '2026-10-17'
  review:
    status: approved
    reviewer: initial-curation
    reviewed_at: '2026-10-17'
    authorization_basis: official-cdn-stream
    adult_screening: pass
    notes: ''
    rejection_reason: ''
"""
PENDING_DE = PENDING_QA.replace("qatar-tv-2", "de-nsfw").replace("country: qa", "country: de").replace(
    "is_nsfw: false", "is_nsfw: true").split("- id: qatar-tv-quran")[0]
REJECTED = """# Rejected candidates kept so they are not re-added. Never published.
channels:
- id: old-restream
  name: Old Restream
  broadcaster: unknown
  country: de
  languages:
  - de
  categories:
  - news
  website: https://example.de
  stream:
    url: https://restream.example.de/live/index.m3u8
    protocol: hls
    quality: unknown
  source:
    reference_url: https://example.org
    evidence_url: https://example.de/live
    evidence_summary: Rejected candidate, restream.
    evidence_checked: null
    notes: ''
  epg_id: null
  logo: null
  geo:
    restricted: unknown
    regions: []
  is_nsfw: false
  first_discovered: '2026-10-17'
  review:
    status: rejected
    reviewer: initial-curation
    reviewed_at: '2026-10-17'
    authorization_basis: none
    adult_screening: pass
    notes: ''
    rejection_reason: Third-party restream.
"""
COUNTRIES = "name,code,languages,flag\n" + "\n".join(
    f"C{a}{b},{a}{b},xx,F" for a in "ABCDEFGHIJ" for b in "ABCDEFGHIJ") + "\nGermany,DE,deu,F\nQatar,QA,ara,F\n"
REFERENCE = ('LANGUAGES: dict[str, str] = {\n' + "".join(f'    "{a}{b}": "x",\n' for a in "abcd" for b in "abcdefg")
             + '    "de": "German",\n    "en": "English",\n    "ar": "Arabic",\n}\n\nCATEGORIES: dict[str, str] = {\n'
             + "".join(f'    "{c}": "x",\n' for c in ["news", "general", "kids", "culture", "sports", "music"]) + "}\n")
CHANNELS_JSON = json.dumps({"channels": [
    {"id": "dw-english", "name": "DW English", "lifecycle": "intermittent",
     "health": {"last_checked": "2026-10-27T11:04:27Z", "last_success": "2026-10-27T08:58:53Z",
                "last_status": "UNAVAILABLE", "success_rate": 0.7, "checks_in_window": 14}},
    {"id": "arte-de", "name": "ARTE", "lifecycle": "working", "health": {}},
    {"id": "arte-fr", "name": "ARTE", "lifecycle": "working", "health": {}}]})

FILES = {"data/pending/qa.yaml": PENDING_QA, "data/pending/de.yaml": PENDING_DE, "data/approved/qa.yaml": APPROVED_QA,
         "data/rejected/rejected.yaml": REJECTED, "nexonic/models.py": 'method: Literal["manual", "community-suggestion"]\n',
         "nexonic/reference/countries.csv": COUNTRIES, "nexonic/reference/__init__.py": REFERENCE,
         "reports/channels.json": CHANNELS_JSON}

FORM = """### Official channel name

{name}

### Country (ISO 3166-1 alpha-2)

{country}

### Language(s) (ISO 639-1)

{language}

### Main category

{category}

### Stream URL (.m3u8)

{stream}

### Official broadcaster website

{website}

### Evidence of free availability

{evidence}

### Region limits

Works worldwide

### Confirmations

{confirm}
"""
GOOD = dict(name="Example News", country="de", language="de, en", category="News",
            stream="https://stream.example.de/live/index.m3u8", website="https://www.example.de",
            evidence="https://www.example.de/live", confirm="- [x] a\n- [x] b\n- [x] c")


def form(**kw) -> str:
    return FORM.format(**{**GOOD, **kw})


def owner_comment(cid, body, **kw):
    return {"id": cid, "body": body, "user": {"login": "owner", "type": "User"}, "author_association": "OWNER", **kw}


class Host:
    """The IPTV repository: a tiny git store plus issues, recording every write."""

    def __init__(self, files=None) -> None:
        self.files = dict(FILES if files is None else files)
        self.commits: list[dict] = []
        self.writes: list[tuple[str, str, dict]] = []
        self.issues: list[dict] = []
        self.comments: dict[int, list[dict]] = {}
        self.sha = "c0"
        self.trees = {"t0": dict(self.files)}
        self.cmap = {"c0": "t0"}
        self.fail_ref = 0
        self.next_id = 1000

    def handler(self, req: httpx.Request) -> httpx.Response:
        p, m = req.url.path, req.method
        body = json.loads(req.content or b"{}") if m != "GET" else {}
        if m != "GET":
            self.writes.append((m, p, body))
        base = f"/repos/{REPO}"
        if not p.startswith(base):
            return httpx.Response(404, json={"message": f"outside: {p}"})
        sub = p[len(base):]
        if sub == "":
            return httpx.Response(200, json={"default_branch": "main"})
        if sub == "/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": self.sha}})
        if sub.startswith("/git/commits/") and m == "GET":
            return httpx.Response(200, json={"tree": {"sha": self.cmap[sub.rsplit("/", 1)[1]]}})
        if sub.startswith("/git/trees/") and m == "GET":
            return httpx.Response(200, json={"truncated": False, "tree": [
                {"path": k, "type": "blob"} for k in self.trees[sub.rsplit("/", 1)[1]]]})
        if sub.startswith("/contents/"):
            path = sub[len("/contents/"):]
            files = self.trees[self.cmap[req.url.params["ref"]]]
            if path not in files:
                return httpx.Response(404)
            return httpx.Response(200, json={"encoding": "base64", "content": base64.b64encode(files[path].encode()).decode()})
        if sub == "/git/trees" and m == "POST":
            files = dict(self.trees[body["base_tree"]])
            files.update({e["path"]: e["content"] for e in body["tree"]})
            self.trees[f"t{len(self.trees)}"] = files
            return httpx.Response(201, json={"sha": f"t{len(self.trees) - 1}"})
        if sub == "/git/commits" and m == "POST":
            sha = f"c{len(self.cmap)}"
            self.cmap[sha] = body["tree"]
            self.commits.append({**body, "sha": sha})
            return httpx.Response(201, json={"sha": sha})
        if sub == "/git/refs/heads/main" and m == "PATCH":
            if self.fail_ref:
                self.fail_ref -= 1
                return httpx.Response(422, json={"message": "not a fast forward"})
            assert body["force"] is False
            self.sha = body["sha"]
            self.files = self.trees[self.cmap[self.sha]]
            return httpx.Response(200, json={})
        if sub == "/issues" and m == "GET":
            wanted = req.url.params.get("labels")
            state = req.url.params.get("state", "open")
            out = [i for i in self.issues if (state == "all" or i["state"] == state)
                   and (not wanted or wanted in [x["name"] for x in i["labels"]])]
            return httpx.Response(200, json=out)
        if sub == "/issues" and m == "POST":
            self.issues.append({"number": 50 + len(self.issues), "state": "open", "user": {"login": "x[bot]", "type": "Bot"},
                                "title": body["title"], "body": body["body"], "labels": [{"name": n} for n in body["labels"]]})
            return httpx.Response(201, json={"number": 99})
        mt = re.fullmatch(r"/issues/(\d+)(/comments|/labels)?", sub)
        if mt:
            n = int(mt.group(1))
            if mt.group(2) == "/comments" and m == "GET":
                return httpx.Response(200, json=self.comments.get(n, []))
            if mt.group(2) == "/comments":
                self.next_id += 1
                self.comments.setdefault(n, []).append({"id": self.next_id, "body": body["body"],
                                                        "user": {"login": "x[bot]", "type": "Bot"}})
                return httpx.Response(201, json={})
            if m == "PATCH":
                next(i for i in self.issues if i["number"] == n)["state"] = body["state"]
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"message": f"not faked: {m} {p}"})

    def replies(self, n) -> list[str]:
        return [c["body"] for c in self.comments.get(n, []) if (c["user"] or {}).get("login") == "x[bot]"]


class Mem(Ledger):
    def __init__(self) -> None:
        super().__init__(None, "owner/nexonic-sentinel")


def review_issue(n=7, title=f"Channel review {WEEK}", state="open"):
    return {"number": n, "state": state, "title": title, "body": iptv.MARK_REVIEW + "\nx",
            "user": {"login": "nexonic-sentinel[bot]", "type": "Bot"}, "labels": [{"name": "channel-review"}]}


def user_issue(n, body, labels=("channel-suggestion",), login="alice"):
    return {"number": n, "state": "open", "title": "Suggest: x", "body": body, "user": {"login": login, "type": "User"},
            "labels": [{"name": x} for x in labels], "html_url": f"https://github.com/{REPO}/issues/{n}"}


def run(host, ledger=None, now=MONDAY, cfg=None, dry=False, owner="owner"):
    ledger = ledger or Mem()
    report = Report()
    gh = GitHub("t", transport=httpx.MockTransport(host.handler))
    conf = {"enabled": True, "repo": REPO, "owner_login": "owner", **(cfg or {})}
    iptv.ChannelReview(gh, conf, owner, now, ledger, report, dry).run()
    return report, ledger


def pending_ids(host):
    ids = []
    for k, v in host.files.items():
        if k.startswith("data/pending/"):
            ids += [r["id"] for r in yaml.safe_load(v)["channels"]]
    return ids


# ------------------------------------------------------------------ approve / reject and the YAML move
def test_approve_moves_record_in_one_commit_and_keeps_other_lines():
    host = Host()
    host.issues = [review_issue()]
    host.comments[7] = [owner_comment(1, "approve qatar-tv-quran")]
    report, _ = run(host)
    assert not report.errors
    assert len(host.commits) == 1 and host.commits[0]["message"] == "Approve channel qatar-tv-quran"
    assert host.commits[0]["parents"] == ["c0"]
    pending, approved = host.files["data/pending/qa.yaml"], host.files["data/approved/qa.yaml"]
    assert "qatar-tv-quran" not in pending and pending.startswith(PENDING_QA.split("- id: qatar-tv-quran")[0])
    assert approved.startswith(APPROVED_QA)  # existing approved text is untouched, the record is appended
    rec = yaml.safe_load(approved)["channels"][-1]
    assert rec["id"] == "qatar-tv-quran" and rec["name"] == "Qatar Quran"
    assert rec["review"] == {"status": "approved", "reviewer": "owner", "reviewed_at": "2026-10-26",
                             "authorization_basis": "official-cdn-stream", "adult_screening": "pass",
                             "notes": "Needs a look.", "rejection_reason": ""}
    original = yaml.safe_load(PENDING_QA)["channels"][1]
    assert {k: v for k, v in rec.items() if k != "review"} == {k: v for k, v in original.items() if k != "review"}
    assert 'website: "https://www.qmc.qa"' in approved  # hand-edited quoting is preserved
    assert "Approved" in host.replies(7)[0] and "c1" in host.replies(7)[0]


def test_approve_needs_basis_when_none_and_accepts_one_on_the_line():
    host = Host()
    host.issues = [review_issue()]
    host.comments[7] = [owner_comment(1, "approve qatar-tv-2")]
    run(host)
    assert host.commits == [] and "no authorization basis" in host.replies(7)[0]
    host.comments[7].append(owner_comment(2, "approve qatar-tv-2 official-broadcaster-stream"))
    run(host)
    rec = yaml.safe_load(host.files["data/approved/qa.yaml"])["channels"][-1]
    assert rec["review"]["authorization_basis"] == "official-broadcaster-stream" and rec["review"]["status"] == "approved"
    assert "rejection_reason: ''" in host.files["data/approved/qa.yaml"]
    assert "pending: need" not in host.files["data/pending/qa.yaml"].split("qatar-tv-quran")[0]  # record left pending file


def test_reject_goes_to_country_file_with_reason_and_creates_it():
    host = Host()
    host.issues = [review_issue()]
    host.comments[7] = [owner_comment(1, "reject qatar-tv-2 it's a restream: not theirs, # no")]
    run(host)
    text = host.files["data/rejected/qa.yaml"]
    assert text.startswith("# Rejected candidates kept so they are not re-added. Never published.\nchannels:\n- id: qatar-tv-2")
    rec = yaml.safe_load(text)["channels"][0]
    assert rec["review"]["status"] == "rejected" and rec["review"]["reviewer"] == "owner"
    assert rec["review"]["rejection_reason"] == "it's a restream: not theirs, # no"
    assert rec["review"]["notes"].startswith("Pending: need") and rec["review"]["authorization_basis"] == "none"
    assert host.commits[0]["message"] == "Reject channel qatar-tv-2"


def test_multiple_commands_unknown_ids_and_bad_syntax_get_one_reply():
    host = Host()
    host.issues = [review_issue()]
    host.comments[7] = [owner_comment(1, "Thanks!\napprove qatar-tv-quran\nreject nope-id because\nreject qatar-tv-2\n"
                                         "approve qatar-tv-2 sketchy")]
    run(host)
    assert [c["message"] for c in host.commits] == ["Approve channel qatar-tv-quran"]
    reply = host.replies(7)[0]
    assert "`nope-id` is not in `data/pending`" in reply and "needs a reason" in reply and "not an authorization basis" in reply
    assert len(host.replies(7)) == 1


def test_emptied_pending_file_keeps_valid_empty_list():
    host = Host({**FILES, "data/pending/qa.yaml": PENDING_QA.split("- id: qatar-tv-quran")[0]})
    host.issues = [review_issue()]
    host.comments[7] = [owner_comment(1, "reject qatar-tv-2 not official")]
    run(host)
    assert host.files["data/pending/qa.yaml"].endswith("channels: []\n")
    assert yaml.safe_load(host.files["data/pending/qa.yaml"])["channels"] == []


def test_adult_unsafe_and_duplicate_approvals_refused():
    dup = PENDING_QA.replace("qatar-tv-quran", "qatar-dup").replace("Qatar Quran", "Al Jazeera English")
    host = Host({**FILES, "data/pending/qa.yaml": dup})
    host.issues = [review_issue()]
    host.comments[7] = [owner_comment(1, "approve qatar-dup\napprove de-nsfw official-cdn-stream")]
    run(host)
    reply = host.replies(7)[0]
    assert host.commits == [] and "duplicate the approved channel" in reply and "adult-oriented" in reply
    token = PENDING_QA.replace("master.m3u8", "master.m3u8?token=abc")
    host = Host({**FILES, "data/pending/qa.yaml": token})
    host.issues = [review_issue()]
    host.comments[7] = [owner_comment(1, "approve qatar-tv-quran")]
    run(host)
    assert host.commits == [] or "token" not in host.files["data/approved/qa.yaml"]


def test_conflict_is_retried_once_then_reported():
    host = Host()
    host.issues = [review_issue()]
    host.comments[7] = [owner_comment(1, "reject qatar-tv-2 not official")]
    host.fail_ref = 1
    run(host)
    assert "Rejected `qatar-tv-2`" in host.replies(7)[0]
    host = Host()
    host.issues = [review_issue()]
    host.comments[7] = [owner_comment(1, "reject qatar-tv-2 not official")]
    host.fail_ref = 5
    run(host)
    assert "repository kept changing" in host.replies(7)[0] and "qatar-tv-2" in pending_ids(host)


# ------------------------------------------------------------------ who may command, idempotency
@pytest.mark.parametrize("comment", [
    {"id": 1, "body": "approve qatar-tv-2 official-cdn-stream", "user": {"login": "mallory", "type": "User"},
     "author_association": "OWNER"},
    {"id": 1, "body": "approve qatar-tv-2 official-cdn-stream", "user": {"login": "owner", "type": "User"},
     "author_association": "COLLABORATOR"},
    {"id": 1, "body": "approve qatar-tv-2 official-cdn-stream", "user": {"login": "owner", "type": "Bot"},
     "author_association": "OWNER"},
    {"id": 1, "body": "approve qatar-tv-2 official-cdn-stream", "user": {"login": "owner[bot]", "type": "Bot"},
     "author_association": "OWNER"},
    {"id": 1, "body": "approve qatar-tv-2 official-cdn-stream", "user": {"login": "Owner2", "type": "User"},
     "author_association": "OWNER"},
    {"id": 1, "body": "approve qatar-tv-2 official-cdn-stream", "user": None, "author_association": "OWNER"},
])
def test_only_the_owner_is_obeyed(comment):
    host = Host()
    host.issues = [review_issue()]
    host.comments[7] = [comment]
    run(host)
    assert host.commits == [] and host.replies(7) == [] and "qatar-tv-2" in pending_ids(host)


def test_commands_on_other_issues_or_closed_review_are_ignored():
    host = Host()
    stranger = {**review_issue(8), "user": {"login": "mallory", "type": "User"}}
    host.issues = [stranger, review_issue(9, state="closed"), {**review_issue(10), "body": "no marker"}]
    for n in (8, 9, 10):
        host.comments[n] = [owner_comment(n, "reject qatar-tv-2 not official")]
    run(host)
    assert host.commits == [] and "qatar-tv-2" in pending_ids(host)


def test_idempotent_via_ledger_and_via_reply_marker():
    host = Host()
    host.issues = [review_issue()]
    host.comments[7] = [owner_comment(1, "reject qatar-tv-2 not official")]
    _, ledger = run(host)
    run(host, ledger)
    assert len(host.commits) == 1 and len(host.replies(7)) == 1
    # the ledger was lost after the reply was posted: the marker in the bot's reply prevents a second run
    run(host, Mem())
    assert len(host.commits) == 1 and len(host.replies(7)) == 1
    # a person pasting the marker does not silence a command
    host2 = Host()
    host2.issues = [review_issue()]
    host2.comments[7] = [{"id": 5, "body": "x <!-- nexonic-sentinel:iptv-cmd:6 -->", "user": {"login": "m", "type": "User"}},
                         owner_comment(6, "reject qatar-tv-2 not official")]
    run(host2)
    assert len(host2.commits) == 1


def test_dry_run_writes_nothing():
    host = Host()
    host.issues = [review_issue(), user_issue(3, form())]
    host.comments[7] = [owner_comment(1, "reject qatar-tv-2 not official")]
    report, _ = run(host, dry=True)
    assert host.writes == [] and any("[dry run]" in a for a in report.actions)


# ------------------------------------------------------------------ weekly issue
def test_weekly_issue_created_table_and_previous_closed():
    host = Host()
    host.issues = [review_issue(3, "Channel review 2026-W43")]
    report, ledger = run(host)
    created = [i for i in host.issues if i["title"] == f"Channel review {WEEK}"]
    assert len(created) == 1 and created[0]["labels"] == [{"name": "channel-review"}]
    body = created[0]["body"]
    assert "| `qatar-tv-2` | qa | Qatar Television 2 | general | [page](https://www.qmc.qa/) | `qatartv.akamaized.net` |" in body
    assert "### How to decide" in body and "approve <id>" in body and "reject <id> <reason>" in body
    assert host.issues[0]["state"] == "closed"
    run(host, ledger)
    assert len([i for i in host.issues if i["title"] == f"Channel review {WEEK}"]) == 1
    assert not report.errors


def test_weekly_issue_waits_for_the_weekday_and_not_duplicated():
    host = Host()
    run(host, cfg={"weekday": "wednesday"})
    assert host.issues == []
    run(host, cfg={"weekday": 0}, now=datetime(2026, 10, 28, tzinfo=timezone.utc))
    assert len(host.issues) == 1
    run(host, Mem(), cfg={"weekday": 0}, now=datetime(2026, 10, 29, tzinfo=timezone.utc))
    assert len(host.issues) == 1


def test_table_text_is_neutralised():
    nasty = PENDING_QA.replace("Qatar Television 2", "Evil | @victim <b>x</b>")
    host = Host({**FILES, "data/pending/qa.yaml": nasty})
    run(host)
    body = host.issues[0]["body"]
    assert "@victim" not in body and "<b>" not in body and "Evil / &#64;victim &lt;b&gt;x&lt;/b&gt;" in body


# ------------------------------------------------------------------ suggestions
def test_valid_suggestion_becomes_pending_record():
    host = Host()
    host.issues = [user_issue(3, form())]
    report, _ = run(host)
    assert not report.errors and len(host.commits) == 1
    assert host.commits[0]["message"] == "Add suggested channel example-news"
    rec = yaml.safe_load(host.files["data/pending/de.yaml"])["channels"][-1]
    assert rec["id"] == "example-news" and rec["source"]["method"] == "community-suggestion"
    assert rec["source"]["reference_url"] == f"https://github.com/{REPO}/issues/3"
    assert rec["source"]["evidence_url"] == "https://www.example.de/live" and rec["source"]["evidence_summary"]
    assert rec["review"]["status"] == "pending" and rec["review"]["authorization_basis"] == "none"
    assert rec["languages"] == ["de", "en"] and rec["categories"] == ["news"] and rec["geo"]["restricted"] is False
    assert host.files["data/pending/de.yaml"].startswith(PENDING_DE)
    reply = host.replies(3)[0]
    assert "review queue" in reply and "every day" in reply and "Nothing is published" in reply


@pytest.mark.parametrize("change,fragment", [
    ({"stream": "http://stream.example.de/a.m3u8"}, "https"),
    ({"stream": "https://203.0.113.5/live/a.m3u8"}, "IP address"),
    ({"stream": "https://stream.example.de/a.m3u8?token=abc"}, "token"),
    ({"stream": "https://user:pw@stream.example.de/a.m3u8"}, "credentials"),
    ({"stream": "https://stream.example.de/page.html"}, ".m3u8"),
    ({"stream": "https://mjh.nz/x.m3u8"}, "restream"),
    ({"evidence": ""}, "evidence link is missing"),
    ({"evidence": "https://other-site.com/live"}, "own domain"),
    ({"evidence": "https://www.facebook.com/x/live", "website": "https://www.facebook.com/x"}, "own pages"),
    ({"country": "zz"}, "country"),
    ({"language": "klingon"}, "language"),
    ({"category": "Adult"}, "categories"),
    ({"name": "Free XXX Channel"}, "Adult"),
    ({"confirm": "- [x] a\n- [ ] b\n- [x] c"}, "confirmations"),
])
def test_invalid_suggestions_get_an_explanation_and_no_commit(change, fragment):
    host = Host()
    host.issues = [user_issue(3, form(**change))]
    run(host)
    assert host.commits == [] and fragment in host.replies(3)[0]


def test_suggestion_dedup_by_url_name_and_rejected():
    for change, word in [({"stream": "https://LIVE-hls-apps-aje-fa.getaj.net:443/AJE/index.m3u8#x", "country": "qa",
                           "language": "en", "website": "https://www.aljazeera.com", "evidence": "https://www.aljazeera.com/live"},
                          "already in the catalogue"),
                         ({"name": "qatar   television 2", "country": "qa", "language": "ar", "category": "General",
                           "stream": "https://qmc.qa/x.m3u8", "website": "https://www.qmc.qa", "evidence": "https://www.qmc.qa/l"},
                          "already waiting"),
                         ({"stream": "https://restream.example.de/live/index.m3u8"}, "rejected because: Third-party restream")]:
        host = Host()
        host.issues = [user_issue(3, form(**change))]
        run(host)
        assert host.commits == [] and word in host.replies(3)[0]


def test_suggestion_idempotent_and_reevaluated_after_edit():
    host = Host()
    host.issues = [user_issue(3, form(country="zz"))]
    _, ledger = run(host)
    run(host, ledger)
    assert len(host.replies(3)) == 1
    host.issues[0]["body"] = form()
    run(host, ledger)
    assert len(host.commits) == 1 and len(host.replies(3)) == 2
    run(host, ledger)
    run(host, Mem())  # even without the ledger the issue's own record is recognised, not added twice
    assert len(host.commits) == 1


def test_suggestion_found_by_form_markers_without_label_and_bots_ignored():
    host = Host()
    host.issues = [user_issue(3, form(), labels=()), user_issue(4, form(name="Other One"), login="spam[bot]")]
    host.issues[1]["user"]["type"] = "Bot"
    run(host)
    assert [c["message"] for c in host.commits] == ["Add suggested channel example-news"]


def test_suggestions_wait_when_catalogue_model_lacks_method():
    host = Host({**FILES, "nexonic/models.py": 'method: Literal["manual"]\n'})
    host.issues = [user_issue(3, form())]
    report, ledger = run(host)
    assert host.commits == [] and host.replies(3) == [] and any("community-suggestion" in f for f in report.flagged)
    assert ledger.get("iptv_review")["sug"] == {}


def test_suggestion_id_collision_gets_country_suffix():
    host = Host()
    host.files["data/pending/de.yaml"] = PENDING_DE.replace("de-nsfw", "example-news").replace("Qatar Television 2", "Another")
    host.trees["t0"] = dict(host.files)
    host.issues = [user_issue(3, form())]
    run(host)
    assert yaml.safe_load(host.files["data/pending/de.yaml"])["channels"][-1]["id"] == "example-news-de"


def test_suggestion_record_quoting_round_trips():
    sug = iptv.validate_suggestion(form(name="L'Équipe: \"live\" #1"), iptv.parse_reference(COUNTRIES, REFERENCE))
    assert sug.problems == []
    rec = iptv.suggestion_record(sug.fields, "2026-10-26", "https://github.com/o/r/issues/1", "lequipe-live")
    assert iptv._load_block(iptv.suggestion_block(rec)) == rec


# ------------------------------------------------------------------ broken-stream reports
def test_broken_report_reply_and_label_without_data_edits():
    host = Host()
    body = "### Channel name or id\n\ndw-english\n\n### Country you are watching from\n\nde\n\n### What happens?\n\nblack"
    host.issues = [user_issue(4, body, labels=("broken-stream",))]
    run(host)
    reply = host.replies(4)[0]
    assert "intermittent" in reply and "2026-10-27T11:04:27Z" in reply and "70% of its last 14" in reply
    assert ("POST", f"/repos/{REPO}/issues/4/labels", {"labels": ["stream-report"]}) in host.writes
    assert host.commits == []
    run(host)
    assert len(host.replies(4)) == 1


def test_broken_report_ambiguous_and_unknown():
    host = Host()
    mk = lambda q: f"### Channel name or id\n\n{q}\n\n### What happens?\n\nx"  # noqa: E731
    host.issues = [user_issue(4, mk("ARTE"), labels=("broken-stream",)), user_issue(5, mk("nothing at all"), labels=())]
    run(host)
    assert "`arte-de`" in host.replies(4)[0] and "`arte-fr`" in host.replies(4)[0]
    assert "could not match" in host.replies(5)[0]


# ------------------------------------------------------------------ scope, config, wording
def test_no_writes_outside_the_configured_repo_and_scope_guard():
    host = Host()
    host.issues = [review_issue(), user_issue(3, form())]
    host.comments[7] = [owner_comment(1, "reject qatar-tv-2 not official")]
    run(host)
    assert host.writes and all(p.startswith(f"/repos/{REPO}/") for _, p, _ in host.writes)
    api = iptv.Scoped(GitHub("t", transport=httpx.MockTransport(host.handler)), REPO)
    for bad in ("/repos/owner/other/issues", f"/repos/{REPO}-x/issues", f"/repos/{REPO}/../other/issues", "/user"):
        with pytest.raises(PermissionError):
            api.request("POST", bad, json={})
        with pytest.raises(PermissionError):
            api.get(bad)


def test_foreign_repository_refused_and_off_by_default():
    with pytest.raises(ValueError):
        run(Host(), cfg={"repo": "someone/else"})
    host = Host()
    bot = Sentinel(GitHub("t", transport=httpx.MockTransport(host.handler)),
                   {"owner": "owner", "iptv_review": {"repo": REPO}}, now=MONDAY, ledger=Mem())
    bot.iptv_review()
    bot2 = Sentinel(GitHub("t", transport=httpx.MockTransport(host.handler)),
                    {"owner": "owner", "iptv_review": {"enabled": True, "repo": "other/iptv"}}, now=MONDAY, ledger=Mem())
    bot2.iptv_review()
    assert host.writes == [] and bot2.report.errors and "does not belong" in bot2.report.errors[0]
    bot3 = Sentinel(GitHub("t", transport=httpx.MockTransport(host.handler)),
                    {"owner": "owner", "iptv_review": {"enabled": True, "repo": REPO}}, now=MONDAY, ledger=Mem(), home=False)
    bot3.iptv_review()
    assert host.writes == []


def test_failures_are_reported_and_fail_closed():
    host = Host({**FILES, "data/pending/qa.yaml": "channels:\n- id: a\n  name: [unclosed\n"})
    host.issues = [review_issue(), user_issue(3, form())]
    host.comments[7] = [owner_comment(1, "reject qatar-tv-2 not official")]
    report, _ = run(host)
    assert host.commits == [] and report.errors


def test_posted_text_has_no_forbidden_words():
    host = Host()
    host.issues = [review_issue(), user_issue(3, form()), user_issue(4, form(country="zz")),
                   user_issue(5, "### Channel name or id\n\ndw-english\n\n### What happens?\n\nx", labels=("broken-stream",))]
    host.comments[7] = [owner_comment(1, "approve qatar-tv-quran\nreject qatar-tv-2 no\nreject zzz-zzz because")]
    run(host)
    run(Host(), cfg={})
    texts = [c["body"] for cs in host.comments.values() for c in cs if c["user"]["login"] == "x[bot]"]
    texts += [i["body"] for i in host.issues if i["number"] >= 50]
    texts += [c["message"] for c in host.commits]
    texts.append(iptv.render_review([], 10))
    assert texts and not [t for t in texts if BANNED.search(t)]
    assert all("Co-authored" not in c["message"] for c in host.commits)


def test_sentinel_toml_enables_for_the_owner():
    import tomllib
    from pathlib import Path
    cfg = tomllib.loads((Path(__file__).resolve().parents[1] / "sentinel.toml").read_text())["iptv_review"]
    assert cfg["enabled"] is True and cfg["repo"] == "Shahrukhalid/nexonic-iptv" and cfg["owner_login"] == "Shahrukhalid"


def test_reference_parser_reads_real_style_tables():
    ref = iptv.parse_reference(COUNTRIES, REFERENCE)
    assert "de" in ref.countries and {"de", "en"} <= ref.languages and "news" in ref.categories
    with pytest.raises(iptv.RecordError):
        iptv.parse_reference("name,code\n", "")
