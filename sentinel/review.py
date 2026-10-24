"""AI bug review of pull requests and the weekly code health scan.

Both send code to a free model with line numbers, ask for concrete, high-confidence bugs only, and then check
every finding against the real diff or file before anything is posted. Nothing is ever executed.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from .llm import safe_text

SEVERITIES = {"high": 2, "medium": 1, "low": 0}
SKIP_FILES = re.compile(
    r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|uv\.lock|requirements\.lock|Cargo\.lock|"
    r"go\.sum|composer\.lock|Gemfile\.lock)$|(^|/)(node_modules|dist|build|vendor|\.next|coverage|__pycache__)/|"
    r"\.(min\.js|map|svg|png|jpe?g|gif|webp|ico|pdf|zip|woff2?|ttf|eot|mp4|onnx|bin|lock)$", re.I)
CODE_FILES = re.compile(r"\.(py|ts|tsx|js|jsx|mjs|cjs|go|rs|java|kt|rb|php|cs|c|cc|cpp|h|hpp|swift|sh|ps1|vue|svelte)$",
                        re.I)
TEST_FILES = re.compile(r"(^|/)(tests?|__tests__|spec|e2e)/|[._-](test|spec)\.\w+$", re.I)

REVIEW_SYSTEM = """You are a careful senior engineer reviewing a pull request for real bugs.
Report ONLY concrete defects you are confident about: logic errors, crashes, wrong API use, unhandled errors that
lose data, race conditions, resource leaks, security problems (injection, path traversal, secrets, unsafe eval).
Do NOT report style, naming, formatting, missing comments, missing tests, or speculative "might" issues.
Each changed line is shown as "<line number> + code". Use those numbers. Removed lines have no number.
Reply with JSON only:
{"findings": [{"file": "path", "line": 42, "severity": "high|medium|low", "title": "short title",
 "explanation": "why this is a bug and what goes wrong", "fix": "corrected code or a precise instruction"}]}
Return at most 5 findings. Return {"findings": []} if you find nothing you are sure about."""

SCAN_SYSTEM = """You are a careful senior engineer auditing one source file for real bugs.
Report ONLY concrete defects you are confident about: logic errors, crashes, wrong API use, unhandled errors that
lose data, race conditions, resource leaks, security problems. No style, naming, docs, tests or speculation.
Every line is shown as "<line number>: code". Use those numbers.
Reply with JSON only:
{"findings": [{"line": 42, "severity": "high|medium|low", "title": "short title",
 "explanation": "why this is a bug and what goes wrong", "fix": "corrected code or a precise instruction"}]}
Return at most 3 findings. Return {"findings": []} if you find nothing you are sure about."""


def reviewable(path: str) -> bool:
    return bool(CODE_FILES.search(path)) and not SKIP_FILES.search(path)


def scannable(path: str) -> bool:
    return reviewable(path) and not TEST_FILES.search(path)


def annotate_patch(patch: str) -> tuple[str, set[int]]:
    """Prefix added and context lines with their line number in the new file; return the numbers of added lines."""
    out, added, new = [], set(), 0
    for line in patch.splitlines():
        m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if m:
            new = int(m.group(1))
            out.append(line)
        elif line.startswith("+"):
            out.append(f"{new} + {line[1:]}")
            added.add(new)
            new += 1
        elif line.startswith("-"):
            out.append(f"    - {line[1:]}")
        elif line.startswith("\\"):
            continue
        else:
            out.append(f"{new}   {line[1:] if line.startswith(' ') else line}")
            new += 1
    return "\n".join(out), added


def number_lines(text: str) -> str:
    return "\n".join(f"{i}: {line}" for i, line in enumerate(text.splitlines(), 1))


def clean_findings(raw: Any, valid_lines: dict[str, set[int]] | None, min_severity: str, limit: int,
                   default_file: str | None = None) -> list[dict[str, Any]]:
    """Keep findings that point at a real file and line, meet the severity bar and carry an explanation."""
    items = raw.get("findings") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return []
    floor = SEVERITIES.get(min_severity, 1)
    out = []
    for f in items:
        if not isinstance(f, dict):
            continue
        sev = str(f.get("severity", "")).lower()
        if SEVERITIES.get(sev, -1) < floor:
            continue
        path = str(f.get("file") or default_file or "")
        try:
            line = int(f.get("line"))
        except (TypeError, ValueError):
            continue
        if valid_lines is not None and (path not in valid_lines or line not in valid_lines[path]):
            continue
        title, why = safe_text(f.get("title"), 120), safe_text(f.get("explanation"), 700)
        if len(title) < 5 or len(why) < 20:
            continue
        out.append({"file": path, "line": line, "severity": sev, "title": title, "explanation": why,
                    "fix": safe_text(f.get("fix"), 1500)})
        if len(out) >= limit:
            break
    return out


def fingerprint(path: str, title: str) -> str:
    norm = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    return hashlib.sha256(f"{path}|{norm}".encode()).hexdigest()[:10]


def render(findings: list[dict[str, Any]], link) -> str:
    parts = []
    for i, f in enumerate(findings, 1):
        fix = f["fix"]
        fix_block = f"\n\n**Suggested fix:**\n```\n{fix}\n```" if "\n" in fix or len(fix) > 80 else (
            f"\n\n**Suggested fix:** {fix}" if fix else "")
        parts.append(f"**{i}. {f['title']}** ({f['severity']}) · [{f['file']}:{f['line']}]({link(f)})\n\n"
                     f"{f['explanation']}{fix_block}\n<!-- fp:{fingerprint(f['file'], f['title'])} -->")
    return "\n\n".join(parts)


def review_prompt(pr: dict, files: list[dict]) -> tuple[str, dict[str, set[int]]]:
    chunks, lines = [], {}
    for f in files:
        text, added = annotate_patch(f.get("patch") or "")
        lines[f["filename"]] = added
        chunks.append(f"### {f['filename']} ({f.get('status')})\n{text}")
    head = (f"Pull request: {pr.get('title', '')}\n\nDescription:\n{(pr.get('body') or '')[:1500]}\n\n"
            "Changed files:\n\n")
    return head + "\n\n".join(chunks), lines
