"""The owner's pull requests in other projects (package catalogues such as winget, Homebrew or Scoop).

The bot only reads them: it never comments, pushes or closes anything outside the owner's repositories. Each run it
works out who has the next step and explains it in plain words. PRs that need the owner (changes requested, a failed
validation, a conflict) go on the "Waiting on you" list with what to do; PRs waiting on the other project's reviewers
are only shown as status, so nothing nags while there is nothing to do.
"""
from __future__ import annotations

import re
from typing import Any

# Label words used by package catalogues. Matching is on lowercase words, so "Needs-Author-Feedback",
# "Manifest-Validation-Error" and "needs author feedback" all count.
AUTHOR_LABELS = ("needs-author-feedback", "needs-author", "changes-requested", "validation-error", "-error",
                 "needs-attention", "blocked", "author-action", "waiting-on-author")
WAITING_LABELS = ("needs-review", "moderator-approved", "azure-pipeline-passed", "validation-completed",
                  "needs-moderator", "awaiting-review")
# Bot comments that only say the project's reviewers are next.
MODERATOR_TEXT = re.compile(r"requires? a moderator|moderators? are community volunteers|waiting for (a )?review|"
                            r"awaiting (maintainer|moderator) review", re.I)
AUTHOR_TEXT = re.compile(r"please (fix|update|address|correct|resolve)|validation (has )?failed|failed validation|"
                         r"needs? (author )?(feedback|changes)|could not (be )?(installed|validated)|error[s]? (was|were) found",
                         re.I)


def _norm(label: str) -> str:
    return re.sub(r"[\s_]+", "-", label.strip().lower())


def classify(pr: dict[str, Any], labels: list[str], reviews: list[dict], checks: list[dict],
             comments: list[dict], owner: str) -> tuple[str, str]:
    """('you' | 'them' | 'done', plain explanation). `comments` are the latest ones, oldest first."""
    if pr.get("merged") or pr.get("merged_at"):
        return "done", "merged"
    if pr.get("state") == "closed":
        return "done", "closed without merging"
    names = [_norm(x) for x in labels]
    # The newest review per reviewer decides.
    latest: dict[str, str] = {}
    for r in reviews:
        who = (r.get("user") or {}).get("login") or ""
        if who and who != owner and r.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[who] = r["state"]
    asked = [who for who, state in latest.items() if state == "CHANGES_REQUESTED"]
    failed = [c.get("name") or "a check" for c in checks
              if c.get("status") == "completed" and c.get("conclusion") in ("failure", "timed_out", "action_required")]
    others = [c for c in comments if ((c.get("user") or {}).get("login") or "") != owner]
    last = others[-1] if others else None
    owner_answered = bool(comments) and ((comments[-1].get("user") or {}).get("login") == owner)
    author_label = next((x for x in names if any(w in x for w in AUTHOR_LABELS)), None)
    if asked:
        return "you", f"{', '.join(asked)} asked for changes: read the review, update the branch, then reply on the PR."
    if pr.get("mergeable_state") == "dirty":
        return "you", "has a merge conflict: update the branch from the project's default branch."
    if author_label and not owner_answered:
        return "you", (f"label {author_label} means the project waits for you: read the newest bot or reviewer comment "
                       "and fix what it lists.")
    if failed:
        return "you", f"check failed ({', '.join(sorted(set(failed))[:3])}): open the check's details for the error."
    if last and AUTHOR_TEXT.search(last.get("body") or "") and not owner_answered:
        who = (last.get("user") or {}).get("login") or "someone"
        return "you", f"{who} asked for a change in the newest comment: read it and reply on the PR."
    if last and MODERATOR_TEXT.search(last.get("body") or ""):
        return "them", "waiting for the project's moderators to review it (volunteers; nothing for you to do)."
    if any(any(w in x for w in WAITING_LABELS) for x in names):
        return "them", "checks passed; waiting for the project's reviewers (nothing for you to do)."
    if latest and all(state == "APPROVED" for state in latest.values()):
        return "them", "approved; waiting for the project to merge it."
    return "them", "open; waiting for the project's reviewers (nothing for you to do)."


def find(gh, owner: str, limit: int = 30) -> list[dict]:
    """The owner's open pull requests in repositories of other accounts."""
    data = gh.get("/search/issues", q=f"is:pr is:open author:{owner} -user:{owner} archived:false",
                  sort="updated", order="desc", per_page=limit)
    return [i for i in (data or {}).get("items", []) if "pull_request" in i]


def check(gh, item: dict, owner: str) -> dict[str, Any]:
    """Status of one PR from the search: repo, number, url, title, who ('you' / 'them' / 'done') and what to do."""
    repo = re.sub(r"^https://api\.github\.com/repos/", "", item["repository_url"])
    n = int(item["number"])
    pr = gh.get(f"/repos/{repo}/pulls/{n}")
    reviews = gh.paged(f"/repos/{repo}/pulls/{n}/reviews", limit=100)
    sha = (pr.get("head") or {}).get("sha")
    checks = (gh.get(f"/repos/{repo}/commits/{sha}/check-runs", per_page=50) or {}).get("check_runs", []) if sha else []
    comments = gh.paged(f"/repos/{repo}/issues/{n}/comments", limit=100)[-10:]
    labels = [lab.get("name", "") for lab in item.get("labels", [])]
    who, what = classify(pr, labels, reviews, checks, comments, owner)
    return {"repo": repo, "n": n, "url": item.get("html_url") or pr.get("html_url", ""), "title": item.get("title", ""),
            "who": who, "what": what}
