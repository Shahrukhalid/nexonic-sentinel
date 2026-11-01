"""Weekly overview of the owner's repositories: pull requests waiting on a review, failing checks on the main
branch, changes not released yet and open security alerts. Feeds the Monday digest and the public activity page.
Read-only: it only asks GitHub for information (security alerts need the app's "Dependabot alerts" permission;
without it that column says so).
"""

from __future__ import annotations

import base64
import html
from datetime import datetime
from typing import Any

from .github import GitHubError

STALE_DAYS = 7
FAILING = ("failure", "timed_out", "action_required", "startup_failure")


def _when(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def _is_bot(user: dict | None) -> bool:
    user = user or {}
    return user.get("type") == "Bot" or str(user.get("login", "")).endswith("[bot]")


def stale_prs(prs: list[dict], now: datetime, owner: str, days: int = STALE_DAYS) -> list[dict]:
    """Open pull requests from people (not bots, not the owner, not drafts) with no update for `days` days."""
    out = []
    for pr in prs:
        user = pr.get("user") or {}
        if pr.get("draft") or _is_bot(user) or user.get("login") == owner:
            continue
        waited = (now - _when(pr.get("updated_at") or pr["created_at"])).days
        if waited >= days:
            out.append({"number": pr["number"], "title": pr.get("title", ""), "author": user.get("login", "?"),
                        "days": waited, "url": pr.get("html_url", "")})
    return sorted(out, key=lambda p: -p["days"])


def repo_insights(gh, repo: dict, now: datetime, owner: str, prs: list[dict] | None = None) -> dict[str, Any]:
    """Everything the digest and activity page show for one repository. Each part fails on its own."""
    name, branch = repo["full_name"], repo.get("default_branch", "main")
    info: dict[str, Any] = {"name": repo["name"], "full_name": name, "stars": repo.get("stargazers_count", 0),
                            "stale": [], "failing": [], "release": None, "unreleased": None, "alerts": None}
    try:
        info["stale"] = stale_prs(prs if prs is not None else gh.paged(f"/repos/{name}/pulls", state="open", limit=100),
                                  now, owner)
    except GitHubError:
        pass
    try:
        runs = gh.get(f"/repos/{name}/commits/{branch}/check-runs", per_page=100).get("check_runs", [])
        info["failing"] = sorted({r["name"] for r in runs if r.get("conclusion") in FAILING})
    except GitHubError:
        pass
    try:
        rel = gh.get(f"/repos/{name}/releases/latest")
        info["release"] = rel.get("tag_name")
        cmp = gh.get(f"/repos/{name}/compare/{rel['tag_name']}...{branch}")
        info["unreleased"] = int(cmp.get("ahead_by", 0))
    except (GitHubError, KeyError, TypeError, ValueError):
        pass
    try:
        info["alerts"] = len(gh.paged(f"/repos/{name}/dependabot/alerts", state="open", limit=100))
    except GitHubError:
        info["alerts"] = None  # no permission, or alerts are off for this repository
    return info


def digest_sections(rows: list[dict[str, Any]]) -> str:
    """Markdown sections added to the weekly digest."""
    out: list[str] = []
    stale = [(r, p) for r in rows for p in r["stale"]]
    out.append(f"### Pull requests waiting more than {STALE_DAYS} days")
    out += [f"- [{r['name']}#{p['number']}]({p['url']}) {p['title']} by {p['author']}, {p['days']} days" for r, p in stale] \
        or ["- None: every contributor has an answer."]
    failing = [r for r in rows if r["failing"]]
    out.append("\n### Failing checks on the main branch")
    out += [f"- [{r['name']}](https://github.com/{r['full_name']}/actions): {', '.join(r['failing'])}" for r in failing] \
        or ["- None"]
    pending = [r for r in rows if r["unreleased"]]
    out.append("\n### Changes not released yet")
    out += [f"- {r['name']}: {r['unreleased']} commit{'s' if r['unreleased'] > 1 else ''} since {r['release']}"
            for r in pending] or ["- None: every release is up to date."]
    alerts = [r for r in rows if r["alerts"]]
    unknown = all(r["alerts"] is None for r in rows) if rows else True
    out.append("\n### Security alerts")
    if unknown:
        out.append("- Not available: give the app the \"Dependabot alerts: read\" permission to list them here.")
    else:
        out += [f"- [{r['name']}](https://github.com/{r['full_name']}/security/dependabot): {r['alerts']} open"
                for r in alerts] or ["- None open"]
    return "\n".join(out)


def activity_html(rows: list[dict[str, Any]], counts: dict[str, int], week: str, updated: str) -> str:
    """Static weekly status page for GitHub Pages (docs/activity.html)."""
    e = html.escape

    def cell(r: dict) -> str:
        checks = "failing: " + ", ".join(r["failing"]) if r["failing"] else "passing"
        rel = f"{r['release']}" + (f" (+{r['unreleased']} unreleased)" if r["unreleased"] else "") if r["release"] else "-"
        return (f"<tr><td><a href=\"https://github.com/{e(r['full_name'])}\">{e(r['name'])}</a></td>"
                f"<td>{r['stars']}</td><td>{r.get('issues', 0)}</td><td>{r.get('prs', 0)}</td>"
                f"<td class=\"{'bad' if r['failing'] else 'ok'}\">{e(checks)}</td><td>{e(rel)}</td></tr>")

    stats = "".join(f"<div class=\"stat\"><b>{v}</b><span>{e(k)}</span></div>" for k, v in counts.items())
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nexonic Sentinel activity</title>
<meta name="description" content="What Nexonic Sentinel, an open-source GitHub maintainer bot, did this week.">
<style>
:root{{--bg:#0d1117;--fg:#e6edf3;--muted:#8b949e;--line:#30363d;--card:#161b22;--ok:#3fb950;--bad:#f85149;--accent:#58a6ff}}
@media (prefers-color-scheme: light){{:root{{--bg:#fff;--fg:#1f2328;--muted:#59636e;--line:#d1d9e0;--card:#f6f8fa;
--ok:#1a7f37;--bad:#cf222e;--accent:#0969da}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 -apple-system,Segoe UI,Helvetica,Arial,sans-serif}}
main{{max-width:960px;margin:auto;padding:24px 16px}} a{{color:var(--accent)}} h1{{margin:0 0 4px}}
.muted{{color:var(--muted)}} .stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;
margin:20px 0}}
.stat{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px}}
.stat b{{display:block;font-size:28px}}
.wrap{{overflow-x:auto}} table{{border-collapse:collapse;width:100%}}
td,th{{border-bottom:1px solid var(--line);padding:8px;text-align:left;white-space:nowrap}}
.ok{{color:var(--ok)}} .bad{{color:var(--bad)}}
</style></head><body><main>
<h1>Nexonic Sentinel activity</h1>
<p class="muted">Week {e(week)} · updated {e(updated)} · <a href="./">about the bot</a></p>
<div class="stats">{stats}</div>
<div class="wrap"><table><thead><tr><th>Repository</th><th>Stars</th><th>Open issues</th><th>Open PRs</th>
<th>Main branch checks</th><th>Latest release</th></tr></thead>
<tbody>{''.join(cell(r) for r in rows)}</tbody></table></div>
</main></body></html>
"""


def publish_page(gh, repo: str, path: str, content: str, message: str) -> None:
    """Create or update one file on the default branch through the contents API."""
    sha = None
    try:
        sha = gh.get(f"/repos/{repo}/contents/{path}").get("sha")
    except GitHubError:
        pass
    body = {"message": message, "content": base64.b64encode(content.encode("utf-8")).decode("ascii")}
    if sha:
        body["sha"] = sha
    gh.request("PUT", f"/repos/{repo}/contents/{path}", json=body)
