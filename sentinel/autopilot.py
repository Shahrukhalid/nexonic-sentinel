"""Autopilot for the owner's own repositories: fix PRs, SEO upkeep, contributor issues and release announcements.

Everything here acts only on repositories the owner installed the app on, and on the owner's own social accounts
when their credentials are set as secrets. Nothing is posted in other people's projects.
"""

from __future__ import annotations

import base64
import re
from datetime import datetime, timedelta
from typing import Any

import httpx

from . import contrib, review
from .github import GitHubError
from .ledger import key as lkey
from .llm import LLMQuotaExceeded, excluded, json_from, safe_text

FIX_LABEL = "autofix"
FIND_AND_FIX = """You are a careful senior engineer. Read one source file (lines are numbered "12| code") and find the
single most important real bug: wrong logic, crash, data loss, security hole. Ignore style, naming and missing tests.
If there is no clear bug, set "confident" false. Otherwise give a minimal fix as line edits: each edit replaces lines
start..end (inclusive, 1-based); "old" repeats those lines exactly and "new" is the replacement. Keep the style.
Reply with JSON only:
{"confident": true|false, "title": "short bug title", "explanation": "what is wrong and why the fix is right",
 "edits": [{"file": "path", "start": 1, "end": 1, "old": "...", "new": "..."}]}"""

SEO_SYSTEM = """You improve how a GitHub repository is found in GitHub and web search. Given its current description,
topics and the start of its README, reply with JSON only:
{"description": "one plain sentence, 80-150 characters, saying what it is and who it is for, with the main keywords",
 "topics": ["up to 12 lowercase GitHub topics, letters, digits and dashes only, most relevant first"],
 "readme_tips": ["up to 3 short, concrete README improvements for search, e.g. a missing section"]}
Do not invent features that the README does not describe."""

ISSUE_SYSTEM = """You write a clear GitHub issue that invites outside contributors to build one item from a project's
roadmap. You get the roadmap item, the README start and the file list. Reply with JSON only:
{"title": "imperative, under 70 characters", "level": "good first issue|help wanted",
 "body": "Markdown: why it matters (2 sentences), what to build (bullets), acceptance criteria (checklist), and where
 to start (only files from the list)"}
Do not invent files or APIs. Keep it under 250 words."""

ANNOUNCE_SYSTEM = """Write a short, friendly announcement of a software release for social media. Plain text, no
hashtags except up to two at the end, no emojis except at most one, under 260 characters, include no link (it is
added separately). Base it only on the release notes given. Reply with JSON only: {"text": "..."}"""

ARTICLE_SYSTEM = """Write a short developer blog post (300-500 words, Markdown) announcing a release of an open-source
project, for Dev.to. Base it only on the README start and release notes given: what the project does, what is new,
how to try it. Plain, honest tone, no hype words. Reply with JSON only: {"title": "...", "body": "...",
"tags": ["up to 4 lowercase tags"]}"""

TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,49}$")
ROADMAP_ITEM = re.compile(r"^\s*[-*]\s+\[ \]\s+(.{8,200})$", re.M)


