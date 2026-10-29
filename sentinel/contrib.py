"""Contribution finder: issues where maintainers asked for help, with a drafted approach for each.

It only reads public issues and writes a shortlist to the bot owner's own repository. It never comments, opens
pull requests or contacts anyone elsewhere; the owner decides what to work on and submits it personally.
"""

from __future__ import annotations

import ast
import difflib
import math
import re
from datetime import datetime, timedelta
from typing import Any

from .llm import excluded, json_from, safe_text

LABELS = ('"good first issue"', '"help wanted"', "bug")
CLAIMED = re.compile(r"(i'?ll take (this|it)|i can take (this|it)|i'?d like to (work on|take|try|fix)|"
                     r"(i'?m|i am|i will|i'?ll) (be )?(working on|looking into|picking up|on it|fixing)|"
                     r"working on (this|it)|can i (work on|take|try|have)|assign (this )?(to )?me|"
                     r"picking (this|it) up|let me (take|try|fix|work)|i'?ve started|taking this|"
                     r"i want to (work on|fix|take))", re.I)
DRAFT_SYSTEM = """You help an experienced developer decide whether to contribute a fix to an open-source issue.
You are given the issue and the repository's file list. Do not invent APIs or files that are not listed.
Reply with JSON only:
{"difficulty": "easy|medium|hard", "summary": "one sentence: what is wrong or wanted",
 "approach": ["step", "step", "step"], "files": ["path from the list"], "test": "how to prove the fix works",
 "worth_it": true|false}
Set worth_it to false if the issue is vague, already solved, needs a maintainer decision, or is mostly not code."""

FIX_SYSTEM = """You write a minimal, correct fix for an open-source issue. You are given the issue and the full text of
the relevant files with line numbers ("12| code"). Change as little as possible and keep the project's style.
Each edit replaces the lines start..end (inclusive, 1-based) of one file. "old" must repeat those lines exactly
(without the line-number prefix) and "new" is the replacement text. To insert, replace one line with itself plus
the new lines. Never touch files that were not given. If you are not confident the fix is right, set "confident" false.
Also describe, from reading the files, how to reproduce the problem ("repro", 1-2 sentences) and where the bug most
likely is ("where", one sentence naming a file and function or line from the files given).
Reply with JSON only:
{"confident": true|false, "repro": "...", "where": "...", "explanation": "why this fixes it, 1-3 sentences",
 "edits": [{"file": "path", "start": 1, "end": 1, "old": "...", "new": "..."}],
 "test": "a short test (code) that fails before the fix and passes after"}"""


def queries(languages: list[str], owner: str, since: str) -> list[str]:
    out = []
    for lang in languages:
        for label in LABELS:
            out.append(f"is:issue is:open no:assignee archived:false language:{lang} label:{label} "
                       f"comments:<6 updated:>{since} -user:{owner}")
    return out


