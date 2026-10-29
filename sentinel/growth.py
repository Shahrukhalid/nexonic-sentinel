"""Growth inbox: backlink finder and discussions helper, plus the weekly inbox text.

Everything here only READS public data. Results are returned as plain data and the bot writes them into one issue in
the owner's own repository. Nothing in this module can comment, open issues or pull requests, react, star or follow
anywhere: it is given a read-only view of the GitHub client.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta
from typing import Any, Callable

from .ledger import key as lkey
from .llm import excluded, json_from, safe_text

SKIP_HEADINGS = re.compile(r"^(contents|table of contents|contributing|contribute|license|licence|footnotes?|"
                           r"related|credits|code of conduct|sponsors?)\b", re.I)
NOT_ACCEPTING = re.compile(r"(not (currently )?(accepting|taking|open to)|no longer (accepting|taking)|"
                           r"(closed|disabled|do not|don'?t) (to )?(accept )?(new )?(pull requests?|prs|submissions|"
                           r"contributions))", re.I)
SENSITIVE = re.compile(r"\b(security|vulnerab\w*|exploit|cve-\d|leak\w*|password|token|secret key|lawsuit|legal|"
                       r"licen[cs]e violation|dmca|copyright claim)\b", re.I)
SUGGESTED_DAYS = 56  # a suggestion that was not acted on is not repeated for eight weeks
MAX_STATE = 600


class ReadOnly:
    """Wraps the GitHub client so only reads can pass: any write attempt raises instead of reaching the network."""

    def __init__(self, gh) -> None:
        self._gh = gh

    def get(self, path: str, **params):
        return self._gh.get(path, **params)

    def paged(self, *a, **kw):
        return self._gh.paged(*a, **kw)

    def graphql(self, query: str, **variables):
        if re.search(r"\bmutation\b", query, re.I):
            raise PermissionError("read-only")
        return self._gh.graphql(query, **variables)

    def raw_file(self, repo: str, path: str):
        return self._gh.raw_file(repo, path)

    def readme(self, repo: str):
        return self._gh.readme(repo)

    def tree_entries(self, repo: str, branch: str):
        return self._gh.tree_entries(repo, branch)

    def tree(self, repo: str, branch: str):
        return self._gh.tree(repo, branch)

    def __getattr__(self, name: str):
        raise PermissionError(f"read-only client: {name} is not available")


def _when(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def _tbl(cfg: dict[str, Any], name: str) -> dict[str, Any]:
    v = cfg.get(name)
    return v if isinstance(v, dict) else {}


# ------------------------------------------------------------------ backlink state (ledger)
def backlink_key(repo: str, list_repo: str) -> str:
    return lkey("backlink", repo, list_repo)


def blocked(entry: Any, now: datetime) -> bool:
    """submitted / declined suggestions are never repeated; plain suggestions wait eight weeks."""
    if not isinstance(entry, dict):
        return False
    if entry.get("s") in ("submitted", "declined"):
        return True
    try:
        return now - datetime.fromisoformat(str(entry.get("at"))).replace(tzinfo=now.tzinfo) < timedelta(days=SUGGESTED_DAYS)
    except ValueError:
        return False


COMMAND = re.compile(r"^\s*(submitted|declined)\s+bl-([0-9a-f]{12})\b", re.I | re.M)


def apply_commands(state: dict[str, Any], comments: list[dict], owner: str, now: datetime) -> int:
    """The owner marks suggestions by commenting `submitted bl-<id>` or `declined bl-<id>` on an inbox issue."""
    changed = 0
    for c in comments:
        if (c.get("user") or {}).get("login", "").lower() != owner.lower():
            continue
        for verb, ident in COMMAND.findall(c.get("body") or ""):
            if ident in state and state[ident].get("s") != verb.lower():
                state[ident] = {**state[ident], "s": verb.lower(), "at": now.date().isoformat()}
                changed += 1
    return changed


def prune(state: dict[str, Any]) -> dict[str, Any]:
    if len(state) <= MAX_STATE:
        return state
    keep = sorted(state.items(), key=lambda kv: (kv[1].get("s") in ("submitted", "declined"), kv[1].get("at", "")))
    return dict(keep[-MAX_STATE:])


# ------------------------------------------------------------------ backlink finder
def sections(readme: str) -> list[tuple[str, list[str]]]:
    """(heading text, lines) for each Markdown section of a README."""
    out: list[tuple[str, list[str]]] = []
    for line in readme.split("\n"):
        m = re.match(r"^#{2,4}\s+(.+?)\s*#*\s*$", line)
        if m:
            text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", m.group(1))
            text = re.sub(r"[^\w\s&/+.#-]", "", text).strip()
            out.append((text, []))
        elif out:
            out[-1][1].append(line)
    return out


ITEM = re.compile(r"^(\s*[-*+]\s+)\[([^\]]+)\]\(([^)]+)\)(.*)$")


def pick_section(secs: list[tuple[str, list[str]]], words: set[str]) -> tuple[str, list[str]] | None:
    best, best_score = None, -1.0
    for head, lines in secs:
        if SKIP_HEADINGS.match(head) or not any(ITEM.match(ln) for ln in lines):
            continue
        hw = set(re.findall(r"[a-z0-9]+", head.lower()))
        score = len(hw & words) * 3 + len(words & set(re.findall(r"[a-z0-9]+", " ".join(lines[:40]).lower()))) * 0.1
        if score > best_score:
            best, best_score = (head, lines), score
    return best


def format_entry(repo: dict, lines: list[str]) -> tuple[str, str]:
    """An entry line copying the format of the section's existing items; returns (line, placement hint)."""
    items = [m for m in (ITEM.match(ln) for ln in lines) if m]
    name = repo["name"]
    url = repo["html_url"]
    desc = safe_text(repo.get("description") or "", 160).replace("\n", " ").strip()
    if not items:
        return f"- [{name}]({url}) - {desc}".rstrip(" -"), "end of the section"
    first = items[0]
    bullet, rest = first.group(1), first.group(4)
    sep = re.match(r"^\s*([-–—:]|\s)\s*", rest)
    sep_text = rest[:sep.end()] if sep and rest.strip() else ""
    if not sep_text and desc:
        sep_text = " - "
    end = "." if rest.rstrip().endswith(".") else ""
    text = desc.rstrip(".")
    if rest.strip() and rest.strip(" -–—:")[:1].islower():
        text = text[:1].lower() + text[1:]
    line = f"{bullet}[{name}]({url}){sep_text}{text}{end}" if text else f"{bullet}[{name}]({url})"
    names = [m.group(2).lower() for m in items]
    place = "end of the section"
    if names == sorted(names):  # alphabetical lists: say where it goes
        before = [m.group(2) for m in items if m.group(2).lower() < name.lower()]
        place = f"alphabetical order, after {before[-1]}" if before else "start of the section (alphabetical order)"
    return line.rstrip(), place


