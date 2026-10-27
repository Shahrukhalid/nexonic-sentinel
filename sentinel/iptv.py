"""Channel review for the owner's IPTV catalogue repository.

What it does, all inside ONE configured repository of the owner (see `[iptv_review]` in sentinel.toml):

1. Opens a weekly "Channel review YYYY-Www" issue that lists the pending channel records, and closes last week's.
2. Carries out `approve <id>` / `reject <id> <reason>` comments, but only from the owner's own account. A decision
   moves the record between data/pending, data/approved and data/rejected in a single commit.
3. Turns well-formed "Suggest a free channel" issues into pending records. Nothing is ever approved automatically.
4. Answers "Report broken channel" issues with the latest status from reports/channels.json. Data is not edited.

The record files are edited as text, one record block at a time, so every other line of a file stays byte-for-byte
the same (the files are partly hand-edited, so re-dumping a whole file would rewrite many unrelated lines). Every
edit is re-parsed and compared with what was intended before it is committed; any doubt aborts the change.
This module does not import anything from the IPTV repository: it only reads its files through the GitHub API.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import html
import io
import ipaddress
import json
import re
import unicodedata
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit

import yaml

from .github import GitHubError

REVIEW_LABEL = "channel-review"
SUGGEST_LABEL = "channel-suggestion"
BROKEN_LABEL = "broken-stream"
REPORT_LABEL = "stream-report"
MARK_REVIEW = "<!-- nexonic-sentinel:iptv-review -->"
MARK_BROKEN = "<!-- nexonic-sentinel:iptv-broken -->"
SUGGEST_METHOD = "community-suggestion"
FOOTER = "\n\n<sub>Nexonic Sentinel</sub>"

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$")
BASES = ("official-broadcaster-stream", "official-cdn-stream", "public-sector-stream")
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
DATA_DIRS = ("pending", "approved", "rejected")

# Mirrors docs/SOURCE_POLICY.md section 2 ("always rejected") closely enough to refuse the obvious cases.
TOKEN_PARAMS = {"token", "auth", "authtoken", "access_token", "hdnts", "hdnea", "wmsauthsign", "sig", "signature",
                "expires", "exp", "e", "st", "md5", "hash", "key", "apikey", "api_key", "password", "pass", "user",
                "username", "u", "p", "session", "sessionid", "policy", "key-pair-id"}
DENY_HOSTS = re.compile(r"(^|\.)(pishow\.tv|tangotv\.in|smartplaytv\.in|mjh\.nz|freem3u\.xyz|aynaott\.com|cinerama\.uz|"
                        r"mcquack\.net|onrender\.com|ddns\.net|duckdns\.org|dynu\.net|gleeze\.com|no-ip\.(org|com|biz)|"
                        r"githubusercontent\.com|youtube\.com|youtu\.be|twitch\.tv)$")
BLOCKED_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home", ".corp", ".intranet", ".arpa")
# Pages that anybody can publish on are never "the broadcaster's own page".
THIRD_PARTY = re.compile(r"(^|\.)(youtube\.com|youtu\.be|facebook\.com|fb\.com|instagram\.com|twitter\.com|x\.com|"
                         r"t\.me|telegram\.org|tiktok\.com|github\.com|github\.io|gitlab\.com|pastebin\.com|wikipedia\.org|"
                         r"google\.com|bit\.ly|tinyurl\.com|reddit\.com|blogspot\.com|wordpress\.com|wixsite\.com)$")
ADULT = re.compile(r"\b(xxx|porn\w*|erotic\w*|playboy|hustler|nude|nudes|sex\w*|adult|18\+|hentai)\b", re.I)
SECOND_LEVEL = {"co", "com", "org", "net", "gov", "edu", "ac", "or", "ne", "go", "gob", "nom"}
CATEGORY_OPTIONS = {"news", "general", "entertainment", "kids", "education", "documentary", "culture", "music", "sports",
                    "lifestyle", "business", "legislative", "science", "weather", "movies"}


class RecordError(ValueError):
    """A record file or edit could not be handled safely. Nothing is committed."""


class Conflict(RuntimeError):
    """The branch moved while a commit was being prepared."""


# ====================================================================== text helpers
def clean(value: Any, limit: int = 400) -> str:
    """One line of plain text: control and line-break characters become spaces, runs of spaces collapse."""
    text = "".join(" " if unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") else ch for ch in str(value or ""))
    return " ".join(text.split())[:limit]


def md_cell(value: Any, limit: int = 160) -> str:
    text = html.escape(clean(value, limit), quote=False).replace("|", "/").replace("@", "&#64;")
    return text.replace("`", "'")


def code(value: Any, limit: int = 120) -> str:
    """Untrusted text shown inside a code span: it can neither break out of it nor @mention anybody."""
    return "`" + clean(value, limit).replace("`", "'") + "`"


def safe_link(url: str) -> str:
    return url.replace(" ", "%20").replace("(", "%28").replace(")", "%29").replace("|", "%7C").replace("<", "%3C")


def norm_url(url: str) -> str:
    """Same normalisation as the catalogue's own duplicate check: scheme and host in lower case, default port and
    fragment dropped."""
    p = urlsplit(url.strip())
    host = (p.hostname or "").lower()
    netloc = host + (f":{p.port}" if p.port and p.port not in (80, 443) else "")
    return urlunsplit((p.scheme.lower(), netloc, p.path, p.query, ""))


def norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", unicodedata.normalize("NFKC", name).casefold())


def base_domain(host: str) -> str:
    labels = host.lower().rstrip(".").split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


_PLAIN = re.compile(r"^(?:[A-Za-z][A-Za-z0-9 ._/+()-]*|https?://[A-Za-z0-9._~/?=&%+-]+)$")


def scalar(value: Any) -> str:
    """A YAML scalar on one line: plain when that is unambiguous (checked by parsing it), otherwise single-quoted."""
    text = clean(value, 2100)
    if _PLAIN.fullmatch(text):
        try:
            if yaml.safe_load(text) == text:
                return text
        except yaml.YAMLError:
            pass
    return "'" + text.replace("'", "''") + "'"


# ====================================================================== record files (text level)
def split_file(text: str) -> tuple[list[str], list[list[str]]]:
    """(lines before `channels:`, one list of lines per record)."""
    if "\r" in text or "\t" in text:
        raise RecordError("carriage returns or tabs in a record file")
    lines = text.splitlines(keepends=True)
    start = next((i for i, ln in enumerate(lines) if re.fullmatch(r"channels:[ ]*(\[\])?[ ]*\n?", ln)), None)
    if start is None:
        raise RecordError("no top-level `channels:` key")
    blocks: list[list[str]] = []
    for ln in lines[start + 1:]:
        if ln.startswith("- "):
            blocks.append([ln])
        elif blocks and (ln.startswith(" ") or ln.strip() == ""):
            blocks[-1].append(ln)
        elif ln.strip() != "":
            raise RecordError(f"unexpected line outside a record: {ln.strip()[:40]!r}")
    if "[]" in lines[start] and blocks:
        raise RecordError("`channels: []` followed by records")
    return lines[:start], blocks


def _load_block(block: list[str]) -> Any:
    try:
        data = yaml.safe_load("".join(["channels:\n", *block]))
    except yaml.YAMLError as exc:
        raise RecordError("record block is not valid YAML") from exc
    items = data.get("channels") if isinstance(data, dict) else None
    if not isinstance(items, list) or len(items) != 1:
        raise RecordError("record block is not a single record")
    return items[0]


@dataclass
class RecordFile:
    path: str
    head: list[str]
    blocks: list[list[str]] = field(default_factory=list)
    records: list[dict] = field(default_factory=list)

    @classmethod
    def parse(cls, path: str, text: str) -> RecordFile:
        head, blocks = split_file(text)
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise RecordError(f"{path}: not valid YAML") from exc
        listed = (data.get("channels") if isinstance(data, dict) else None) or []
        if not isinstance(listed, list) or len(listed) != len(blocks):
            raise RecordError(f"{path}: record count does not match its text")
        for block, rec in zip(blocks, listed, strict=True):
            if not isinstance(rec, dict) or not isinstance(rec.get("id"), str) or _load_block(block) != rec:
                raise RecordError(f"{path}: a record block does not parse to its own record")
        return cls(path, head, blocks, listed)

    @classmethod
    def new(cls, path: str, header: str) -> RecordFile:
        return cls(path, [header.rstrip("\n") + "\n"])

    def copy(self) -> RecordFile:
        return RecordFile(self.path, list(self.head), [list(b) for b in self.blocks], list(self.records))

    def ids(self) -> list[str]:
        return [r["id"] for r in self.records]

    def render(self) -> str:
        tail = ["channels:\n"] if self.blocks else ["channels: []\n"]
        return "".join(self.head + tail + [ln for b in self.blocks for ln in b])

    def pop(self, rec_id: str) -> None:
        i = self.ids().index(rec_id)
        del self.blocks[i], self.records[i]

    def append(self, block: list[str], rec: dict) -> None:
        block = list(block)
        if not block[-1].endswith("\n"):
            block[-1] += "\n"
        self.blocks.append(block)
        self.records.append(rec)


def set_review(block: list[str], updates: dict[str, str]) -> list[str]:
    """Replace (or add) single-line values inside the record's `  review:` mapping; touch nothing else."""
    lines = list(block)
    if not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    start = next((i for i, ln in enumerate(lines) if ln.rstrip("\n") == "  review:"), None)
    if start is None:
        raise RecordError("record has no review section")
    end = start + 1
    while end < len(lines) and (lines[end].startswith("    ") or lines[end].strip() == ""):
        end += 1
    for key, value in updates.items():
        new = f"    {key}: {value}\n"
        idx = next((i for i in range(start + 1, end) if re.match(rf"    {re.escape(key)}:(\s|$)", lines[i])), None)
        if idx is None:
            lines.insert(end, new)
            end += 1
            continue
        stop = idx + 1  # a value folded over several lines owns the more deeply indented lines after it
        while stop < end and lines[stop].startswith("     "):
            stop += 1
        lines[idx:stop] = [new]
        end -= (stop - idx) - 1
    return lines


