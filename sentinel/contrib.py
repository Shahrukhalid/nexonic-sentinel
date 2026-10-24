"""Contribution finder: issues where maintainers asked for help, with a drafted approach for each.

It only reads public issues and writes a shortlist to the bot owner's own repository. It never comments, opens
pull requests or contacts anyone elsewhere; the owner decides what to work on and submits it personally.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

from .llm import json_from, safe_text

LABELS = ('"good first issue"', '"help wanted"')
DRAFT_SYSTEM = """You help an experienced developer decide whether to contribute a fix to an open-source issue.
You are given the issue and the repository's file list. Do not invent APIs or files that are not listed.
Reply with JSON only:
{"difficulty": "easy|medium|hard", "summary": "one sentence: what is wrong or wanted",
 "approach": ["step", "step", "step"], "files": ["path from the list"], "test": "how to prove the fix works",
 "worth_it": true|false}
Set worth_it to false if the issue is vague, already solved, needs a maintainer decision, or is mostly not code."""


def queries(languages: list[str], owner: str, since: str) -> list[str]:
    out = []
    for lang in languages:
        for label in LABELS:
            out.append(f"is:issue is:open no:assignee archived:false language:{lang} label:{label} "
                       f"comments:<6 created:>{since} -user:{owner}")
    return out


def score(repo: dict, issue: dict, now: datetime) -> float:
    stars = repo.get("stargazers_count", 0)
    pushed = datetime.fromisoformat(repo["pushed_at"].replace("Z", "+00:00"))
    fresh = max(0.0, 1 - (now - pushed).days / 90)
    quiet = 1 / (1 + issue.get("comments", 0))
    return math.log10(stars + 1) + fresh + quiet


def find(gh, llm, cfg: dict[str, Any], owner: str, now: datetime, seen: set[str], hasher) -> list[dict[str, Any]]:
    """`seen` holds hashed URLs of issues already shown or rejected; new rejections are added to it."""
    c = cfg.get("contrib", {})
    languages = c.get("languages", ["python", "typescript"])
    min_stars, max_items = int(c.get("min_stars", 30)), int(c.get("max_items", 5))
    since = (now - timedelta(days=int(c.get("max_age_days", 30)))).date().isoformat()
    candidates: dict[str, dict] = {}
    for q in queries(languages, owner, since):
        data = gh.get("/search/issues", q=q, sort="created", order="desc", per_page=20)
        for item in data.get("items", []):
            if hasher(item["html_url"]) not in seen and "pull_request" not in item:
                candidates.setdefault(item["html_url"], item)
    repos: dict[str, dict] = {}
    ranked = []
    for item in list(candidates.values())[:40]:
        full = item["repository_url"].split("/repos/", 1)[1]
        if full not in repos:
            repos[full] = gh.get(f"/repos/{full}")
        repo = repos[full]
        if repo.get("archived") or repo.get("fork") or repo.get("stargazers_count", 0) < min_stars:
            continue
        pushed = datetime.fromisoformat(repo["pushed_at"].replace("Z", "+00:00"))
        if (now - pushed).days > 90:
            continue
        ranked.append((score(repo, item, now), item, repo))
    ranked.sort(key=lambda t: t[0], reverse=True)

    picks = []
    for _, item, repo in ranked:
        if len(picks) >= max_items:
            break
        draft = None
        if llm is not None and llm.available():
            try:
                paths = gh.tree(repo["full_name"], repo.get("default_branch", "main"))[:400]
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
                         "test": safe_text(raw.get("test"), 300)}
            except Exception:  # noqa: BLE001 - one bad draft must not stop the shortlist
                draft = None
        picks.append({"url": item["html_url"], "title": safe_text(item["title"], 150), "repo": repo["full_name"],
                      "stars": repo.get("stargazers_count", 0), "language": repo.get("language") or "",
                      "labels": [lb["name"] for lb in item.get("labels", [])][:4], "draft": draft})
    return picks


def render(picks: list[dict[str, Any]], day: str) -> str:
    if not picks:
        return f"No new matching issues on {day}."
    out = [f"Updated {day}. Issues where maintainers asked for help, ranked by project activity and how fresh they are. "
           "Before starting, check that nobody is already working on it, and comment on the issue to say you will "
           "take it.\n"]
    for i, p in enumerate(picks, 1):
        out.append(f"### {i}. [{p['title']}]({p['url']})\n{p['repo']} · ★ {p['stars']} · {p['language']} · "
                   + ", ".join(f"`{lb}`" for lb in p["labels"]))
        d = p["draft"]
        if d:
            out.append(f"**{d['difficulty'].title() or 'Unknown'}**: {d['summary']}")
            if d["approach"]:
                out.append("\n".join(f"{n}. {s}" for n, s in enumerate(d["approach"], 1)))
            if d["files"]:
                out.append("Start with: " + ", ".join(f"`{f}`" for f in d["files"]))
            if d["test"]:
                out.append(f"Test: {d['test']}")
        out.append("")
    out.append("<sub>The approach is an AI draft from the issue text and file list only. Read the code before "
               "relying on it.</sub>")
    return "\n".join(out)