def draft_entry(llm, repo: dict, list_repo: dict, readme: str, guide: str | None,
                words: set[str]) -> dict[str, str] | None:
    secs = sections(readme)
    names = {h.lower(): (h, ln) for h, ln in secs if any(ITEM.match(x) for x in ln)}
    chosen = pick_section(secs, words)
    if chosen is None:
        return None
    head, lines = chosen
    line, place = format_entry(repo, lines)
    if llm is not None and llm.available():
        examples = [ln for ln in lines if ITEM.match(ln)][:3]
        prompt = (f"Project to add: {repo['name']} - {repo.get('description') or ''}\nURL: {repo['html_url']}\n"
                  f"Topics: {', '.join(repo.get('topics') or [])}\n\nList: {list_repo['full_name']}\n"
                  f"Section headings: {', '.join(h for h in names)}\nExample entries:\n" + "\n".join(examples)
                  + (f"\n\nContribution guidelines (excerpt):\n{guide[:1500]}" if guide else ""))
        system = ("You add a project to a curated list. Reply with JSON only: {\"section\": \"exact heading from the "
                  "given headings\", \"entry\": \"one line in exactly the format of the example entries, containing "
                  "the project URL\", \"suitable\": true|false}. Set suitable to false when the project does not "
                  "fit the list's scope or the guidelines. Do not invent facts about the project.")
        try:
            raw = json_from(llm.complete(system, prompt, 400))
            if raw.get("suitable") is False:
                return None
            sec = str(raw.get("section") or "").strip().lower()
            entry = safe_text(raw.get("entry"), 400).replace("\n", " ").strip()
            if sec in names and repo["html_url"] in entry:
                head, lines = names[sec]
                line, place = entry, format_entry(repo, lines)[1]
        except Exception:  # noqa: BLE001 - fall back to the format-copying entry
            pass
    return {"section": head, "entry": line, "placement": place}