def decide_block(block: list[str], rec: dict, status: str, today: str, basis: str | None = None,
                 reason: str = "") -> tuple[list[str], dict]:
    """The record as it must look after the owner's decision, with every other field unchanged."""
    updates = {"status": status, "reviewer": "owner", "reviewed_at": scalar(today)}
    expect = deepcopy(rec)
    review = expect.setdefault("review", {})
    review.update(status=status, reviewer="owner", reviewed_at=today)
    if status == "approved":
        updates.update(authorization_basis=str(basis), adult_screening="pass", rejection_reason="''")
        review.update(authorization_basis=basis, adult_screening="pass", rejection_reason="")
    else:
        updates["rejection_reason"] = scalar(reason)
        review["rejection_reason"] = clean(reason, 2100)
    out = set_review(block, updates)
    if _load_block(out) != expect:
        raise RecordError(f"edit of {rec.get('id')} did not come out as intended")
    return out, expect


def suggestion_record(f: dict, today: str, issue_url: str, rec_id: str) -> dict:
    return {
        "id": rec_id, "name": f["name"], "broadcaster": "unknown", "country": f["country"],
        "languages": f["languages"], "categories": [f["category"]], "website": f["website"],
        "stream": {"url": f["stream"], "protocol": f["protocol"], "quality": "unknown"},
        "source": {"reference_url": issue_url, "evidence_url": f["evidence"], "evidence_summary": f["summary"],
                   "evidence_checked": None, "notes": "", "method": SUGGEST_METHOD},
        "epg_id": None, "logo": None, "geo": {"restricted": f["restricted"], "regions": f["regions"]},
        "is_nsfw": False, "first_discovered": today,
        "review": {"status": "pending", "reviewer": SUGGEST_METHOD, "reviewed_at": today,
                   "authorization_basis": "none", "adult_screening": "uncertain", "notes": f["review_note"],
                   "rejection_reason": ""},
    }


