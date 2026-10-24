"""Pick the documentation passages most relevant to a question (simple keyword scoring, no vector DB)."""

from __future__ import annotations

import re

DOC_PATTERNS = (re.compile(r"^README\.md$", re.I), re.compile(r"^docs/.+\.md$", re.I),
                re.compile(r"^(CONTRIBUTING|SECURITY|CHANGELOG|TROUBLESHOOTING)\.md$", re.I))
STOP = set("a an and are as at be by can do does for from how i if in is it its me my of on or so that the this to "
           "was what when where which who why will with you your we our not use using get".split())
MAX_DOC_FILES = 25


def doc_paths(paths: list[str]) -> list[str]:
    return [p for p in paths if any(rx.match(p) for rx in DOC_PATTERNS)][:MAX_DOC_FILES]


def words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9][a-z0-9_.+-]*", text.lower()) if w not in STOP and len(w) > 1]


def chunks(path: str, text: str, size: int = 1400) -> list[tuple[str, str]]:
    """Split Markdown at headings, then by size, keeping the path for citations."""
    parts = re.split(r"\n(?=#{1,4} )", text)
    out = []
    for part in parts:
        for i in range(0, len(part), size):
            piece = part[i:i + size].strip()
            if piece:
                out.append((path, piece))
    return out


def best_excerpts(question: str, docs: dict[str, str], k: int = 5) -> list[tuple[str, str]]:
    q = set(words(question))
    if not q:
        return []
    scored = []
    for path, text in docs.items():
        for c_path, piece in chunks(path, text):
            ws = words(piece)
            if not ws:
                continue
            hits = sum(1 for w in ws if w in q)
            cover = len(q & set(ws)) / len(q)
            if hits:
                scored.append((cover * 3 + hits / len(ws) * 10, c_path, piece))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [(p, t) for _, p, t in scored[:k]]