def _when(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def score(repo: dict, issue: dict, now: datetime, fresh_days: int = 14) -> float:
    """Higher is better: fresh issue, active and popular project, likely small effort."""
    stars = repo.get("stargazers_count", 0)
    active = max(0.0, 1 - (now - _when(repo["pushed_at"])).days / 90)
    updated = _when(issue.get("updated_at") or issue.get("created_at") or now.isoformat())
    fresh = max(0.0, 1 - (now - updated).days / max(1, fresh_days))
    names = {lb["name"].lower() for lb in issue.get("labels", [])}
    effort = (0.4 if "good first issue" in names else 0.0) + (0.2 if len(issue.get("body") or "") < 1200 else 0.0)
    comments = issue.get("comments", 0)
    return math.log10(stars + 1) + 1.5 * fresh + active + effort - (0.2 if comments >= 4 else 0.0) + 0.5 / (1 + comments)


def claimed(comments: list[dict], now: datetime, days: int = 21) -> bool:
    """A recent "I'm working on this" style comment from anyone."""
    for c in comments:
        if (c.get("user") or {}).get("type") == "Bot":
            continue
        try:
            recent = (now - _when(c["created_at"])).days <= days
        except (KeyError, ValueError):
            recent = True
        if recent and CLAIMED.search(c.get("body") or ""):
            return True
    return False


def has_open_pr(timeline: list[dict]) -> bool:
    """True when an open pull request references the issue (cross-reference events carry the PR state)."""
    for e in timeline:
        src = (e.get("source") or {}).get("issue") or {}
        if e.get("event") == "cross-referenced" and "pull_request" in src and src.get("state") == "open":
            return True
    return False


def available(gh, full: str, item: dict, now: datetime, c: dict) -> bool:
    """Read-only checks that nobody has already taken the issue. If they cannot be made, the issue is skipped."""
    if item.get("assignee") or item.get("assignees"):
        return False
    try:
        comments = gh.get(f"/repos/{full}/issues/{item['number']}/comments", per_page=30)
        if claimed(comments, now, int(c.get("claim_days", 21))):
            return False
        return not has_open_pr(gh.get(f"/repos/{full}/issues/{item['number']}/timeline", per_page=100))
    except Exception:  # noqa: BLE001 - cannot verify, so do not suggest it
        return False


def find(gh, llm, cfg: dict[str, Any], owner: str, now: datetime, seen: set[str], hasher) -> list[dict[str, Any]]:
    """`seen` holds hashed URLs of issues already shown or rejected; new rejections are added to it."""
    c = cfg.get("contrib", {})
    languages = c.get("languages", ["python", "typescript", "javascript"])
    min_stars, max_items = int(c.get("min_stars", 30)), int(c.get("max_items", 3))
    fresh_days, active_days = int(c.get("fresh_days", 14)), int(c.get("active_days", 60))
    since = (now - timedelta(days=fresh_days)).date().isoformat()
    candidates: dict[str, dict] = {}
    for q in queries(languages, owner, since):
        data = gh.get("/search/issues", q=q, sort="updated", order="desc", per_page=15)
        for item in data.get("items", []):
            if hasher(item["html_url"]) in seen or "pull_request" in item:
                continue
            if excluded(item.get("title"), item.get("body"), item.get("repository_url")):
                seen.add(hasher(item["html_url"]))
                continue
            candidates.setdefault(item["html_url"], item)
    repos: dict[str, dict] = {}
    ranked = []
    for item in list(candidates.values())[:60]:
        full = item["repository_url"].split("/repos/", 1)[1]
        if full.split("/", 1)[0].lower() == owner.lower() or item.get("assignee") or item.get("assignees"):
            continue
        if now - _when(item.get("updated_at") or now.isoformat()) > timedelta(days=fresh_days):
            continue
        if full not in repos:
            repos[full] = gh.get(f"/repos/{full}")
        repo = repos[full]
        if repo.get("archived") or repo.get("fork") or repo.get("stargazers_count", 0) < min_stars:
            continue
        if excluded(repo.get("description"), *(repo.get("topics") or [])):
            continue
        if (now - _when(repo["pushed_at"])).days > active_days:
            continue
        ranked.append((score(repo, item, now, fresh_days), item, repo))
    ranked.sort(key=lambda t: t[0], reverse=True)

    picks = []
    checks = 0
    fix_files, max_bytes = int(c.get("fix_files", 2)), int(c.get("max_file_bytes", 20000))
    for _, item, repo in ranked:
        if len(picks) >= max_items or checks >= int(c.get("max_checks", 20)):
            break
        checks += 1
        if not available(gh, repo["full_name"], item, now, c):
            continue
        draft = None
        if llm is not None and llm.available():
            try:
                entries = gh.tree_entries(repo["full_name"], repo.get("default_branch", "main"))[:400]
                paths = [p for p, _ in entries]
                prompt = (f"Repository: {repo['full_name']} ({repo.get('language')}): {repo.get('description') or ''}\n\n"
                          f"Issue #{item['number']}: {item['title']}\n\n{(item.get('body') or '')[:3000]}\n\n"
                          "Repository files:\n" + "\n".join(paths))
                raw = json_from(llm.complete(DRAFT_SYSTEM, prompt, 700))
                if not raw.get("worth_it", True):
                    seen.add(hasher(item["html_url"]))
                    continue
                known = set(paths)
                draft = {"difficulty": safe_text(raw.get("difficulty"), 10),
                         "summary": safe_text(raw.get("summary"), 300),
                         "approach": [safe_text(s, 300) for s in (raw.get("approach") or [])[:5] if s],
                         "files": [p for p in (raw.get("files") or []) if isinstance(p, str) and p in known][:5],
                         "test": safe_text(raw.get("test"), 300), "fix": None, "note": None}
                if c.get("draft_fix", True) and llm.available():
                    sizes = dict(entries)
                    targets = [p for p in draft["files"] if 0 < sizes.get(p, 0) <= max_bytes][:fix_files]
                    draft["fix"], draft["note"] = draft_fix(gh, llm, repo, item, targets)
            except Exception:  # noqa: BLE001 - one bad draft must not stop the shortlist
                draft = None
        picks.append({"url": item["html_url"], "title": safe_text(item["title"], 150), "repo": repo["full_name"],
                      "stars": repo.get("stargazers_count", 0), "language": repo.get("language") or "",
                      "labels": [lb["name"] for lb in item.get("labels", [])][:4], "draft": draft})
    return picks


def numbered(text: str) -> str:
    return "\n".join(f"{i}| {line}" for i, line in enumerate(text.split("\n"), 1))


def apply_edits(sources: dict[str, str], edits: list[Any]) -> dict[str, str] | None:
    """Apply line edits only when every "old" block matches the real file exactly; otherwise reject the fix."""
    out = {p: t.split("\n") for p, t in sources.items()}
    by_file: dict[str, list[tuple[int, int, list[str]]]] = {}
    for e in edits[:12]:
        if not isinstance(e, dict) or e.get("file") not in out:
            return None
        try:
            start, end = int(e["start"]), int(e["end"])
        except (KeyError, TypeError, ValueError):
            return None
        lines = out[e["file"]]
        if not 1 <= start <= end <= len(lines):
            return None
        old = str(e.get("old", "")).rstrip("\n").split("\n")
        if [x.rstrip() for x in old] != [x.rstrip() for x in lines[start - 1:end]]:
            return None
        by_file.setdefault(e["file"], []).append((start, end, str(e.get("new", "")).rstrip("\n").split("\n")))
    if not by_file:
        return None
    result = dict(sources)
    for path, items in by_file.items():
        items.sort(key=lambda t: t[0])
        if any(a[1] >= b[0] for a, b in zip(items, items[1:], strict=False)):
            return None  # overlapping edits
        lines = out[path]
        for start, end, new in reversed(items):
            lines[start - 1:end] = new
        result[path] = "\n".join(lines)
    return result


def check_syntax(path: str, text: str) -> bool:
    if path.endswith(".py"):
        try:
            ast.parse(text)
        except SyntaxError:
            return False
    return True


def unified(before: dict[str, str], after: dict[str, str]) -> str:
    parts = []
    for path in before:
        if before[path] != after[path]:
            parts.extend(difflib.unified_diff(before[path].split("\n"), after[path].split("\n"),
                                              f"a/{path}", f"b/{path}", lineterm="", n=3))
    return "\n".join(parts)


def draft_fix(gh, llm, repo: dict, item: dict, targets: list[str]) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    """Read the real files, ask for line edits, and keep the fix only if it applies cleanly and still parses.
    Returns (fix, note); the note says how to reproduce the problem and where it likely is."""
    sources = {}
    for path in targets:
        text = gh.raw_file(repo["full_name"], path)
        if text:
            sources[path] = text
    if not sources:
        return None, None
    prompt = (f"Repository: {repo['full_name']}\nIssue #{item['number']}: {item['title']}\n\n"
              f"{(item.get('body') or '')[:3000]}\n\n"
              + "\n\n".join(f"### {p}\n{numbered(t)}" for p, t in sources.items()))
    raw = json_from(llm.complete(FIX_SYSTEM, prompt, 2500))
    note = None
    if raw.get("repro") or raw.get("where"):
        note = {"repro": safe_text(raw.get("repro"), 400), "where": safe_text(raw.get("where"), 300)}
    if not raw.get("confident"):
        return None, note
    after = apply_edits(sources, raw.get("edits") or [])
    if after is None or not all(check_syntax(p, t) for p, t in after.items()):
        return None, note
    diff = unified(sources, after)
    if not diff or len(diff) > 12000:
        return None, note
    return ({"diff": diff.replace("```", "ˋˋˋ"), "explanation": safe_text(raw.get("explanation"), 500),
             "test": safe_text(raw.get("test"), 1500).replace("```", "ˋˋˋ")}, note)


def render(picks: list[dict[str, Any]]) -> str:
    """Markdown for the "Scout" section of the weekly inbox."""
    if not picks:
        return "Nothing new this week."
    out = ["Fresh issues where maintainers asked for help, in active projects, with no assignee, no open pull "
           "request and no recent claim. Before starting, check again that nobody has taken it.\n"]
    for i, p in enumerate(picks, 1):
        out.append(f"#### {i}. [{p['title']}]({p['url']})\n{p['repo']} · ★ {p['stars']} · {p['language']} · "
                   + ", ".join(f"`{lb}`" for lb in p["labels"]))
        d = p["draft"]
        if d:
            out.append(f"**{d['difficulty'].title() or 'Unknown'}**: {d['summary']}")
            note = d.get("note")
            if note and note.get("repro"):
                out.append(f"How to reproduce: {note['repro']}")
            if note and note.get("where"):
                out.append(f"Where it likely is: {note['where']}")
            if d["approach"]:
                out.append("\n".join(f"{n}. {s}" for n, s in enumerate(d["approach"], 1)))
            if d["files"]:
                out.append("Start with: " + ", ".join(f"`{f}`" for f in d["files"]))
            if d["test"]:
                out.append(f"Test: {d['test']}")
            fx = d.get("fix")
            if fx:
                out.append(f"**Drafted fix** (applies cleanly to the current default branch): {fx['explanation']}")
                out.append(f"```diff\n{fx['diff']}\n```")
                if fx["test"]:
                    out.append(f"Suggested test:\n```\n{fx['test']}\n```")
        out.append("")
    out.append("<sub>Drafts are checked to apply to the real files (and to parse, for Python) but not run. "
               "Run the project's tests and submit only what you have verified yourself.</sub>")
    return "\n".join(out)