def _when(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def clean_topics(values: Any) -> list[str]:
    out: list[str] = []
    for v in values or []:
        t = re.sub(r"[^a-z0-9-]+", "-", str(v).lower()).strip("-")[:50]
        if TOPIC_RE.match(t) and t not in out and not excluded(t):
            out.append(t)
    return out[:12]


def seo_score(repo: dict, readme: str | None, files: set[str]) -> tuple[int, list[str]]:
    """0-100 plus the list of what is missing. Purely structural, no AI."""
    checks = [
        (bool(repo.get("description")) and 50 <= len(repo["description"]) <= 160,
         "description of 50-160 characters"),
        (len(repo.get("topics") or []) >= 5, "at least 5 topics"),
        (bool(repo.get("homepage")), "website link"),
        (bool(readme) and readme.lstrip().startswith("#"), "README starting with a title"),
        (bool(readme) and len(readme) >= 1500, "README of at least 1,500 characters"),
        (bool(readme) and re.search(r"^#+\s*(install|getting started|quick ?start|usage)", readme or "", re.I | re.M)
         is not None, "install or quick-start section"),
        (bool(readme) and "![" in (readme or ""), "badges or screenshots in the README"),
        (any(f.upper().startswith("LICENSE") for f in files), "LICENSE file"),
        (any(f.upper().startswith("CONTRIBUTING") or f.upper() == ".GITHUB/CONTRIBUTING.MD" for f in files),
         "CONTRIBUTING guide"),
        (bool(repo.get("has_discussions")), "Discussions enabled"),
    ]
    missing = [label for ok, label in checks if not ok]
    return round(100 * (len(checks) - len(missing)) / len(checks)), missing


def roadmap_items(*texts: str | None) -> list[str]:
    items: list[str] = []
    for text in texts:
        for m in ROADMAP_ITEM.finditer(text or ""):
            item = re.sub(r"\s+", " ", m.group(1)).strip()
            if item not in items and not excluded(item):
                items.append(item)
    return items


def _bluesky_facets(text: str, url: str) -> list[dict]:
    start = len(text.encode("utf-8")) - len(url.encode("utf-8"))
    return [{"index": {"byteStart": start, "byteEnd": start + len(url.encode("utf-8"))},
             "features": [{"$type": "app.bsky.richtext.facet#link", "uri": url}]}]


class Autopilot:
    """Runs inside a Sentinel pass for the owner's installation only."""

    def __init__(self, bot, env: dict[str, str], transport: httpx.BaseTransport | None = None) -> None:
        self.bot, self.env = bot, env
        self.cfg = bot.cfg.get("autopilot", {}) if isinstance(bot.cfg.get("autopilot"), dict) else {}
        self.http = httpx.Client(timeout=30, transport=transport)

    # ------------------------------------------------------------------ helpers
    def on(self, name: str) -> bool:
        part = self.cfg.get(name, {})
        return bool(self.cfg.get("enabled", True)) and isinstance(part, dict) and bool(part.get("enabled", True))

    def part(self, name: str) -> dict[str, Any]:
        part = self.cfg.get(name, {})
        return part if isinstance(part, dict) else {}

    def weekly(self, kind: str, repo: str) -> bool:
        """True once per ISO week per repository and feature."""
        week = self.bot.now.strftime("%G-W%V")
        k = lkey("auto", kind, repo)
        if self.bot.ledger is None or self.bot.ledger.get(k) == week:
            return False
        return True

    def mark(self, kind: str, repo: str) -> None:
        if self.bot.ledger is not None:
            self.bot.ledger.set(lkey("auto", kind, repo), self.bot.now.strftime("%G-W%V"))

    def ai(self) -> bool:
        return self.bot._ai_ok(self.bot.base)

    # ------------------------------------------------------------------ entry point
    def run(self) -> None:
        skip = set(self.bot.cfg.get("skip_repos", []))
        repos = [r for r in self.bot.gh.repos() if r["name"] not in skip and not r.get("private")]
        steps = [("fixer", self.fixer), ("contributors", self.contributors), ("promote", self.promote)]
        for repo in repos:
            for name, step in steps:
                if not self.on(name):
                    continue
                try:
                    step(repo)
                except LLMQuotaExceeded:
                    self.bot.report.flagged.append("Autopilot paused until the next run (daily AI limit reached)")
                    return
                except (GitHubError, httpx.HTTPError, ValueError, KeyError) as exc:
                    self.bot._error(repo, f"autopilot {name}", exc)
            self.merge_ready(repo)
        if self.on("seo"):
            try:
                self.seo(repos)
            except LLMQuotaExceeded:
                self.bot.report.flagged.append("SEO report paused until the next run (daily AI limit reached)")
            except (GitHubError, ValueError) as exc:
                self.bot.report.errors.append(f"autopilot seo: {exc}")

    # ------------------------------------------------------------------ 1. fixer
    def fixer(self, repo: dict) -> None:
        """Weekly: read a few source files, and for a confident, cleanly applying fix open a pull request."""
        name, branch = repo["full_name"], repo.get("default_branch", "main")
        if not self.weekly("fix", name) or not self.ai():
            return
        p = self.part("fixer")
        open_prs = [x for x in self.bot.gh.paged(f"/repos/{name}/pulls", state="open", limit=50)
                    if any(lb.get("name") == FIX_LABEL for lb in x.get("labels", []))]
        if len(open_prs) >= int(p.get("max_open", 2)):
            self.mark("fix", name)
            return
        files = sorted(path for path, size in self.bot.gh.tree_entries(name, branch)
                       if review.scannable(path) and 200 <= size <= int(p.get("max_file_bytes", 15000)))
        if not files:
            self.mark("fix", name)
            return
        per = min(int(p.get("files_per_week", 2)), len(files))
        offset = (int(self.bot.now.strftime("%V")) * per + 7) % len(files)  # a different slice from code_scan
        made = 0
        for path in [files[(offset + i) % len(files)] for i in range(per)]:
            if not self.ai():
                return  # budget used up: the week stays open and is retried next run
            text = self.bot.gh.raw_file(name, path)
            if not text:
                continue
            try:
                raw = json_from(self.bot.llm.complete(FIND_AND_FIX, f"File: {path}\n\n{contrib.numbered(text)}", 2500))
            except LLMQuotaExceeded:
                raise
            except Exception:  # noqa: BLE001 - one unreadable reply must not stop the week
                continue
            if not raw.get("confident"):
                continue
            after = contrib.apply_edits({path: text}, raw.get("edits") or [])
            if after is None or not contrib.check_syntax(path, after[path]) or after[path] == text:
                continue
            title = safe_text(raw.get("title"), 70) or "Fix a likely bug"
            if excluded(title, raw.get("explanation")):
                continue
            changed = sum(1 for ln in contrib.unified({path: text}, after).splitlines()
                          if ln[:1] in "+-" and not ln.startswith(("+++", "---")))
            if self.open_fix_pr(repo, path, text, after[path], title, safe_text(raw.get("explanation"), 800), changed):
                made += 1
        self.mark("fix", name)
        if not made:
            self.bot.report.act(f"{name}: autopilot fixer found nothing it was confident about")

    def open_fix_pr(self, repo: dict, path: str, before: str, after: str, title: str, why: str, changed: int) -> bool:
        name, base = repo["full_name"], repo.get("default_branch", "main")
        fp = review.fingerprint(path, title)
        branch = f"sentinel/fix-{fp}"
        gh = self.bot.gh
        try:
            gh.get(f"/repos/{name}/git/ref/heads/{branch}")
            return False  # already proposed
        except GitHubError:
            pass
        diff = contrib.unified({path: before}, {path: after}).replace("```", "ˋˋˋ")
        small = changed <= int(self.part("fixer").get("automerge_max_lines", 6))
        body = (f"{self.bot_marker('autofix')}**{title}** in `{path}`\n\n{why}\n\n```diff\n{diff}\n```\n\n"
                + ("This is a small change, so I will merge it once every check passes, unless you add the "
                   "`hold` label or close this PR." if small else
                   "This change is larger than the automatic-merge limit, so it waits for your review.")
                + "\n\n<sub>Found by a weekly automated read of the code. Please check it before relying on it.</sub>")

        def do() -> None:
            head = gh.get(f"/repos/{name}/git/ref/heads/{base}")["object"]["sha"]
            gh.request("POST", f"/repos/{name}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": head})
            meta = gh.get(f"/repos/{name}/contents/{path}", ref=base)
            gh.request("PUT", f"/repos/{name}/contents/{path}",
                       json={"message": f"Fix: {title}", "branch": branch, "sha": meta["sha"],
                             "content": base64.b64encode(after.encode("utf-8")).decode("ascii")})
            pr = gh.request("POST", f"/repos/{name}/pulls",
                            json={"title": f"Fix: {title}", "head": branch, "base": base, "body": body})
            gh.add_labels(name, pr["number"], [FIX_LABEL] + (["automerge"] if small else []))

        self.bot._write(f"{name}: open fix PR for {path} ({changed} changed lines)", do)
        return True

    def bot_marker(self, kind: str) -> str:
        return f"<!-- nexonic-sentinel:{kind} -->\n"

    def merge_ready(self, repo: dict) -> None:
        """Squash-merge the bot's own small fix PRs once every check passed; anything else waits for the owner."""
        name = repo["full_name"]
        for pr in self.bot.gh.paged(f"/repos/{name}/pulls", state="open", limit=50):
            labels = {lb.get("name") for lb in pr.get("labels", [])}
            if not {FIX_LABEL, "automerge"} <= labels or "hold" in labels:
                continue
            if not pr["head"]["ref"].startswith("sentinel/fix-") or "nexonic-sentinel:autofix" not in (pr.get("body") or ""):
                continue
            sha = pr["head"]["sha"]
            runs = self.bot._check_runs(name, sha)
            if not runs or not self.bot._checks_green(name, sha):
                continue  # a repository without CI never gets automatic merges
            self.bot._write(f"{name}#{pr['number']}: merge small fix (checks passed)", self.bot.gh.request, "PUT",
                            f"/repos/{name}/pulls/{pr['number']}/merge",
                            json={"merge_method": "squash", "sha": sha})

    # ------------------------------------------------------------------ 2. SEO
    def seo(self, repos: list[dict]) -> None:
        """Weekly report for all repositories; descriptions and topics are applied when the app may edit them."""
        home = f"{self.bot.owner}/{self.bot.cfg.get('home_repo', 'nexonic-sentinel')}"
        if not self.weekly("seo", "all"):
            return
        p = self.part("seo")
        rows, notes = [], []
        for repo in repos:
            name = repo["full_name"]
            tree = {path for path, _ in self.bot.gh.tree_entries(name, repo.get("default_branch", "main"))}
            readme = self.bot.gh.readme(name)
            before, missing = seo_score(repo, readme, tree)
            applied: list[str] = []
            tips: list[str] = []
            if self.ai() and (before < 100 or len(repo.get("topics") or []) < 8):
                raw = json_from(self.bot.llm.complete(
                    SEO_SYSTEM, f"Repository: {name}\nDescription: {repo.get('description') or ''}\n"
                                f"Topics: {', '.join(repo.get('topics') or [])}\n\nREADME start:\n{(readme or '')[:3500]}",
                    800))
                desc = safe_text(raw.get("description"), 160).replace("\n", " ").strip()
                topics = clean_topics([*(repo.get("topics") or []), *(raw.get("topics") or [])])
                tips = [safe_text(t, 200) for t in (raw.get("readme_tips") or [])[:3] if t and not excluded(t)]
                if p.get("apply", True):
                    applied = self._apply_seo(repo, desc, topics)
                if not applied:
                    notes.append(f"**{name}**: suggested description: {desc or '(keep)'}; topics: "
                                 + ", ".join(f"`{t}`" for t in topics))
            rows.append(f"| [{repo['name']}](https://github.com/{name}) | {before} | "
                        f"{'; '.join(missing) or 'all good'} | {', '.join(applied) or '-'} |")
            for t in tips:
                notes.append(f"**{repo['name']}** README: {t}")
        body = (f"{self.bot_marker('seo')}Weekly search check of every public repository: what helps people find "
                "it on GitHub and in web search. Scores are 0-100.\n\n| Repository | Score | Missing | Applied this week |\n"
                "|---|---:|---|---|\n" + "\n".join(rows)
                + ("\n\n### Suggestions\n" + "\n".join(f"- {n}" for n in notes) if notes else "")
                + "\n\n<sub>Descriptions and topics are applied automatically when the app has the Administration "
                  "permission; otherwise they are listed above. Social preview images can only be uploaded in each "
                  "repository's Settings.</sub>")
        existing = [i for i in self.bot.gh.paged(f"/repos/{home}/issues", state="open", labels="seo", limit=5)
                    if "nexonic-sentinel:seo" in (i.get("body") or "")]
        if existing:
            self.bot._write(f"{home}: refresh SEO report", self.bot.gh.request, "PATCH",
                            f"/repos/{home}/issues/{existing[0]['number']}", json={"body": body})
        else:
            self.bot._write(f"{home}: create SEO report", self.bot.gh.request, "POST", f"/repos/{home}/issues",
                            json={"title": "SEO report", "body": body, "labels": ["seo"]})
        self.mark("seo", "all")

    def _apply_seo(self, repo: dict, desc: str, topics: list[str]) -> list[str]:
        name, done = repo["full_name"], []
        try:
            if desc and desc != (repo.get("description") or "") and not excluded(desc):
                self.bot._write(f"{name}: set description", self.bot.gh.request, "PATCH", f"/repos/{name}",
                                json={"description": desc})
                done.append("description")
            if topics and sorted(topics) != sorted(repo.get("topics") or []):
                self.bot._write(f"{name}: set {len(topics)} topics", self.bot.gh.request, "PUT",
                                f"/repos/{name}/topics", json={"names": topics})
                done.append("topics")
        except GitHubError as exc:
            if " 403" in str(exc) or "-> 403" in str(exc):
                return []  # no Administration permission: suggestions go to the report instead
            raise
        return done

    # ------------------------------------------------------------------ 3. contributor issues
    def contributors(self, repo: dict) -> None:
        """Weekly: turn unchecked roadmap items into well-specified issues that invite contributors."""
        name, branch = repo["full_name"], repo.get("default_branch", "main")
        if not repo.get("has_issues", True) or not self.weekly("contrib-issues", name):
            return
        p = self.part("contributors")
        readme = self.bot.gh.readme(name) or ""
        roadmap = self.bot.gh.raw_file(name, "ROADMAP.md") or self.bot.gh.raw_file(name, "docs/ROADMAP.md") or ""
        items = roadmap_items(roadmap, readme)
        existing = self.bot.gh.paged(f"/repos/{name}/issues", state="all", labels="help wanted", limit=100)
        seen = set(re.findall(r"<!-- roadmap:([0-9a-f]{12}) -->", " ".join(i.get("body") or "" for i in existing)))
        files = self.bot.gh.tree(name, branch)[:300]
        made = 0
        for item in items:
            if made >= int(p.get("per_week", 2)):
                break
            fp = lkey("roadmap", name, item)
            if fp in seen:
                continue
            if not self.ai():
                return
            raw = json_from(self.bot.llm.complete(
                ISSUE_SYSTEM, f"Repository: {name}\nRoadmap item: {item}\n\nREADME start:\n{readme[:2500]}\n\n"
                              "Files:\n" + "\n".join(files), 1000))
            title, body = safe_text(raw.get("title"), 80), safe_text(raw.get("body"), 3000)
            if not title or not body or excluded(title, body):
                continue
            labels = ["help wanted"] + (["good first issue"] if raw.get("level") == "good first issue" else [])
            self.bot._write(f"{name}: open contributor issue '{title}'", self.bot.gh.request, "POST",
                            f"/repos/{name}/issues",
                            json={"title": title, "labels": labels,
                                  "body": f"{body}\n\nComment here to take it on, and feel free to ask questions."
                                          f"\n\n<!-- roadmap:{fp} -->"})
            made += 1
        self.mark("contrib-issues", name)

    # ------------------------------------------------------------------ 4. release announcements
    def promote(self, repo: dict) -> None:
        """Announce each new release on the owner's own accounts (only those whose secrets are set)."""
        name = repo["full_name"]
        try:
            rel = self.bot.gh.get(f"/repos/{name}/releases/latest")
        except GitHubError:
            return  # no releases
        if rel.get("draft") or rel.get("prerelease") or not rel.get("published_at"):
            return
        if self.bot.now - _when(rel["published_at"]) > timedelta(days=int(self.part("promote").get("max_age_days", 7))):
            return
        k = lkey("announced", name, str(rel.get("id")))
        if self.bot.ledger is None or self.bot.ledger.get(k) or not self.ai():
            return
        notes = (rel.get("body") or "")[:3000]
        url = rel.get("html_url") or f"https://github.com/{name}"
        raw = json_from(self.bot.llm.complete(
            ANNOUNCE_SYSTEM,
            f"Project: {repo['name']}: {repo.get('description') or ''}\n"
            f"Release: {rel.get('name') or rel.get('tag_name')}\n\n{notes}", 400))
        text = safe_text(raw.get("text"), 260).replace("@", "")
        if not text or excluded(text):
            return
        post = f"{text}\n\n{url}"
        done = []
        if self.env.get("BLUESKY_HANDLE") and self.env.get("BLUESKY_APP_PASSWORD"):
            self.bot._write(f"Bluesky: announce {name} {rel.get('tag_name')}", self._bluesky, post, url)
            done.append("Bluesky")
        if self.env.get("MASTODON_INSTANCE") and self.env.get("MASTODON_TOKEN"):
            self.bot._write(f"Mastodon: announce {name} {rel.get('tag_name')}", self._mastodon, post)
            done.append("Mastodon")
        if self.env.get("DEV_API_KEY") and self.ai():
            readme = (self.bot.gh.readme(name) or "")[:3000]
            art = json_from(self.bot.llm.complete(
                ARTICLE_SYSTEM,
                f"Project: {name}\nRelease: {rel.get('tag_name')}\n\nNotes:\n{notes}\n\nREADME start:\n{readme}",
                1800))
            if art.get("title") and art.get("body") and not excluded(art.get("title"), art.get("body")):
                self.bot._write(f"Dev.to: draft article for {name} {rel.get('tag_name')}", self._devto, art, url)
                done.append("Dev.to draft")
        self.bot.ledger.set(k, self.bot.now.date().isoformat())
        if done:
            self.bot.report.act(f"{name} {rel.get('tag_name')}: announced on {', '.join(done)}")

    def _bluesky(self, text: str, url: str) -> None:
        s = self.http.post("https://bsky.social/xrpc/com.atproto.server.createSession",
                           json={"identifier": self.env["BLUESKY_HANDLE"], "password": self.env["BLUESKY_APP_PASSWORD"]})
        s.raise_for_status()
        sess = s.json()
        r = self.http.post("https://bsky.social/xrpc/com.atproto.repo.createRecord",
                           headers={"Authorization": f"Bearer {sess['accessJwt']}"},
                           json={"repo": sess["did"], "collection": "app.bsky.feed.post",
                                 "record": {"$type": "app.bsky.feed.post", "text": text,
                                            "createdAt": self.bot.now.isoformat().replace("+00:00", "Z"),
                                            "facets": _bluesky_facets(text, url)}})
        r.raise_for_status()

    def _mastodon(self, text: str) -> None:
        base = self.env["MASTODON_INSTANCE"].rstrip("/")
        if not base.startswith("https://"):
            base = "https://" + base
        r = self.http.post(f"{base}/api/v1/statuses", headers={"Authorization": f"Bearer {self.env['MASTODON_TOKEN']}"},
                           data={"status": text, "visibility": "public"})
        r.raise_for_status()

    def _devto(self, art: dict, url: str) -> None:
        body = safe_text(art.get("body"), 8000) + f"\n\nSource and install: {url}"
        r = self.http.post("https://dev.to/api/articles", headers={"api-key": self.env["DEV_API_KEY"]},
                           json={"article": {"title": safe_text(art.get("title"), 120), "body_markdown": body,
                                             "published": bool(self.part("promote").get("publish_devto", False)),
                                             "tags": [t.replace("-", "") for t in clean_topics(art.get("tags"))[:4]]}})
        r.raise_for_status()
