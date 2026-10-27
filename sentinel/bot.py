"""What Nexonic Sentinel does on every run, for every repository the GitHub App is installed on."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import contrib, growth, knowledge, review
from . import settings as repo_settings
from .github import GitHub, GitHubError
from .ledger import Ledger
from .ledger import key as lkey
from .llm import LLMError, LLMQuotaExceeded, OpenRouter, json_from

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
DEPENDABOT = "dependabot[bot]"
TEST_TAG = "[sentinel-test]"  # lets the owner try the bot on their own post
V = r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?"  # 1.2.3, 1.2 or just 4 (GitHub Actions)
BUMP = re.compile(rf"from {V}\S* to {V}", re.I)

DISCUSSIONS_Q = """query($owner:String!,$name:String!){repository(owner:$owner,name:$name){hasDiscussionsEnabled
 discussions(first:25,orderBy:{field:CREATED_AT,direction:DESC}){nodes{id number title body createdAt url isAnswered
 locked author{login __typename} category{name isAnswerable} comments(first:40){nodes{body author{login __typename}}}}}}}"""


def marker(kind: str) -> str:
    return f"<!-- nexonic-sentinel:{kind} -->"


def is_bot(user: dict | None) -> bool:
    if not user:
        return True
    return user.get("type") == "Bot" or user.get("__typename") == "Bot" or str(user.get("login", "")).endswith("[bot]")


def bot_marked(comments: list[dict], kind: str, author_key: str = "user") -> bool:
    """True when a bot already left this marker. Markers typed by people are ignored, so nobody can
    silence a warning (for example on a major dependency update) by pasting the hidden comment."""
    return any(marker(kind) in (c.get("body") or "") and bool(c.get(author_key)) and is_bot(c.get(author_key))
               for c in comments)


RANK = {"patch": 0, "minor": 1, "major": 2}
BUMP_LINE = re.compile(r"^\s*(?:Updates|Bumps)\b.*$", re.I | re.M)


def _one_kind(m: re.Match) -> str | None:
    a, b = (tuple(int(x or 0) for x in m.groups()[:3]), tuple(int(x or 0) for x in m.groups()[3:]))
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
    private: bool = False  # other people's installations: never print their repository names in public logs

    def act(self, text: str) -> None:
        if not self.private:
            log.info(text)
        self.actions.append(text)


class Sentinel:
    def __init__(self, gh: GitHub, cfg: dict[str, Any], llm: OpenRouter | None = None, dry_run: bool = False,
                 now: datetime | None = None, ledger: Ledger | None = None, home: bool = True,
                 ai_cap: int | None = None) -> None:
        self.gh, self.cfg, self.llm, self.dry, self.ledger, self.home = gh, cfg, llm, dry_run, ledger, home
        self.owner = cfg["owner"]
        # Mentioning the owner sends them a GitHub notification email on every comment; off by default.
        mention = bool(cfg.get("mention_owner", False)) and home
        self.them = f"@{self.owner}" if mention else "the maintainer"
        self.They = f"@{self.owner}" if mention else "The maintainer"
        self.now = now or datetime.now(timezone.utc)
        self.since = self.now - timedelta(hours=int(cfg.get("lookback_hours", 72)))
        self.report = Report(private=not home)
        self.base = repo_settings.home_defaults(cfg) if home else dict(repo_settings.PUBLIC_DEFAULTS)
        self.ai_cap = ai_cap
        self.ai_start = llm.calls if llm else 0
        self._docs: dict[str, dict[str, str]] = {}
        self._settings: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ helpers
    def _write(self, description: str, fn: Callable, *args, **kwargs) -> Any:
        self.report.act(("[dry run] " if self.dry else "") + description)
        if not self.dry:
            return fn(*args, **kwargs)
        return None

    def settings(self, repo: dict) -> dict[str, Any]:
        name = repo["full_name"]
        if name not in self._settings:
            self._settings[name] = repo_settings.parse(self.gh.raw_file(name, repo_settings.FILE), self.base)
        return self._settings[name]

    def _ai_ok(self, s: dict[str, Any]) -> bool:
        if not s.get("ai") or self.llm is None or not self.llm.available():
            return False
        return self.ai_cap is None or self.llm.calls - self.ai_start < self.ai_cap

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

    def _error(self, repo: dict, where: str, exc: Exception) -> None:
        detail = f"{repo['full_name']} {where}: {exc}" if self.home else f"{where}: {exc.__class__.__name__}"
        if self.home:
            log.warning(detail)
        self.report.errors.append(detail)

    # ------------------------------------------------------------------ entry point
    def run(self) -> Report:
        if not self.cfg.get("enabled", True):
            self.report.act("Disabled in sentinel.toml; nothing done.")
            return self.report
        skip = set(self.cfg.get("skip_repos", [])) if self.home else set()
        for repo in self.gh.repos():
            if repo["name"] in skip:
                continue
            try:
                s = self.settings(repo)
            except GitHubError as exc:
                self._error(repo, "settings", exc)
                continue
            if not s["enabled"]:
                continue
            steps: list[Callable] = []
            if s["triage"]:
                steps.append(self.issues)
            steps.append(self.pulls)
            if s["discussions"]:
                steps.append(self.discussions)
            if s["ai"] and s["code_scan"]:
                steps.append(self.code_scan)
            for step in steps:
                try:
                    step(repo)
                except LLMQuotaExceeded:
                    self.report.flagged.append("Automated reviews paused until the next run (daily limit reached)")
                except (GitHubError, LLMError) as exc:
                    self._error(repo, step.__name__, exc)
        return self.report

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
            if labels & {"digest", "ledger", "code-health", "contribute", "growth"}:
                continue
            comments = self.gh.paged(f"/repos/{name}/issues/{issue['number']}/comments", limit=100)
            if bot_marked(comments, "triage"):
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
                         f"{self.They} has been notified.")
            self.report.flagged.append(f"{repo['full_name']}#{issue['number']} (possible security report)")
        else:
            if "bug" in [label.lower() for label in labels] and len(issue.get("body") or "") < 200:
                lines.append("To help reproduce it, please add: the version or commit you're using, your OS, "
                             "the exact steps, what you expected, and any error output or logs.")
            hits = knowledge.best_excerpts(text, self._docs_for(repo), k=3)
            if hits:
                lines.append("These docs may already cover it:\n" + self._doc_links(repo, [p for p, _ in hits]))
            lines.append(f"{self.They} will take a look.")
        return "\n\n".join(lines) + FOOTER

    # ------------------------------------------------------------------ pull requests
    def pulls(self, repo: dict) -> None:
        name = repo["full_name"]
        s = self.settings(repo)
        for pr in self.gh.paged(f"/repos/{name}/pulls", state="open", limit=100):
            user = pr.get("user") or {}
            if pr.get("draft") or "bot-off" in {lab["name"].lower() for lab in pr.get("labels", [])}:
                continue
            if user.get("login") == DEPENDABOT and is_bot(user):
                self._dependabot(repo, pr, s)
                continue
            if is_bot(user):
                continue
            if s["welcome"] and user.get("login") != self.owner and self._recent(pr["created_at"]):
                self._welcome_pr(repo, pr)
            if s["pr_review"] and self._recent(pr.get("updated_at") or pr["created_at"]) and self._ai_ok(s):
                self._review_pr(repo, pr, s)

    def _has_marker(self, repo: str, number: int, kind: str) -> bool:
        comments = self.gh.paged(f"/repos/{repo}/issues/{number}/comments", limit=100)
        return bot_marked(comments, kind)

    def _welcome_pr(self, repo: dict, pr: dict) -> None:
        name, n = repo["full_name"], pr["number"]
        if pr.get("author_association") not in NEW_CONTRIBUTOR or self._has_marker(name, n, "welcome"):
            return
        tested = re.search(r"\btest", pr.get("body") or "", re.I)
        body = (f"{marker('welcome')}Thank you for your first contribution, @{pr['user']['login']}! "
                f"{self.They} will review it soon. Checks for pull requests from forks only start after a maintainer "
                "approves them, so a short wait is normal."
                + ("" if tested else " Please make sure the tests pass and describe how you tested the change.")
                + FOOTER)
        self._write(f"{name}#{n}: welcome new contributor", self.gh.comment, name, n, body)
        self.report.flagged.append(f"{name}#{n} (new contributor PR to review)")

    def _review_pr(self, repo: dict, pr: dict, s: dict[str, Any]) -> None:
        """AI bug review, once per head commit. Posted as a plain review comment: never approves or blocks."""
        name, n, sha = repo["full_name"], pr["number"], pr["head"]["sha"]
        mark = marker(f"review:{sha[:12]}")
        attempted = lkey("review", name, sha)
        if self.ledger is not None and self.ledger.get(attempted):
            return
        reviews = self.gh.paged(f"/repos/{name}/pulls/{n}/reviews", limit=100)
        if any(mark in (r.get("body") or "") for r in reviews):
            return
        rc = self.cfg.get("review", {})
        files = self.gh.paged(f"/repos/{name}/pulls/{n}/files", limit=100)
        code = [f for f in files if review.reviewable(f["filename"]) and f.get("patch")]
        if not code:
            return
        if len(code) > int(rc.get("max_files", 30)) or sum(len(f["patch"]) for f in code) > int(rc.get("max_patch_chars", 30000)):
            return  # too large for a useful automatic review
        prompt, lines = review.review_prompt(pr, code)
        reply = self.llm.complete(review.REVIEW_SYSTEM, prompt, 1800)  # quota errors propagate: retried next run
        if self.ledger is not None:
            self.ledger.set(attempted, self.now.date().isoformat())
        try:
            raw = json_from(reply)
        except ValueError:
            return
        findings = review.clean_findings(raw, lines, rc.get("min_severity", "medium"), 5)
        runs = self._check_runs(name, sha)
        failing = [r for r in runs if r.get("conclusion") in ("failure", "timed_out", "action_required")]
        if not findings and not failing and not rc.get("post_when_clean", True):
            return
        link = lambda f: f"https://github.com/{name}/blob/{sha}/{f['file']}#L{f['line']}"  # noqa: E731
        parts = [mark, f"### Automated review of `{sha[:7]}`"]
        if findings:
            parts.append(f"I found {len(findings)} likely bug{'s' if len(findings) > 1 else ''} in the changed lines:")
            parts.append(review.render(findings, link))
        else:
            parts.append("I didn't spot likely bugs in the changed lines.")
        if failing:
            parts.append("**Failing checks:** " + ", ".join(f"[{r['name']}]({r.get('html_url', '')})" for r in failing[:6]))
        parts.append("<sub>These findings are automated and based only on the diff, so treat them as hints "
                     "and check each one against the code. "
                     "Reply here if something is wrong.</sub>")
        body = "\n\n".join(parts) + FOOTER
        self._write(f"{name}#{n}: review {sha[:7]} ({len(findings)} finding{'s' if len(findings) != 1 else ''})",
                    self.gh.request, "POST", f"/repos/{name}/pulls/{n}/reviews",
                    json={"commit_id": sha, "event": "COMMENT", "body": body})

    def _check_runs_full(self, repo: str, sha: str) -> tuple[list[dict], bool]:
        """Check runs plus whether the list is complete (fetched count matches GitHub's total_count, cap 500)."""
        out: list[dict] = []
        total: Any = None
        page = 1
        while len(out) < 500:
            data = self.gh.get(f"/repos/{repo}/commits/{sha}/check-runs", per_page=100, page=page)
            if total is None:
                total = data.get("total_count")
            items = data.get("check_runs", [])
            out.extend(items)
            if len(items) < 100:
                break
            page += 1
        complete = isinstance(total, int) and not isinstance(total, bool) and len(out) == total
        return out[:500], complete

    def _check_runs(self, repo: str, sha: str) -> list[dict]:
        return self._check_runs_full(repo, sha)[0]

    def _checks_green(self, repo: str, sha: str) -> bool:
        runs, complete = self._check_runs_full(repo, sha)
        if not runs or not complete:
            return False
        ok = all(r["status"] == "completed" and r["conclusion"] in ("success", "skipped", "neutral") for r in runs)
        status = self.gh.get(f"/repos/{repo}/commits/{sha}/status")
        return ok and (status.get("total_count", 0) == 0 or status.get("state") == "success")

    def _dependabot(self, repo: dict, pr: dict, s: dict[str, Any]) -> None:
        name, n = repo["full_name"], pr["number"]
        kind = bump_kind(pr.get("title", ""), pr.get("body") or "")
        allowed = set(s["dependabot"])
        if not allowed:
            return  # this repository has not opted in to automatic dependency handling
        if kind not in allowed:
            if not self._has_marker(name, n, "major"):
                self._write(f"{name}#{n}: flag {kind or 'unrecognised'} dependency update for review", self.gh.comment,
                            name, n, f"{marker('major')}This is a {kind or 'non-standard'} update, so I won't merge it "
                            f"automatically; {self.them} will review the release notes." + FOOTER)
            self.report.flagged.append(f"{name}#{n} ({kind or 'unrecognised'} dependency update)")
            return
        detail = self.gh.get(f"/repos/{name}/pulls/{n}")
        if not self._genuine_dependabot(name, n, pr, detail):
            self.report.flagged.append(f"{name}#{n} (Dependabot PR has commits from someone else or moved; not merged)")
            return
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

    def _genuine_dependabot(self, name: str, n: int, pr: dict, detail: dict) -> bool:
        """Only merge what Dependabot itself wrote: the PR (re-read from the API) is opened by dependabot[bot] from a
        branch of this repository, its head has not moved, and every commit on it is authored by Dependabot."""
        user = detail.get("user") or {}
        head = detail.get("head") or {}
        if user.get("login") != DEPENDABOT or user.get("type") != "Bot":
            return False
        if (head.get("repo") or {}).get("full_name") != name or head.get("sha") != pr["head"]["sha"]:
            return False
        count = detail.get("commits")
        if not isinstance(count, int) or isinstance(count, bool) or not 0 < count <= 250:
            return False  # unknown or beyond what the commits endpoint can list: cannot vouch for every commit
        commits = self.gh.paged(f"/repos/{name}/pulls/{n}/commits", limit=250)
        if len(commits) != count or commits[-1].get("sha") != pr["head"]["sha"]:
            return False
        return all(self._dependabot_commit(c) for c in commits)

    @staticmethod
    def _dependabot_commit(c: dict) -> bool:
        """Author attribution alone can be faked with an email address, so also require a verified signature made by
        GitHub itself (web-flow) or Dependabot."""
        verification = (c.get("commit") or {}).get("verification") or {}
        return ((c.get("author") or {}).get("login") == DEPENDABOT
                and (c.get("committer") or {}).get("login") in {"web-flow", DEPENDABOT}
                and verification.get("verified") is True)

    def _checks_done(self, repo: str, sha: str) -> bool:
        runs, complete = self._check_runs_full(repo, sha)
        return complete and bool(runs) and all(r["status"] == "completed" for r in runs)

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
            if bot_marked(d["comments"]["nodes"], "discussion", author_key="author"):
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
                return (f"{marker('discussion')}Thanks for the idea, @{who}! {self.They} reads every suggestion; "
                        "a 👍 from others helps show how many people want it." + FOOTER)
            return None
        if SENSITIVE.search(question):
            self.report.flagged.append(f"{repo['full_name']} discussion #{d['number']} (sensitive topic)")
            return (f"{marker('discussion')}Thanks, @{who}. This touches on security or legal matters, so I've left it "
                    f"for {self.them} to answer personally." + FOOTER)
        excerpts = knowledge.best_excerpts(question, self._docs_for(repo))
        if excerpts and self._ai_ok(self.settings(repo)):
            try:
                reply = self.llm.answer(repo["full_name"], question, excerpts)
            except LLMQuotaExceeded:
                raise
            except LLMError as exc:
                self._error(repo, f"discussion #{d['number']}", exc)
                reply = {"confident": False}
            if reply.get("confident"):
                return (f"{marker('discussion')}Hi @{who}, here's what the project's documentation says:\n\n"
                        f"{reply['answer']}\n\n**Sources**\n{self._doc_links(repo, reply['sources'])}\n\n"
                        f"If this solves it, please mark it as the answer. {self.They} will double-check." + FOOTER)
        self.report.flagged.append(f"{repo['full_name']} discussion #{d['number']} (needs an answer)")
        return (f"{marker('discussion')}Thanks for asking, @{who}! I couldn't find a clear answer in the docs, so I've "
                f"flagged this for {self.them}." + FOOTER)

    # ------------------------------------------------------------------ weekly code health scan
    def code_scan(self, repo: dict) -> None:
        """Once a week, read a few source files (a different slice each week) and report likely bugs in one issue."""
        if self.ledger is None or not repo.get("has_issues", True):
            return
        name, branch = repo["full_name"], repo.get("default_branch", "main")
        week = self.now.strftime("%G-W%V")
        done = lkey("scan", name)
        if self.ledger.get(done) == week:
            return
        s = self.settings(repo)
        sc = self.cfg.get("scan", {})
        candidates = sorted(p for p, size in self.gh.tree_entries(name, branch)
                            if review.scannable(p) and 200 <= size <= int(sc.get("max_file_bytes", 15000)))
        if not candidates:
            self.ledger.set(done, week)
            return
        per_week = min(int(sc.get("files_per_week", 4)), len(candidates))
        start = (int(self.now.strftime("%V")) * per_week) % len(candidates)
        chosen = [candidates[(start + i) % len(candidates)] for i in range(per_week)]
        previous = set()
        for issue in self.gh.paged(f"/repos/{name}/issues", state="all", labels="code-health", limit=30):
            previous.update(re.findall(r"<!-- fp:([0-9a-f]{10}) -->", issue.get("body") or ""))
        head = self.gh.get(f"/repos/{name}/commits/{branch}")["sha"]
        findings = []
        for path in chosen:
            if not self._ai_ok(s):
                return  # budget used up: try again on the next run, the week is not marked as done
            text = self.gh.raw_file(name, path)
            if not text:
                continue
            try:
                raw = json_from(self.llm.complete(review.SCAN_SYSTEM, f"File: {path}\n\n{review.number_lines(text)}", 1500))
            except LLMQuotaExceeded:
                raise
            except (LLMError, ValueError):
                continue  # one unreadable reply must not block the weekly scan
            valid = {path: set(range(1, len(text.splitlines()) + 1))}
            findings += review.clean_findings(raw, valid, sc.get("min_severity", "medium"), 3, default_file=path)
        self.ledger.set(done, week)
        findings = [f for f in findings if review.fingerprint(f["file"], f["title"]) not in previous]
        if not findings:
            self.report.act(f"{name}: code scan {week}, nothing new" if self.home else "code scan, nothing new")
            return
        link = lambda f: f"https://github.com/{name}/blob/{head}/{f['file']}#L{f['line']}"  # noqa: E731
        body = (f"{marker('code-health')}This week I read {len(chosen)} source files "
                f"({', '.join(f'`{p}`' for p in chosen)}) and found {len(findings)} likely "
                f"bug{'s' if len(findings) > 1 else ''}. Each week covers a different set of files.\n\n"
                + review.render(findings, link)
                + "\n\n<sub>These findings are automated, so check each one against the code before fixing it. "
                  "Close this issue once handled; findings already reported are not repeated.</sub>" + FOOTER)
        self._write(f"{name}: code health report {week} ({len(findings)} findings)", self.gh.request, "POST",
                    f"/repos/{name}/issues", json={"title": f"Code health report {week}", "body": body,
                                                   "labels": ["code-health"]})

    # ------------------------------------------------------------------ growth inbox (owner only)
    def growth(self) -> None:
        """Once a week: scout, backlinks and discussion drafts, all listed in ONE issue in the owner's own repository.

        Everything found about other projects is only ever written there. Foreign repositories are read through a
        read-only client, and the single write below goes to the home repository."""
        g = self.cfg.get("growth", {})
        if (not self.home or self.ledger is None or not self.cfg.get("enabled", True)
                or not isinstance(g, dict) or not g.get("enabled", True)):
            return
        week = self.now.strftime("%G-W%V")
        if self.ledger.get("growth_week") == week:
            return
        home = f"{self.owner}/{self.cfg.get('home_repo', 'nexonic-sentinel')}"
        title = f"Growth inbox {week}"
        issues = self.gh.paged(f"/repos/{home}/issues", state="all", labels="growth", limit=30)
        state = dict(self.ledger.get("backlinks", {}))
        for old in issues[:3]:  # the owner marks backlinks as submitted/declined in comments
            if old.get("comments"):
                growth.apply_commands(state, self.gh.paged(f"/repos/{home}/issues/{old['number']}/comments", limit=100),
                                      self.owner, self.now)
        if any(i["title"] == title for i in issues):
            self.ledger.set("growth_week", week)
            self.ledger.set("backlinks", state)
            return
        ro = growth.ReadOnly(self.gh)
        skip = set(self.cfg.get("skip_repos", []))
        mine = [r for r in self.gh.repos() if r["name"] not in skip and not r.get("private")]
        llm = self.llm if self._ai_ok(self.base) else None
        notes: list[str] = []
        hasher = lambda url: lkey("contrib", url)  # noqa: E731
        scout_text = back_text = disc_text = "Not enabled."
        found = 0

        c = self.cfg.get("contrib", {})
        if isinstance(c, dict) and c.get("enabled", True):
            try:
                seen_list = list(self.ledger.get("contrib_seen", []))
                seen = set(seen_list)
                picks = contrib.find(ro, llm, self.cfg, self.owner, self.now, seen, hasher)
                seen.update(hasher(p["url"]) for p in picks)
                self.ledger.set("contrib_seen", (seen_list + sorted(k for k in seen if k not in set(seen_list)))[-1500:])
                scout_text, found = contrib.render(picks), found + len(picks)
            except Exception as exc:  # noqa: BLE001 - one broken section must not stop the others
                notes.append("scout"), self.report.errors.append(f"growth scout: {exc}")
                scout_text = "Could not be fetched this week."

        b = self.cfg.get("backlinks", {})
        if isinstance(b, dict) and b.get("enabled", False):
            try:
                llm = self.llm if self._ai_ok(self.base) else None
                items = growth.find_backlinks(ro, llm, self.cfg, self.owner, self.now, state, mine)
                for it in items:
                    state[it["id"]] = {"s": "suggested", "at": self.now.date().isoformat()}
                back_text, found = growth.render_backlinks(items), found + len(items)
            except Exception as exc:  # noqa: BLE001
                notes.append("backlinks"), self.report.errors.append(f"growth backlinks: {exc}")
                back_text = "Could not be fetched this week."

        a = self.cfg.get("answers", {})
        if isinstance(a, dict) and a.get("enabled", False):
            try:
                dseen_list = list(self.ledger.get("disc_seen", []))
                dh = lambda url: lkey("disc", url)  # noqa: E731
                found_d = growth.find_discussions(ro, self.cfg, self.owner, self.now, set(dseen_list), dh, mine)
                items = []
                for d in found_d:
                    items.append({"discussion": d, "draft": self._help_draft(d)})
                self.ledger.set("disc_seen", (dseen_list + [dh(d["url"]) for d in found_d])[-1500:])
                disc_text, found = growth.render_discussions(items), found + len(items)
            except LLMQuotaExceeded:
                notes.append("discussion drafts (daily quota reached)")
                disc_text = "Not available this week."
            except Exception as exc:  # noqa: BLE001
                notes.append("discussions"), self.report.errors.append(f"growth discussions: {exc}")
                disc_text = "Could not be fetched this week."

        self.ledger.set("backlinks", growth.prune(state))
        if not found and notes:
            return  # nothing to show and something failed: try again on the next run
        self.ledger.set("growth_week", week)
        if not found:
            self.report.act(f"{home}: growth inbox {week}, nothing new")
            return
        body = marker("growth") + "\n" + growth.render_inbox(week, scout_text, back_text, disc_text, notes)
        self._write(f"{home}: create {title} ({found} suggestions)", self.gh.request, "POST",
                    f"/repos/{home}/issues", json={"title": title, "body": body, "labels": ["growth"]})
        for old in issues:  # only the newest inbox stays open
            if old.get("state") == "open" and old["title"] != title and marker("growth") in (old.get("body") or ""):
                self._write(f"{home}#{old['number']}: close previous growth inbox", self.gh.request, "PATCH",
                            f"/repos/{home}/issues/{old['number']}", json={"state": "closed", "state_reason": "completed"})

    def _help_draft(self, d: dict) -> dict[str, Any] | None:
        """Draft an answer for someone else's discussion from its text plus the project's docs. Never posted."""
        if not self._ai_ok(self.base):
            return None
        repo = d["repository"]
        info = {"full_name": repo["nameWithOwner"], "default_branch": (repo.get("defaultBranchRef") or {}).get("name", "main")}
        question = f"{d['title']}\n\n{d.get('body') or ''}"
        try:
            excerpts = knowledge.best_excerpts(question, self._docs_for(info))
        except GitHubError:
            excerpts = []
        try:
            if excerpts:
                reply = self.llm.answer(info["full_name"], question, excerpts)
                if reply.get("confident"):
                    links = [f"https://github.com/{info['full_name']}/blob/{info['default_branch']}/{p}"
                             for p in dict.fromkeys(reply["sources"])]
                    return {"answer": reply["answer"], "sources": links, "basis": "from the project's docs"}
            reply = self.llm.draft_help(info["full_name"], question)
            if reply["confident"] and len(reply["answer"]) >= 20:
                return {"answer": reply["answer"], "sources": [], "basis": "from the question only, verify it"}
        except LLMQuotaExceeded:
            raise
        except (LLMError, ValueError):
            return None
        return None

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
            # Repositories with issues turned off answer 410 here, which used to abort the whole digest.
            issues = ([i for i in self.gh.paged(f"/repos/{name}/issues", state="open", limit=300)
                       if "pull_request" not in i] if repo.get("has_issues", True) else [])
            prs = self.gh.paged(f"/repos/{name}/pulls", state="open", limit=100)
            rows.append(f"| [{repo['name']}](https://github.com/{name}) | {repo.get('stargazers_count', 0)} | "
                        f"{len(issues)} | {len(prs)} |")
        body = ("Open work across your repositories.\n\n| Repository | Stars | Open issues | Open PRs |\n|---|---|---|---|\n"
                + "\n".join(rows) + "\n\n**Waiting on you this run**\n"
                + ("\n".join(f"- {f}" for f in self.report.flagged) or "- Nothing") + FOOTER)
        self._write(f"{home}: create {title}", self.gh.request, "POST", f"/repos/{home}/issues",
                    json={"title": title, "body": body, "labels": ["digest"]})
        # Only the newest digest stays open, so old "waiting on you" lists don't pile up.
        for old in existing:
            if old.get("state") == "open" and old["title"] != title and is_bot(old.get("user")):
                self._write(f"{home}#{old['number']}: close previous digest", self.gh.request, "PATCH",
                            f"/repos/{home}/issues/{old['number']}", json={"state": "closed", "state_reason": "completed"})