def suggestion_block(rec: dict) -> list[str]:
    s, r, g, rv = rec["stream"], rec["source"], rec["geo"], rec["review"]
    restricted = str(g["restricted"]).lower() if isinstance(g["restricted"], bool) else "unknown"
    lines = [f"- id: {scalar(rec['id'])}", f"  name: {scalar(rec['name'])}", f"  broadcaster: {scalar(rec['broadcaster'])}",
             f"  country: {scalar(rec['country'])}", "  languages:", *[f"  - {scalar(x)}" for x in rec["languages"]],
             "  categories:", *[f"  - {scalar(x)}" for x in rec["categories"]], f"  website: {scalar(rec['website'])}",
             "  stream:", f"    url: {scalar(s['url'])}", f"    protocol: {s['protocol']}", "    quality: unknown",
             "  source:", f"    reference_url: {scalar(r['reference_url'])}", f"    evidence_url: {scalar(r['evidence_url'])}",
             f"    evidence_summary: {scalar(r['evidence_summary'])}", "    evidence_checked: null", "    notes: ''",
             f"    method: {SUGGEST_METHOD}", "  epg_id: null", "  logo: null", "  geo:", f"    restricted: {restricted}",
             "    regions:" if g["regions"] else "    regions: []", *[f"    - {scalar(x)}" for x in g["regions"]],
             "  is_nsfw: false", f"  first_discovered: {scalar(rec['first_discovered'])}", "  review:",
             "    status: pending", f"    reviewer: {SUGGEST_METHOD}", f"    reviewed_at: {scalar(rv['reviewed_at'])}",
             "    authorization_basis: none", "    adult_screening: uncertain", f"    notes: {scalar(rv['notes'])}",
             "    rejection_reason: ''"]
    block = [ln + "\n" for ln in lines]
    if _load_block(block) != rec:
        raise RecordError("generated record does not parse back to the intended record")
    return block


# ====================================================================== URL policy
def url_problems(url: str, *, https_only: bool, tokens: bool) -> list[str]:
    """Why a URL cannot be accepted (empty list = acceptable). Static checks only, no network access."""
    if not url:
        return ["is missing"]
    if len(url) > 2048 or any(ch.isspace() or ord(ch) < 32 for ch in url):
        return ["is not a single clean link"]
    try:
        p = urlsplit(url)
        port = p.port
    except ValueError:
        return ["is a malformed link"]
    scheme = p.scheme.lower()
    if scheme != "https" and (https_only or scheme != "http"):
        return ["must be an https link" if https_only else "must be an http(s) link"]
    if p.username or p.password or "@" in p.netloc:
        return ["contains credentials"]
    host = (p.hostname or "").lower().rstrip(".")
    if not host:
        return ["has no host"]
    try:
        ipaddress.ip_address(host.strip("[]"))
        return ["uses an IP address instead of a domain name"]
    except ValueError:
        pass
    if "." not in host or host.endswith(BLOCKED_SUFFIXES) or not re.fullmatch(r"[a-z0-9.-]+", host) or ".." in host:
        return ["does not use a public domain name"]
    if port not in (None, 80, 443, 8080, 8443):
        return [f"uses port {port}"]
    if DENY_HOSTS.search(host):
        return [f"is on {host}, a restream, redirect or platform host"]
    if tokens:
        bad = [k for k, _ in parse_qsl(p.query, keep_blank_values=True) if k.lower() in TOKEN_PARAMS]
        if bad:
            return [f"contains a token or credential parameter ({bad[0]})"]
    return []


# ====================================================================== form parsing
def form_fields(body: str) -> dict[str, str]:
    """`### Label` sections of an issue-form body, keyed by lower-case label."""
    out: dict[str, str] = {}
    label, buf = None, []
    for ln in (body or "").replace("\r", "").split("\n"):
        if ln.startswith("### "):
            if label is not None:
                out[label] = "\n".join(buf).strip()
            label, buf = ln[4:].strip().lower(), []
        elif label is not None:
            buf.append(ln)
    if label is not None:
        out[label] = "\n".join(buf).strip()
    return {k: ("" if v == "_No response_" else v) for k, v in out.items()}


def field_of(fields: dict[str, str], prefix: str) -> str:
    return next((v for k, v in fields.items() if k.startswith(prefix)), "")


def _labels(issue: dict) -> set[str]:
    return {str(lab.get("name", "")).lower() for lab in issue.get("labels", []) if isinstance(lab, dict)}


def is_suggestion(issue: dict) -> bool:
    body = (issue.get("body") or "").lower()
    return SUGGEST_LABEL in _labels(issue) or ("### official channel name" in body and "### stream url" in body)


def is_broken_report(issue: dict) -> bool:
    body = (issue.get("body") or "").lower()
    return BROKEN_LABEL in _labels(issue) or ("### channel name or id" in body and "### what happens?" in body)


@dataclass
class Reference:
    countries: set[str]
    languages: set[str]
    categories: set[str]


def parse_reference(countries_csv: str, reference_py: str) -> Reference:
    """Valid country, language and category codes, read from the catalogue's own reference tables."""
    countries = {(row.get("code") or "").strip().lower() for row in csv.DictReader(io.StringIO(countries_csv))}
    countries.discard("")

    def table(name: str) -> set[str]:
        m = re.search(rf"^{name}\b[^\n]*=\s*\{{\n(.*?)^\}}", reference_py, re.S | re.M)
        return set(re.findall(r'^\s+"([a-z-]+)":', m.group(1), re.M)) if m else set()

    ref = Reference(countries, table("LANGUAGES"), table("CATEGORIES"))
    if len(ref.countries) < 100 or len(ref.languages) < 20 or len(ref.categories) < 5:
        raise RecordError("could not read the catalogue's reference tables")
    return ref


@dataclass
class Suggestion:
    fields: dict[str, Any] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