def find_backlinks(gh, llm, cfg: dict[str, Any], owner: str, now: datetime, state: dict[str, Any],
                   repos: list[dict]) -> list[dict[str, Any]]:
    """Curated lists that fit the owner's public repositories and do not mention them yet. Read-only."""
    b = _tbl(cfg, "backlinks")
    max_items = int(b.get("max_items", 3))
    min_stars = int(b.get("min_stars", 50))
    since = (now - timedelta(days=int(b.get("active_days", 180)))).date().isoformat()
    max_searches, max_reads = int(b.get("max_searches", 10)), int(b.get("max_reads", 15))
    searches = reads = 0
    picks: list[dict[str, Any]] = []
    seen_lists: set[tuple[str, str]] = set()
    mine = [r for r in repos if not r.get("private") and r.get("topics")]
    for repo in mine:
        for topic in repo["topics"][:2]:
            if len(picks) >= max_items or searches >= max_searches:
                return picks
            searches += 1
            data = gh.get("/search/repositories", q=f"awesome {topic} in:name,description archived:false "
                          f"pushed:>{since} stars:>={min_stars}", sort="stars", order="desc", per_page=8)
            for lst in data.get("items", []):
                full = lst["full_name"]
                low = lst["name"].lower()
                if (len(picks) >= max_items or reads >= max_reads or lst.get("archived") or lst.get("fork")
                        or excluded(full, lst.get("description"), *(lst.get("topics") or []))
                        or full.split("/", 1)[0].lower() == owner.lower() or (repo["full_name"], full) in seen_lists
                        or not ("awesome" in low or "awesome-list" in (lst.get("topics") or []) or "list" in low)):
                    continue
                seen_lists.add((repo["full_name"], full))
                ident = backlink_key(repo["full_name"], full)
                if blocked(state.get(ident), now):
                    continue
                reads += 1
                text = gh.readme(full)
                if not text or f"github.com/{repo['full_name']}".lower() in text.lower():
                    continue  # no README, or the repository is already listed
                guide = gh.raw_file(full, "CONTRIBUTING.md") or gh.raw_file(full, ".github/CONTRIBUTING.md")
                if guide and NOT_ACCEPTING.search(guide):
                    continue
                words = set(re.findall(r"[a-z0-9]+", " ".join([topic, *(repo.get("topics") or []),
                                                                 repo.get("description") or ""]).lower()))
                drafted = draft_entry(llm, repo, lst, text, guide, words - {"a", "the", "and", "for", "of"})
                if not drafted:
                    continue
                picks.append({"id": ident, "repo": repo["full_name"], "list": full, "stars": lst.get("stargazers_count", 0),
                              "guide": f"https://github.com/{full}/blob/{lst.get('default_branch', 'main')}/CONTRIBUTING.md"
                              if guide else "", **drafted})
    return picks


# ------------------------------------------------------------------ discussions helper
DISC_Q = """query($q:String!,$n:Int!){search(query:$q,type:DISCUSSION,first:$n){nodes{... on Discussion{id number title body
 createdAt url isAnswered locked author{login __typename} category{name isAnswerable}
 comments(first:10){totalCount nodes{author{login}}}
 repository{nameWithOwner stargazerCount pushedAt isArchived isFork hasDiscussionsEnabled defaultBranchRef{name}
 owner{login}}}}}}"""


def keywords(cfg: dict[str, Any], repos: list[dict]) -> list[str]:
    configured = _tbl(cfg, "answers").get("keywords")
    if isinstance(configured, list) and configured:
        return [str(k) for k in configured][:6]
    counts: dict[str, int] = {}
    for r in repos:
        for t in r.get("topics") or []:
            counts[t] = counts.get(t, 0) + 1
    return [t for t, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))][:4]


