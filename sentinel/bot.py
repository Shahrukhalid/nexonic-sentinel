"""What Nexonic Sentinel does on every run, for every repository the GitHub App is installed on."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import knowledge
from .github import GitHub, GitHubError
from .llm import LLMError, OpenRouter

log = logging.getLogger("sentinel")

FOOTER = ("\n\n<sub>I'm Nexonic Sentinel, an automated assistant. Add the `bot-off` label "
          "(or write `bot-off` in the post) to stop me on this thread.</sub>")
SENSITIVE = re.compile(r"\b(security|vulnerab\w*|exploit|cve-\d|leak\w*|password|token|secret key|lawsuit|legal|"
                       r"licen[cs]e violation|dmca|copyright claim)\b", re.I)
LABEL_RULES = {
    "bug": re.compile(r"\b(bug|error|crash\w*|fails?|failing|broken|exception|traceback|doesn'?t work)\b", re.I),
    "enhancement": re.compile(r"\b(feature request|add support|would be (nice|great)|enhancement|proposal|suggest\w*)\b", re.I),
    "question": re.compile(r"(\bhow (do|can|to)\b|\bquestion\b|\bis it possible\b)", re.I),
    "documentation": re.compile(r"\b(docs?|documentation|readme|typo)\b", re.I),
}
NEW_CONTRIBUTOR = {"FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER", "NONE"}
TEST_TAG = "[sentinel-test]"  # lets the owner try the bot on their own post
BUMP = re.compile(r"from v?(\d+)\.(\d+)\.(\d+)\S* to v?(\d+)\.(\d+)\.(\d+)", re.I)

DISCUSSIONS_Q = """query($owner:String!,$name:String!){repository(owner:$owner,name:$name){hasDiscussionsEnabled
 discussions(first:25,orderBy:{field:CREATED_AT,direction:DESC}){nodes{id number title body createdAt url isAnswered
 locked author{login __typename} category{name isAnswerable} comments(first:40){nodes{body author{login}}}}}}}"""


def marker(kind: str) -> str:
    return f"<!-- nexonic-sentinel:{kind} -->"


def is_bot(user: dict | None) -> bool:
    if not user:
        return True
    return user.get("type") == "Bot" or user.get("__typename") == "Bot" or str(user.get("login", "")).endswith("[bot]")


RANK = {"patch": 0, "minor": 1, "major": 2}
BUMP_LINE = re.compile(r"^\s*(?:Updates|Bumps)\b.*$", re.I | re.M)


def _one_kind(m: re.Match) -> str | None:
    a, b = tuple(map(int, m.groups()[:3])), tuple(map(int, m.groups()[3:]))
    if b <= a:
        return None
    if b[0] != a[0]:
        return "major"
    if b[1] != a[1]:
        return "minor" if a[0] > 0 else "major"  # 0.x minor bumps can break
    return "patch"


def bump_kind(title: str, body: str = "") -> str | None:
    """Severity of a Dependabot update. Grouped PRs have no versions in the title, so read the
    "Updates `x` from A to B" lines in the body and take the biggest jump."""
    found = list(BUMP.finditer(title or ""))
    if not found:
        for line in BUMP_LINE.findall(body or ""):
            found.extend(BUMP.finditer(line))
    kinds = [_one_kind(m) for m in found]
    if not kinds or None in kinds:
        return None
    return max(kinds, key=RANK.__getitem__)


@dataclass
class Report:
    actions: list[str] = field(default_factory=list)
    flagged: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def act(self, text: str) -> None:
        log.info(text)
        self.actions.append(text)


class Sentinel:
    def __init__(self, gh: GitHub, cfg: dict[str, Any], llm: OpenRouter | None = None, dry_run: bool = False,
                 now: datetime | None = None) -> None:
        self.gh, self.cfg, self.llm, self.dry = gh, cfg, llm, dry_run
        self.owner = cfg["owner"]
        self.now = now or datetime.now(timezone.utc)
        self.since = self.now - timedelta(hours=int(cfg.get("lookback_hours", 72)))
        self.report = Report()
        self._docs: dict[str, dict[str, str]] = {}

    # ------------------------------------------------------------------ writes (respect dry run)
    def _write(self, description: str, fn: Callable, *args, **kwargs) -> None:
        self.report.act(("[dry run] " if self.dry else "") + description)
        if not self.dry:
            fn(*args, **kwargs)

    # ------------------------------------------------------------------ entry point
    def run(self) -> Report:
        if not self.cfg.get("enabled", True):
            self.report.act("Disabled in sentinel.toml; nothing done.")
            return self.report
        skip = set(self.cfg.get("skip_repos", []))
        for repo in self.gh.repos():
            if repo["name"] in skip:
                continue
            for step in (self.issues, self.pulls, self.discussions):
                try:
                    step(repo)
                except (GitHubError, LLMError) as exc:
                    msg = f"{repo['full_name']} {step.__name__}: {exc}"
                    log.warning(msg)
                    self.report.errors.append(msg)
        return self.report

    def _recent(self, stamp: str) -> bool:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")) >= self.since

    def _docs_for(self, repo: dict) -> dict[str, str]:
        name = repo["full_name"]
        if name not in self._docs:
            paths = knowledge.doc_paths(self.gh.tree(name, repo.get("default_branch", "main")))
            self._docs[name] = {p: t for p in paths if (t := self.gh.raw_file(name, p))}
        return self._docs[name]

    def _doc_links(self, repo: dict, paths: list[str]) -> str:
        branch = repo.get("default_branch", "main")
        uniq = list(dict.fromkeys(paths))
        return "\n".join(f"- [{p}](https://github.com/{repo['full_name']}/blob/{branch}/{p})" for p in uniq)

    # ------------------------------------------------------------------ issues
    def issues(self, repo: dict) -> None:
        name = repo["full_name"]
        if not repo.get("has_issues", True):
            return
        existing = {lab["name"].lower(): lab["name"] for lab in self.gh.paged(f"/repos/{name}/labels")}
        for issue in self.gh.paged(f"/repos/{name}/issues", state="open", since=self.since.isoformat(), limit=100):
            if "pull_request" in issue or not self._recent(issue["created_at"]):
                continue
            user = issue.get("user") or {}
            labels = {lab["name"].lower() for lab in issue.get("labels", [])}
            text = f"{issue.get('title', '')}\n{issue.get('body') or ''}"
            owner_post = user.get("login") == self.owner and TEST_TAG not in text.lower()
            if is_bot(user) or owner_post or "bot-off" in labels or "bot-off" in text:
                continue
            comments = self.gh.paged(f"/repos/{name}/issues/{issue['number']}/comments", limit=100)
            if any(marker("triage") in (c.get("body") or "") for c in comments):
                continue
            wanted = [existing[k] for k, rx in LABEL_RULES.items() if rx.search(text) and k in existing and k not in labels]
            if wanted:
                self._write(f"{name}#{issue['number']}: label {', '.join(wanted)}",
                            self.gh.add_labels, name, issue["number"], wanted)
            body = self._triage_text(repo, issue, text, wanted)
            self._write(f"{name}#{issue['number']}: triage comment", self.gh.comment, name, issue["number"], body)

    def _triage_text(self, repo: dict, issue: dict, text: str, labels: list[str]) -> str:
        who = issue["user"]["login"]
        lines = [marker("triage"), f"Thanks for opening this, @{who}!"]
        if SENSITIVE.search(text):
            lines.append("This looks security-related. If it is a vulnerability, please don't share details publicly: "
                         "use the repository's private vulnerability reporting (see SECURITY.md). "
                         f"@{self.owner} has been notified.")
            self.report.flagged.append(f"{repo['full_name']}#{issue['number']} (possible security report)")
        else:
            if "bug" in [label.lower() for label in labels] and len(issue.get("body") or "") < 200:
                lines.append("To help reproduce it, please add: the version or commit you're using, your OS, "
                             "the exact steps, what you expected, and any error output or logs.")
            hits = knowledge.best_excerpts(text, self._docs_for(repo), k=3)
            if hits:
                lines.append("These docs may already cover it:\n" + self._doc_links(repo, [p for p, _ in hits]))
            lines.append(f"@{self.owner} will take a look.")
        return "\n\n".join(lines) + FOOTER

    # ------------------------------------------------------------------ pull requests
    def pulls(self, repo: dict) -> None:
        name = repo["full_name"]
        for pr in self.gh.paged(f"/repos/{name}/pulls", state="open", limit=100):
            user = pr.get("user") or {}
            if pr.get("draft") or "bot-off" in {lab["name"].lower() for lab in pr.get("labels", [])}:
                continue
            if user.get("login") == "dependabot[bot]":
                self._dependabot(repo, pr)
            elif not is_bot(user) and user.get("login") != self.owner and self._recent(pr["created_at"]):
                self._welcome_pr(repo, pr)

    def _has_marker(self, repo: str, number: int, kind: str) -> bool:
        comments = self.gh.paged(f"/repos/{repo}/issues/{number}/comments", limit=100)
        return any(marker(kind) in (c.get("body") or "") for c in comments)

    def _welcome_pr(self, repo: dict, pr: dict) -> None:
        name, n = repo["full_name"], pr["number"]
        if pr.get("author_association") not in NEW_CONTRIBUTOR or self._has_marker(name, n, "welcome"):
            return
        body = (f"{marker('welcome')}Thank you for your first contribution, @{pr['user']['login']}! "
                f"@{self.owner} will review it soon. Checks for pull requests from forks only start after a maintainer "
                "approves them, so a short wait is normal. Please make sure the tests pass and describe how you tested "
                "the change." + FOOTER)
        self._write(f"{name}#{n}: welcome new contributor", self.gh.comment, name, n, body)
        self.report.flagged.append(f"{name}#{n} (new contributor PR to review)")

    def _checks_green(self, repo: str, sha: str) -> bool:
        runs = self.gh.get(f"/repos/{repo}/commits/{sha}/check-runs", per_page=100).get("check_runs", [])
        if not runs:
            return False
        ok = all(r["status"] == "completed" and r["conclusion"] in ("success", "skipped", "neutral") for r in runs)
        status = self.gh.get(f"/repos/{repo}/commits/{sha}/status")
        return ok and (status.get("total_count", 0) == 0 or status.get("state") == "success")

    def _dependabot(self, repo: dict, pr: dict) -> None:
        name, n = repo["full_name"], pr["number"]
        kind = bump_kind(pr.get("title", ""), pr.get("body") or "")
        allowed = set(self.cfg.get("automerge", {}).get("dependabot", ["patch", "minor"]))
        if kind not in allowed:
            if not self._has_marker(name, n, "major"):
                self._write(f"{name}#{n}: flag {kind or 'unrecognised'} dependency update for review", self.gh.comment,
                            name, n, f"{marker('major')}@{self.owner} this is a {kind or 'non-standard'} update, so I "
                            "won't merge it automatically. Please review the release notes." + FOOTER)
            self.report.flagged.append(f"{name}#{n} ({kind or 'unrecognised'} dependency update)")
            return
        detail = self.gh.get(f"/repos/{name}/pulls/{n}")
        state = detail.get("mergeable_state")
        green = self._checks_green(name, pr["head"]["sha"])
        if state == "behind" or (state in ("blocked", "unstable") and not green and self._checks_done(name, pr["head"]["sha"])):
            # behind main, or failing checks that a rebase onto a fixed main may resolve: ask once
            if not self._has_marker(name, n, "rebase"):
                self._write(f"{name}#{n}: ask Dependabot to rebase", self.gh.comment, name, n,
                            f"@dependabot rebase\n\n{marker('rebase')}")
            return
        if detail.get("mergeable") and state in ("clean", "has_hooks") and green:
            self._write(f"{name}#{n}: squash-merge {kind} update ({pr['title']})", self.gh.request, "PUT",
                        f"/repos/{name}/pulls/{n}/merge",
                        json={"merge_method": "squash", "sha": pr["head"]["sha"]})

    def _checks_done(self, repo: str, sha: str) -> bool:
        runs = self.gh.get(f"/repos/{repo}/commits/{sha}/check-runs", per_page=100).get("check_runs", [])
        return bool(runs) and all(r["status"] == "completed" for r in runs)

    # ------------------------------------------------------------------ discussions
    def discussions(self, repo: dict) -> None:
        name = repo["full_name"]
        if not repo.get("has_discussions"):
            return
        owner, short = name.split("/", 1)
        data = self.gh.graphql(DISCUSSIONS_Q, owner=owner, name=short)["repository"]
        if not data.get("hasDiscussionsEnabled"):
            return
        for d in data["discussions"]["nodes"]:
            author = d.get("author") or {}
            owner_post = author.get("login") == self.owner and TEST_TAG not in f"{d['title']} {d.get('body') or ''}".lower()
            if (d["locked"] or d["isAnswered"] or not self._recent(d["createdAt"]) or is_bot(author)
                    or owner_post or "bot-off" in (d.get("body") or "")):
                continue
            if any(marker("discussion") in (c.get("body") or "") for c in d["comments"]["nodes"]):
                continue
            if any((c.get("author") or {}).get("login") == self.owner for c in d["comments"]["nodes"]):
                continue  # the owner is already handling it
            body = self._discussion_reply(repo, d)
            if body:
                self._write(f"{name} discussion #{d['number']}: reply", self.gh.discussion_comment, d["id"], body)

    def _discussion_reply(self, repo: dict, d: dict) -> str | None:
        question = f"{d['title']}\n\n{d.get('body') or ''}"
        who = d["author"]["login"]
        if not d["category"].get("isAnswerable"):
            if d["category"]["name"].lower().startswith("idea"):
                return (f"{marker('discussion')}Thanks for the idea, @{who}! @{self.owner} reads every suggestion; "
                        "a 👍 from others helps show how many people want it." + FOOTER)
            return None
        if SENSITIVE.search(question):
            self.report.flagged.append(f"{repo['full_name']} discussion #{d['number']} (sensitive topic)")
            return (f"{marker('discussion')}Thanks, @{who}. This touches on security or legal matters, so I've left it "
                    f"for @{self.owner} to answer personally." + FOOTER)
        excerpts = knowledge.best_excerpts(question, self._docs_for(repo))
        cap = int(self.cfg.get("max_llm_calls_per_run", 6))
        if self.llm is not None and excerpts and self.llm.calls < cap:
            try:
                reply = self.llm.answer(repo["full_name"], question, excerpts)
            except LLMError as exc:
                self.report.errors.append(f"{repo['full_name']} discussion #{d['number']}: {exc}")
                reply = {"confident": False}
            if reply.get("confident"):
                return (f"{marker('discussion')}Hi @{who}, here's what the project's documentation says:\n\n"
                        f"{reply['answer']}\n\n**Sources**\n{self._doc_links(repo, reply['sources'])}\n\n"
                        f"If this solves it, please mark it as the answer. @{self.owner} will double-check." + FOOTER)
        self.report.flagged.append(f"{repo['full_name']} discussion #{d['number']} (needs an answer)")
        return (f"{marker('discussion')}Thanks for asking, @{who}! I couldn't find a clear answer in the docs, so I've "
                f"flagged this for @{self.owner}." + FOOTER)

    # ------------------------------------------------------------------ weekly digest
    def digest(self) -> None:
        """Monday summary issue in the bot's own repository, so nothing waiting on the owner gets lost."""
        home = f"{self.owner}/{self.cfg.get('home_repo', 'nexonic-sentinel')}"
        week = self.now.strftime("%G-W%V")
        title = f"Weekly digest {week}"
        existing = self.gh.paged(f"/repos/{home}/issues", state="all", labels="digest", limit=20)
        if any(i["title"] == title for i in existing):
            return
        rows = []
        skip = set(self.cfg.get("skip_repos", []))
        for repo in self.gh.repos():
            if repo["name"] in skip:
                continue
            name = repo["full_name"]
            issues = [i for i in self.gh.paged(f"/repos/{name}/issues", state="open", limit=300)
                      if "pull_request" not in i]
            prs = self.gh.paged(f"/repos/{name}/pulls", state="open", limit=100)
            rows.append(f"| [{repo['name']}](https://github.com/{name}) | {repo.get('stargazers_count', 0)} | "
                        f"{len(issues)} | {len(prs)} |")
        body = ("Open work across your repositories.\n\n| Repository | Stars | Open issues | Open PRs |\n|---|---|---|---|\n"
                + "\n".join(rows) + "\n\n**Waiting on you this run**\n"
                + ("\n".join(f"- {f}" for f in self.report.flagged) or "- Nothing") + FOOTER)
        self._write(f"{home}: create {title}", self.gh.request, "POST", f"/repos/{home}/issues",
                    json={"title": title, "body": f"@{self.owner}\n\n{body}", "labels": ["digest"]})