def validate_suggestion(body: str, ref: Reference) -> Suggestion:
    raw = form_fields(body)
    bad: list[str] = []
    name = clean(field_of(raw, "official channel name"), 200)
    if not name:
        bad.append("The channel name is missing.")
    elif len(name) > 120:
        bad.append("The channel name is longer than 120 characters.")
    elif ADULT.search(name):
        bad.append("Adult-oriented channels are never listed.")
    country = clean(field_of(raw, "country")).lower()
    if country not in ref.countries:
        bad.append(f"The country must be a two-letter ISO 3166-1 code such as `de` (got {code(country, 40)}).")
    langs = [x for x in re.split(r"[,;\s]+", clean(field_of(raw, "language")).lower()) if x]
    if not langs or len(set(langs)) != len(langs) or any(x not in ref.languages for x in langs):
        bad.append("The language must be one or more two-letter ISO 639-1 codes such as `de, en` that the catalogue supports.")
    category = clean(field_of(raw, "main category")).lower()
    if category not in ref.categories or category not in CATEGORY_OPTIONS:
        bad.append("Please pick one of the listed categories.")

    stream = clean(field_of(raw, "stream url"), 2100)
    website = clean(field_of(raw, "official broadcaster website"), 2100)
    evidence = clean(field_of(raw, "evidence"), 2100)
    path = urlsplit(stream).path.lower()
    stream_issue = url_problems(stream, https_only=True, tokens=True)
    if not stream_issue and not path.endswith((".m3u8", ".mpd")):
        stream_issue = ["must link to a .m3u8 (HLS) or .mpd (DASH) playlist"]
    if stream_issue:
        bad.append(f"The stream URL {stream_issue[0]}.")
    page_issue = False
    for label, value in (("official website", website), ("evidence link", evidence)):
        issue = url_problems(value, https_only=True, tokens=False)
        if issue:
            page_issue = True
            bad.append(f"The {label} {issue[0]}." if value else
                       f"The {label} is missing. It must be the broadcaster's own page that plays this stream free of charge.")
    note = ""
    if not page_issue:
        w, e = (urlsplit(u).hostname or "" for u in (website, evidence))
        if THIRD_PARTY.search(w) or THIRD_PARTY.search(e):
            bad.append("The website and the evidence must be the broadcaster's own pages, not a social network, video "
                       "platform or code host.")
        elif base_domain(w) != base_domain(e):
            bad.append(f"The evidence page must be on the broadcaster's own domain ({code(base_domain(w), 60)}), "
                       f"but it is on {code(base_domain(e), 60)}.")
        sh = (urlsplit(stream).hostname or "").lower()
        if not stream_issue and base_domain(sh) != base_domain(w):
            note = f" The stream host {sh} is not on the website's domain; check that it is the broadcaster's own CDN."
    confirms = field_of(raw, "confirmations")
    if len(re.findall(r"^\s*-\s*\[[xX]\]", confirms, re.M)) < 3 or re.search(r"^\s*-\s*\[ \]", confirms, re.M):
        bad.append("All three confirmations must be ticked (not adult-oriented, free without login, not a restream or "
                   "tokenised link).")
    geo = clean(field_of(raw, "region limits")).lower()
    restricted: Any = False if geo.startswith("works worldwide") else True if geo.startswith("limited") else "unknown"
    stream_host = urlsplit(stream).hostname or ""
    return Suggestion({
        "name": name, "country": country, "languages": langs, "category": category, "stream": stream,
        "protocol": "dash" if path.endswith(".mpd") else "hls", "website": website, "evidence": evidence,
        "restricted": restricted, "regions": [country] if restricted is True else [],
        "summary": clean(f"Community suggestion: the reporter says {evidence} offers this stream free of charge "
                         f"(stream host {stream_host}). Not yet checked by a maintainer.", 600),
        "review_note": clean("Suggested by a reader through the suggestion form; awaiting the owner's review." + note, 600),
    }, bad)


def slug(name: str, country: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower())
    base = base.strip("-")[:56].strip("-")
    for cand in (base, f"{base}-{country}", *[f"{base}-{country}-{n}" for n in range(2, 50)]):
        if ID_RE.match(cand) and cand not in taken:
            return cand
    raise RecordError("could not find a free id")


# ====================================================================== GitHub access, limited to one repository
class Scoped:
    """Passes requests through only when they target the one configured repository."""

    def __init__(self, gh: Any, repo: str) -> None:
        if not REPO_RE.fullmatch(repo) or ".." in repo:
            raise ValueError(f"invalid repository name {repo!r}")
        self._gh, self.repo = gh, repo
        self._prefix = f"/repos/{repo}".lower()

    def _check(self, path: str) -> None:
        p = path.lower().split("?")[0]
        if not (p == self._prefix or p.startswith(self._prefix + "/")) or ".." in p.split("/"):
            raise PermissionError(f"{path} is outside {self.repo}")

    def request(self, method: str, path: str, **kw: Any) -> Any:
        self._check(path)
        return self._gh.request(method, path, **kw)

    def get(self, path: str, **params: Any) -> Any:
        self._check(path)
        return self._gh.get(path, **params)

    def paged(self, path: str, *a: Any, **kw: Any) -> list[dict]:
        self._check(path)
        return self._gh.paged(path, *a, **kw)

    def comment(self, number: int, body: str) -> Any:
        return self.request("POST", f"/repos/{self.repo}/issues/{int(number)}/comments", json={"body": body})


