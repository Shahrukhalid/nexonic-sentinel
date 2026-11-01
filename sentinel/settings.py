"""Per-repository settings.

Every repository can add `.github/nexonic-sentinel.toml` to switch features on or off. Repositories of the
bot's owner start from sentinel.toml; every other installation starts from conservative defaults where
nothing is sent to AI models and nothing is merged until the repository opts in.
"""

from __future__ import annotations

import tomllib
from typing import Any

FILE = ".github/nexonic-sentinel.toml"
BOOLS = ("enabled", "triage", "welcome", "discussions", "ai", "pr_review", "code_scan")
UPDATE_KINDS = {"patch", "minor", "major"}
TEMPLATE_KEYS = ("greeting", "bug_repro", "security", "docs_intro", "closing", "welcome")
TEMPLATE_MAX = 1500  # characters per template

PUBLIC_DEFAULTS: dict[str, Any] = {
    "enabled": True, "triage": True, "welcome": True, "discussions": True,
    "ai": False,              # nothing leaves GitHub for an AI model unless the repository opts in
    "pr_review": True, "code_scan": True,  # only take effect when ai = true
    "dependabot": [],         # no automatic merges unless the repository opts in
}


def _table(cfg: dict[str, Any], name: str) -> dict[str, Any]:
    """A TOML table from sentinel.toml, or {} when it is missing or not a table (e.g. `features = "x"`)."""
    value = cfg.get(name)
    return value if isinstance(value, dict) else {}


def home_defaults(cfg: dict[str, Any]) -> dict[str, Any]:
    f = _table(cfg, "features")
    out = dict(PUBLIC_DEFAULTS)
    out["ai"] = True  # the owner's own repositories use AI features unless sentinel.toml turns them off
    out.update({k: f[k] for k in BOOLS if isinstance(f.get(k), bool)})
    # A malformed [automerge] section merges nothing rather than falling back to the default.
    automerge = cfg.get("automerge", {})
    dep = automerge.get("dependabot", ["patch", "minor"]) if isinstance(automerge, dict) else []
    # Anything but a list of strings (e.g. [["patch"]]) merges nothing.
    ok = isinstance(dep, list) and all(isinstance(x, str) for x in dep)
    out["dependabot"] = [x for x in dep if x in UPDATE_KINDS] if ok else []
    return out


def parse(text: str | None, base: dict[str, Any]) -> dict[str, Any]:
    """Overlay a repository's TOML file on the defaults, ignoring unknown keys and wrong types."""
    out = dict(base)
    if not text:
        return out
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return out
    for key in BOOLS:
        if isinstance(data.get(key), bool):
            out[key] = data[key]
    tpl = data.get("templates")
    if isinstance(tpl, dict):
        out["templates"] = {k: v.strip()[:TEMPLATE_MAX] for k, v in tpl.items()
                            if k in TEMPLATE_KEYS and isinstance(v, str) and v.strip()}
    if "dependabot" in data:
        dep = data["dependabot"]
        ok = isinstance(dep, list) and all(isinstance(x, str) for x in dep)
        out["dependabot"] = [x for x in dep if x in UPDATE_KINDS] if ok else []  # malformed: merge nothing
    return out


EXAMPLE = """# .github/nexonic-sentinel.toml
enabled = true      # false = Nexonic Sentinel leaves this repository alone
triage = true       # label new issues and point to relevant docs
welcome = true      # welcome first-time contributors
discussions = true  # reply in Q&A discussions (AI answers need ai = true)
ai = false          # allow sending issue, discussion and code text to free OpenRouter models
pr_review = true    # AI bug review of pull requests (needs ai = true)
code_scan = true    # weekly AI code health report (needs ai = true)
dependabot = []     # Dependabot updates to merge once all checks pass, e.g. ["patch", "minor"]

# Optional: your own wording for replies. Placeholders: {author} {issue} {repo} {labels}
# [templates]
# greeting = "Hi {author}, thanks for reporting this in {repo}!"
# bug_repro = "Please add your version, OS, the steps and any error output."
# security = "Please report vulnerabilities privately, see SECURITY.md."
# docs_intro = "These pages may help:"
# closing = "A maintainer will reply soon."
# welcome = "Thanks for your first pull request, {author}!"
"""