def find_discussions(gh, cfg: dict[str, Any], owner: str, now: datetime, seen: set[str], hasher: Callable[[str], str],
                     repos: list[dict]) -> list[dict[str, Any]]:
    """Recent unanswered Q&A discussions in other active projects on the owner's topics. Read-only."""
    a = _tbl(cfg, "answers")
    days, min_stars = int(a.get("max_age_days", 7)), int(a.get("min_stars", 100))
    active, max_items = int(a.get("active_days", 60)), int(a.get("max_items", 3))
    since = (now - timedelta(days=days)).date().isoformat()
    found: dict[str, dict] = {}
    for kw in keywords(cfg, repos):
        q = f'{kw} is:unanswered category:"Q&A" created:>={since} sort:created-desc'
        for d in gh.graphql(DISC_Q, q=q, n=10)["search"]["nodes"]:
            if d and d.get("url"):
                found.setdefault(d["url"], d)
    ranked = []
    for d in found.values():
        repo = d.get("repository") or {}
        author = d.get("author") or {}
        text = f"{d['title']}\n{d.get('body') or ''}"
        logins = [(c.get("author") or {}).get("login", "") for c in d["comments"]["nodes"]]
        if (d.get("locked") or d.get("isAnswered") or not (d.get("category") or {}).get("isAnswerable")
                or hasher(d["url"]) in seen or author.get("__typename") == "Bot"
                or (author.get("login") or "").lower() == owner.lower()
                or any(x.lower() == owner.lower() for x in logins)
                or (repo.get("owner") or {}).get("login", "").lower() == owner.lower()
                or repo.get("isArchived") or repo.get("isFork") or not repo.get("hasDiscussionsEnabled", True)
                or repo.get("stargazerCount", 0) < min_stars or d["comments"]["totalCount"] > 2
                or now - _when(d["createdAt"]) > timedelta(days=days)
                or now - _when(repo.get("pushedAt") or "1970-01-01T00:00:00Z") > timedelta(days=active)
                or SENSITIVE.search(text) or excluded(text, repo.get("nameWithOwner"), repo.get("description"))):
            continue
        age = (now - _when(d["createdAt"])).total_seconds() / 86400
        score = math.log10(repo.get("stargazerCount", 0) + 1) + max(0.0, 1 - age / days) + 1 / (1 + d["comments"]["totalCount"])
        ranked.append((score, d))
    ranked.sort(key=lambda t: t[0], reverse=True)
    return [d for _, d in ranked[:max_items]]


# ------------------------------------------------------------------ inbox text
def render_backlinks(items: list[dict[str, Any]]) -> str:
    if not items:
        return "Nothing new this week."
    out = ["Curated lists that match your repositories and do not mention them yet. Each entry copies the list's "
           "format. Read the list's contribution rules before submitting.\n"]
    for i, b in enumerate(items, 1):
        out.append(f"#### {i}. [{b['list']}](https://github.com/{b['list']}) for {b['repo']} · ★ {b['stars']}\n"
                   f"Section: **{b['section']}** ({b['placement']})\n```markdown\n{b['entry']}\n```\n"
                   + (f"Guidelines: {b['guide']}\n" if b.get("guide") else "")
                   + f"After you decide, comment `submitted bl-{b['id']}` or `declined bl-{b['id']}` here so it "
                     "is not suggested again.")
    return "\n".join(out)


def render_discussions(items: list[dict[str, Any]]) -> str:
    if not items:
        return "Nothing new this week."
    out = ["Recent unanswered questions in projects on your topics. The drafts are only suggestions: verify them "
           "before replying yourself.\n"]
    for i, it in enumerate(items, 1):
        d, repo = it["discussion"], it["discussion"]["repository"]
        out.append(f"#### {i}. [{safe_text(d['title'], 150)}]({d['url']})\n{repo['nameWithOwner']} · ★ "
                   f"{repo['stargazerCount']} · {d['comments']['totalCount']} comments")
        out.append("> " + safe_text(d.get("body") or "", 300).replace("\n", "\n> "))
        draft = it.get("draft")
        if draft:
            out.append(f"**Draft answer** ({draft['basis']}):\n\n{safe_text(draft['answer'], 2500)}")
            if draft.get("sources"):
                out.append("Sources:\n" + "\n".join(f"- {s}" for s in draft["sources"]))
        else:
            out.append("No draft this week.")
        out.append("")
    return "\n".join(out)


def render_inbox(week: str, scout: str, backlinks: str, discussions: str, notes: list[str]) -> str:
    parts = [f"Automated suggestions for {week}. Check each one before acting. Nothing here has been posted "
             "anywhere: all of it is only listed in this issue.\n",
             "## Scout\n" + scout, "## Backlinks\n" + backlinks, "## Discussions\n" + discussions]
    if notes:
        parts.append("**Not available this week:** " + "; ".join(notes))
    return "\n\n".join(parts)