class Catalogue:
    """The repository's default branch as of one commit: reads files, and commits edits together."""

    def __init__(self, api: Scoped) -> None:
        self.api, self.repo = api, api.repo
        self.branch = self.head = self.tree = ""
        self.paths: set[str] = set()
        self.texts: dict[str, str] = {}
        self.files: dict[str, RecordFile] = {}
        self.loaded = False

    def reload(self) -> None:
        r = self.repo
        self.branch = self.api.get(f"/repos/{r}")["default_branch"]
        self.head = self.api.get(f"/repos/{r}/git/ref/heads/{self.branch}")["object"]["sha"]
        self.tree = self.api.get(f"/repos/{r}/git/commits/{self.head}")["tree"]["sha"]
        listing = self.api.get(f"/repos/{r}/git/trees/{self.tree}", recursive=1)
        if listing.get("truncated"):
            raise RecordError("repository listing is incomplete")
        self.paths = {t["path"] for t in listing["tree"] if t.get("type") == "blob"}
        self.texts, self.files, self.loaded = {}, {}, True

    def _ensure(self) -> None:
        if not self.loaded:
            self.reload()

    def text(self, path: str) -> str | None:
        self._ensure()
        if path in self.texts:
            return self.texts[path]
        if path not in self.paths:
            return None
        data = self.api.get(f"/repos/{self.repo}/contents/{quote(path)}", ref=self.head)
        if not isinstance(data, dict) or data.get("encoding") != "base64":
            raise RecordError(f"{path} could not be read")
        self.texts[path] = text = base64.b64decode(data.get("content", "")).decode("utf-8")
        return text

    def record_file(self, path: str) -> RecordFile | None:
        if path not in self.files:
            text = self.text(path)
            if text is None:
                return None
            self.files[path] = RecordFile.parse(path, text)
        return self.files[path]

    def record_paths(self, folder: str) -> list[str]:
        self._ensure()
        return sorted(p for p in self.paths if p.startswith(f"data/{folder}/") and p.endswith(".yaml"))

    def records(self, folders: tuple[str, ...] = DATA_DIRS) -> list[tuple[str, dict]]:
        out: list[tuple[str, dict]] = []
        for folder in folders:
            for path in self.record_paths(folder):
                rf = self.record_file(path)
                out += [(folder, r) for r in (rf.records if rf else [])]
        return out

    def commit(self, message: str, changes: dict[str, str], parsed: dict[str, RecordFile]) -> str:
        """One commit on the default branch holding every change. The branch is never force-updated."""
        self._ensure()
        r = self.repo
        entries = [{"path": p, "mode": "100644", "type": "blob", "content": t} for p, t in sorted(changes.items())]
        tree = self.api.request("POST", f"/repos/{r}/git/trees", json={"base_tree": self.tree, "tree": entries})
        commit = self.api.request("POST", f"/repos/{r}/git/commits",
                                  json={"message": message, "tree": tree["sha"], "parents": [self.head]})
        try:
            self.api.request("PATCH", f"/repos/{r}/git/refs/heads/{self.branch}", json={"sha": commit["sha"], "force": False})
        except GitHubError as exc:
            if " 422" in str(exc) or " 409" in str(exc):
                raise Conflict(str(exc)) from exc
            raise
        self.head, self.tree = commit["sha"], tree["sha"]
        self.paths |= set(changes)
        self.texts.update(changes)
        self.files.update(parsed)
        return commit["sha"]


# ====================================================================== owner commands
CMD = re.compile(r"^(approve|reject)\b\s*(.*)$", re.I)


@dataclass
class Command:
    action: str
    rec_id: str
    arg: str = ""
    error: str = ""


def parse_commands(body: str, limit: int = 20) -> list[Command]:
    out: list[Command] = []
    for raw in (body or "").replace("\r", "").split("\n"):
        line = raw.strip()
        m = CMD.match(line)
        if not m:
            continue
        action, parts = m.group(1).lower(), m.group(2).strip().split(None, 1)
        cmd = Command(action, parts[0].lower() if parts else "", parts[1].strip() if len(parts) > 1 else "")
        if not ID_RE.match(cmd.rec_id):
            cmd.error = f"Could not read a channel id in {code(line)}. Use `{action} <id>`."
        elif action == "approve" and cmd.arg and cmd.arg.lower() not in BASES:
            cmd.error = f"{code(cmd.arg, 60)} is not an authorization basis. Use one of: " + ", ".join(f"`{b}`" for b in BASES)
        elif action == "reject" and len(clean(cmd.arg)) < 3:
            cmd.error = f"`reject {cmd.rec_id}` needs a reason, for example `reject {cmd.rec_id} restream, not the broadcaster`."
        out.append(cmd)
        if len(out) >= limit:
            break
    return out


def is_bot(user: dict | None) -> bool:
    if not user:
        return True
    return user.get("type") == "Bot" or str(user.get("login", "")).endswith("[bot]")


def is_owner_comment(comment: dict, owner_login: str) -> bool:
    """Only the owner's own account counts: exact login, a user account (never a bot) and the OWNER association."""
    user = comment.get("user") or {}
    return bool(owner_login) and user.get("login") == owner_login and user.get("type") == "User" \
        and comment.get("author_association") == "OWNER"


def bot_marked(comments: list[dict], mark: str) -> bool:
    return any(mark in (c.get("body") or "") and is_bot(c.get("user")) for c in comments)


# ====================================================================== broken stream reports
def lifecycle_note(entry: dict) -> str:
    health = entry.get("health") or {}
    meaning = {"working": "passing its recent checks", "intermittent": "passing some recent checks and failing others",
               "restricted": "limited to some regions, so it can fail from elsewhere",
               "unreachable": "failing its recent checks", "unknown": "not checked often enough yet to say"}
    parts = [f"it is {meaning.get(entry.get('lifecycle'), 'in a state this note does not know')}"]
    if isinstance(health.get("success_rate"), (int, float)) and health.get("checks_in_window"):
        parts.append(f"{round(health['success_rate'] * 100)}% of its last {health['checks_in_window']} checks succeeded")
    if health.get("last_status"):
        parts.append(f"the latest result was {clean(health['last_status'], 40)}")
    return "; ".join(parts) + "."


def find_channel(channels: list[dict], query: str) -> tuple[dict | None, list[dict]]:
    """(the one matching channel, or None) and the candidates when several match."""
    q = clean(query, 200).lower()
    key = norm_name(q)
    if not key:
        return None, []
    exact = [c for c in channels if str(c.get("id", "")).lower() == q or norm_name(str(c.get("name", ""))) == key]
    if len(exact) == 1:
        return exact[0], []
    if exact:
        return None, exact
    part = [c for c in channels if key in norm_name(str(c.get("name", "")))]
    return (part[0], []) if len(part) == 1 else (None, part)


# ====================================================================== weekly issue text
def render_review(rows: list[dict], limit: int) -> str:
    rows = sorted(rows, key=lambda r: ((r.get("source") or {}).get("method") == "automated-domain-match",
                                       str(r.get("country")), str(r.get("id"))))
    out = [MARK_REVIEW, f"{len(rows)} channel records are waiting for a decision in `data/pending`. "
           "Nothing here is published until it is approved.", "",
           "| id | country | name | categories | evidence | stream host | basis | why pending |",
           "|---|---|---|---|---|---|---|---|"]
    for r in rows[:limit]:
        src, rv = r.get("source") or {}, r.get("review") or {}
        ev = str(src.get("evidence_url") or "")
        link = f"[page]({safe_link(ev)})" if ev.startswith(("https://", "http://")) else "none"
        host = urlsplit(str((r.get("stream") or {}).get("url") or "")).hostname or "?"
        why = rv.get("notes") or src.get("evidence_summary") or "no note"
        cats = ", ".join(str(c) for c in (r.get("categories") or []))
        out.append(f"| `{md_cell(r.get('id'), 64)}` | {md_cell(r.get('country'), 4)} | {md_cell(r.get('name'), 60)} | "
                   f"{md_cell(cats, 40)} | {link} | `{md_cell(host, 60)}` | {md_cell(rv.get('authorization_basis'), 30)} | "
                   f"{md_cell(why, 180)} |")
    if len(rows) > limit:
        out.append(f"\n{len(rows) - limit} more records are not listed here; the next review shows the rest.")
    out += ["", "### How to decide", "",
            "Approve only when the broadcaster's own page plays this stream free of charge without a login, and the "
            "stream host belongs to the broadcaster or its CDN (see `docs/SOURCE_POLICY.md`). Reject restreams, "
            "IP-address hosts, tokenised links, pay-TV and adult channels. If you are unsure, leave the record pending.",
            "", "### Commands", "", "Comment on this issue, one command per line, as many as you like:", "",
            "```", "approve <id>", "approve <id> <basis>", "reject <id> <reason>", "```", "",
            "A record whose basis is `none` needs one in the command: " + ", ".join(f"`{b}`" for b in BASES) + ". "
            "Each decision moves the record to `data/approved` or `data/rejected` in its own commit, and the result is "
            "posted as a reply here. Only comments from the repository owner are acted on."]
    return "\n".join(out)[:60000]


# ====================================================================== the workflow
class ChannelReview:
    def __init__(self, gh: Any, cfg: dict[str, Any], owner: str, now: datetime, ledger: Any, report: Any,
                 dry_run: bool = False) -> None:
        self.cfg, self.now, self.ledger, self.report, self.dry = cfg, now, ledger, report, dry_run
        repo = str(cfg.get("repo", ""))
        if repo.split("/")[0].lower() != owner.lower():
            raise ValueError(f"{repo!r} does not belong to {owner}")
        self.api = Scoped(gh, repo)
        self.repo = repo
        self.owner_login = str(cfg.get("owner_login") or owner)
        self.cat = Catalogue(self.api)
        self.today = now.date().isoformat()
        state = ledger.get("iptv_review", {}) if ledger is not None else {}
        state = state if isinstance(state, dict) else {}
        self.done_cmds = [x for x in state.get("cmd", []) if isinstance(x, int)]
        self.done_sug = {str(k): str(v) for k, v in (state.get("sug") or {}).items()}
        self.done_brk = [x for x in state.get("brk", []) if isinstance(x, int)]
        self.week_done = str(state.get("week", ""))
        rows = cfg.get("max_rows", 80)
        self.max_rows = max(10, min(rows, 300)) if isinstance(rows, int) else 80
        self._issues: list[dict] | None = None

    # -------------------------------------------------------------- plumbing
    def _save(self) -> None:
        self.ledger.set("iptv_review", {"cmd": self.done_cmds[-500:], "sug": dict(list(self.done_sug.items())[-400:]),
                                        "brk": self.done_brk[-400:], "week": self.week_done})

    def _act(self, text: str) -> None:
        self.report.act(("[dry run] " if self.dry else "") + f"{self.repo}: {text}")

    def _reply(self, number: int, body: str, what: str) -> None:
        self._act(f"#{number} {what}")
        if not self.dry:
            self.api.comment(number, body + FOOTER)

    def run(self) -> None:
        if self.ledger is None:
            raise RuntimeError("no ledger available, so channel review stays off")
        steps = [("commands", True, self.commands),
                 ("suggestions", self.cfg.get("suggestions", True) is True, self.suggestions),
                 ("broken reports", self.cfg.get("broken_reports", True) is True, self.broken),
                 ("weekly issue", True, self.weekly_issue)]
        for name, wanted, step in steps:
            if not wanted:
                continue
            try:
                step()
            except (GitHubError, RecordError, PermissionError, Conflict, ValueError) as exc:
                self.report.errors.append(f"iptv review {name}: {clean(exc, 300)}")
        self._save()

    # -------------------------------------------------------------- weekly issue
    def weekday(self) -> int:
        value = self.cfg.get("weekday", "monday")
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 6:
            return value
        return WEEKDAYS.index(str(value).lower()) if str(value).lower() in WEEKDAYS else 0

    def review_issues(self) -> list[dict]:
        found = self.api.paged(f"/repos/{self.repo}/issues", state="all", labels=REVIEW_LABEL, limit=30)
        return [i for i in found if "pull_request" not in i and MARK_REVIEW in (i.get("body") or "") and is_bot(i.get("user"))]

    def weekly_issue(self) -> None:
        week = self.now.strftime("%G-W%V")
        if self.week_done == week or self.now.weekday() < self.weekday():
            return
        title = f"Channel review {week}"
        issues = self.review_issues()
        if not any(i["title"] == title for i in issues):
            rows = [r for _, r in self.cat.records(("pending",))]
            if rows:
                self._act(f"create {title} ({len(rows)} pending records)")
                if not self.dry:
                    self.api.request("POST", f"/repos/{self.repo}/issues",
                                     json={"title": title, "body": render_review(rows, self.max_rows),
                                           "labels": [REVIEW_LABEL]})
        for old in issues:  # last week's issue closes whether or not there is a new one
            if old.get("state") == "open" and old["title"] != title:
                self._act(f"#{old['number']} close previous channel review")
                if not self.dry:
                    self.api.request("PATCH", f"/repos/{self.repo}/issues/{old['number']}",
                                     json={"state": "closed", "state_reason": "completed"})
        self.week_done = week

    # -------------------------------------------------------------- owner commands
    def commands(self) -> None:
        for issue in self.review_issues():
            if issue.get("state") != "open":
                continue
            n = issue["number"]
            comments = self.api.paged(f"/repos/{self.repo}/issues/{n}/comments", limit=300)
            for c in sorted(comments, key=lambda c: c["id"]):
                if not is_owner_comment(c, self.owner_login) or c["id"] in self.done_cmds:
                    continue
                mark = f"<!-- nexonic-sentinel:iptv-cmd:{c['id']} -->"
                cmds = [] if bot_marked(comments, mark) else parse_commands(c.get("body") or "")
                if cmds:
                    lines = [self._run_command(cmd) for cmd in cmds]
                    self._reply(n, mark + "\n" + "\n".join(f"- {ln}" for ln in lines), f"answer comment {c['id']}")
                self.done_cmds.append(c["id"])

    def _run_command(self, cmd: Command) -> str:
        if cmd.error:
            return cmd.error
        for _ in range(2):
            try:
                return self._decide(cmd)
            except Conflict:
                self.cat.reload()
            except (RecordError, GitHubError) as exc:
                self.report.errors.append(f"iptv review {cmd.action} {cmd.rec_id}: {clean(exc, 300)}")
                return f"Could not {cmd.action} `{cmd.rec_id}`: no change was confirmed. {clean(exc, 160)}"
        return f"Could not change `{cmd.rec_id}` because the repository kept changing. Send the command again."

    def _locate(self, rec_id: str) -> tuple[RecordFile, dict] | None:
        for path in self.cat.record_paths("pending"):
            rf = self.cat.record_file(path)
            if rf and rec_id in rf.ids():
                return rf, rf.records[rf.ids().index(rec_id)]
        return None

    def _decide(self, cmd: Command) -> str:
        found = self._locate(cmd.rec_id)
        if found is None:
            return f"`{cmd.rec_id}` is not in `data/pending` (unknown id, or already decided). Nothing changed."
        src, rec = found
        block = src.blocks[src.ids().index(cmd.rec_id)]
        cc = str(rec.get("country", ""))
        if not re.fullmatch(r"[a-z]{2}", cc):
            raise RecordError("the record has no valid country code")
        if cmd.action == "approve":
            problem = self._approval_problem(rec, cmd)
            if problem:
                return f"Not approved: `{cmd.rec_id}` {problem}"
            basis = cmd.arg.lower() or str((rec.get("review") or {}).get("authorization_basis"))
            new_block, new_rec = decide_block(block, rec, "approved", self.today, basis)
            auto = (rec.get("source") or {}).get("method") == "automated-domain-match"
            dest_path = f"data/approved/{'auto/' if auto else ''}{cc}.yaml"
            header = f"# Approved channels for {cc.upper()}. See docs/SOURCE_POLICY.md before editing."
            message, done = f"Approve channel {cmd.rec_id}", f"Approved `{cmd.rec_id}` ({basis})"
        else:
            new_block, new_rec = decide_block(block, rec, "rejected", self.today, reason=clean(cmd.arg, 300))
            dest_path = f"data/rejected/{cc}.yaml"
            header = "# Rejected candidates kept so they are not re-added. Never published."
            message, done = f"Reject channel {cmd.rec_id}", f"Rejected `{cmd.rec_id}`"
        if any(r["id"] == cmd.rec_id for _, r in self.cat.records(("approved", "rejected"))):
            raise RecordError("this id already exists in the approved or rejected records")
        dest = self.cat.record_file(dest_path) or RecordFile.new(dest_path, header)
        src2, dest2 = src.copy(), dest.copy()  # edit copies, so a failed commit leaves the cache as it was
        src2.pop(cmd.rec_id)
        dest2.append(new_block, new_rec)
        changes = {src2.path: src2.render(), dest2.path: dest2.render()}
        for rf in (src2, dest2):  # the files must parse back to exactly the intended records
            if RecordFile.parse(rf.path, changes[rf.path]).records != rf.records:
                raise RecordError(f"{rf.path} did not come out as intended")
        self._act(message)
        if self.dry:
            return f"{done} (dry run, nothing written)."
        sha = self.cat.commit(message, changes, {src2.path: src2, dest2.path: dest2})
        return f"{done} in [{sha[:7]}](https://github.com/{self.repo}/commit/{sha})."

    def _approval_problem(self, rec: dict, cmd: Command) -> str:
        review = rec.get("review") or {}
        if rec.get("is_nsfw") or review.get("adult_screening") == "fail":
            return "is marked adult-oriented, and those are never approved."
        basis = cmd.arg.lower() or str(review.get("authorization_basis") or "")
        if basis not in BASES:
            return ("has no authorization basis yet. Repeat the command with one: "
                    + ", ".join(f"`approve {cmd.rec_id} {b}`" for b in BASES) + ".")
        stream = str((rec.get("stream") or {}).get("url") or "")
        issues = url_problems(stream, https_only=False, tokens=True)
        if issues:
            return f"has a stream link that the source policy does not allow (it {issues[0]}). Reject it instead."
        clash = self._clash(rec, "approved")
        if clash:
            return f"would duplicate the approved channel `{clash['id']}` (same stream link, or same name and country)."
        return ""

    def _clash(self, rec: dict, folder: str) -> dict | None:
        url = norm_url(str((rec.get("stream") or {}).get("url") or ""))
        key = (rec.get("country"), norm_name(str(rec.get("name", ""))))
        for _, other in self.cat.records((folder,)):
            stream = other.get("stream")
            same_url = isinstance(stream, dict) and norm_url(str(stream.get("url") or "")) == url
            same_name = (other.get("country"), norm_name(str(other.get("name", "")))) == key
            if other.get("id") != rec.get("id") and (same_url or same_name):
                return other
        return None

    # -------------------------------------------------------------- suggestions and broken reports
    def _open_issues(self) -> list[dict]:
        if self._issues is None:
            found = self.api.paged(f"/repos/{self.repo}/issues", state="open", limit=200)
            self._issues = [i for i in found if "pull_request" not in i and not is_bot(i.get("user"))]
        return self._issues

    def suggestions(self) -> None:
        todo = []
        for i in self._open_issues():
            digest = hashlib.sha256((i.get("body") or "").encode()).hexdigest()[:12]
            if is_suggestion(i) and self.done_sug.get(str(i["number"])) != digest:
                todo.append((i, digest))
        if not todo:
            return
        if SUGGEST_METHOD not in (self.cat.text("nexonic/models.py") or ""):
            self.report.flagged.append(f"{self.repo}: channel suggestions wait until nexonic/models.py accepts "
                                       f"source.method `{SUGGEST_METHOD}`")
            return
        ref = parse_reference(self.cat.text("nexonic/reference/countries.csv") or "",
                              self.cat.text("nexonic/reference/__init__.py") or "")
        for issue, digest in todo[:10]:
            n = issue["number"]
            mark = f"<!-- nexonic-sentinel:iptv-suggestion:{digest} -->"
            if not bot_marked(self.api.paged(f"/repos/{self.repo}/issues/{n}/comments", limit=100), mark):
                reply = self._suggest_one(issue, ref)
                if reply is None:  # the branch kept moving; try again on the next run
                    continue
                self._reply(n, mark + "\n" + reply, "answer channel suggestion")
            self.done_sug[str(n)] = digest

    def _suggest_one(self, issue: dict, ref: Reference, retry: bool = True) -> str | None:
        n = issue["number"]
        url = str(issue.get("html_url") or f"https://github.com/{self.repo}/issues/{n}")
        sug = validate_suggestion(issue.get("body") or "", ref)
        if sug.problems:
            return ("Thanks for the suggestion. It cannot be queued yet:\n\n" + "\n".join(f"- {p}" for p in sug.problems)
                    + "\n\nEdit the issue to fix these and it will be checked again. A suggestion is only queued when the "
                    "stream is offered free by the broadcaster itself (see `docs/SOURCE_POLICY.md`).")
        f = sug.fields
        known = self.cat.records()
        by_url = {norm_url(str(r["stream"].get("url") or "")): (d, r) for d, r in known if isinstance(r.get("stream"), dict)}
        by_name = {(r.get("country"), norm_name(str(r.get("name", "")))): (d, r) for d, r in known}
        hit = by_url.get(norm_url(f["stream"])) or by_name.get((f["country"], norm_name(f["name"])))
        if hit:
            folder, other = hit
            if (other.get("source") or {}).get("reference_url") == url:
                return f"Thanks, this suggestion is already in the review queue as `{other['id']}`."
            why = ""
            if folder == "rejected" and (other.get("review") or {}).get("rejection_reason"):
                why = f" It was rejected because: {clean(other['review']['rejection_reason'], 200)}"
            state = {"approved": "already in the catalogue", "pending": "already waiting for review",
                     "rejected": "one that was reviewed and rejected earlier"}[folder]
            return (f"Thanks for the suggestion. This looks like a channel that is {state} (`{other['id']}`).{why} "
                    "If you think that is wrong, add details here.")
        rec_id = slug(f["name"], f["country"], {r["id"] for _, r in known})
        rec = suggestion_record(f, self.today, url, rec_id)
        path = f"data/pending/{f['country']}.yaml"
        rf = (self.cat.record_file(path) or RecordFile.new(
            path, f"# Candidates awaiting review for {f['country'].upper()}. See docs/SOURCE_POLICY.md before editing.")).copy()
        rf.append(suggestion_block(rec), rec)
        text = rf.render()
        if RecordFile.parse(path, text).records != rf.records:
            raise RecordError(f"{path} did not come out as intended")
        message = f"Add suggested channel {rec_id}"
        self._act(message)
        if self.dry:
            return "(dry run, nothing written)"
        try:
            self.cat.commit(message, {path: text}, {path: rf})
        except Conflict:
            self.cat.reload()
            return self._suggest_one(issue, ref, retry=False) if retry else None
        return ("Thanks for the suggestion. It passed the first checks and has been added to the review queue as "
                f"`{rec_id}`. The owner will review it. If it is approved, it is then checked every day and listed "
                "while it keeps working. Nothing is published before that review.")

    def broken(self) -> None:
        todo = [i for i in self._open_issues() if is_broken_report(i) and i["number"] not in self.done_brk]
        if not todo:
            return
        text = self.cat.text("reports/channels.json")
        if text is None:
            raise RecordError("reports/channels.json is missing")
        data = json.loads(text)
        channels = [c for c in (data.get("channels") if isinstance(data, dict) else None) or [] if isinstance(c, dict)]
        for issue in todo[:10]:
            n = issue["number"]
            if not bot_marked(self.api.paged(f"/repos/{self.repo}/issues/{n}/comments", limit=100), MARK_BROKEN):
                query = field_of(form_fields(issue.get("body") or ""), "channel name or id")
                entry, options = find_channel(channels, query)
                if entry:
                    health = entry.get("health") or {}
                    body = (f"Thanks for the report. The latest automatic check of **{md_cell(entry.get('name'))}** "
                            f"(`{clean(entry.get('id'), 80).replace('`', '')}`):\n\n"
                            f"- Status: **{clean(entry.get('lifecycle'), 40)}**\n"
                            f"- Last checked: {clean(health.get('last_checked') or 'not yet', 40)}\n"
                            f"- Last successful check: {clean(health.get('last_success') or 'none recorded', 40)}\n"
                            f"- Note: {lifecycle_note(entry)}\n\n"
                            "Streams are checked every day, and ones that keep failing are left out of the playlists "
                            "automatically. Many public broadcasters only allow viewers from their own country, which "
                            "is not a broken stream. If it plays on the broadcaster's own website but not in your "
                            "player, please say which player and version you used.")
                elif options:
                    ids = ", ".join(code(c.get("id"), 80) for c in options[:5])
                    body = f"Thanks for the report. More than one channel matches {code(query)}: {ids}. Which one do you mean?"
                else:
                    body = (f"Thanks for the report. I could not match {code(query)} to a channel. Please give the channel "
                            "id shown on the website or in the playlist.")
                self._act(f"#{n} label {REPORT_LABEL}")
                if not self.dry:
                    self.api.request("POST", f"/repos/{self.repo}/issues/{n}/labels", json={"labels": [REPORT_LABEL]})
                self._reply(n, MARK_BROKEN + "\n" + body, "answer stream report")
            self.done_brk.append(n)
