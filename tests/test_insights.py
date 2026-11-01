from __future__ import annotations

from datetime import datetime, timezone

from sentinel import health, insights
from sentinel import settings as repo_settings
from tests.test_bot import FakeGitHub, issue, make

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)


def pr(n, login="alice", updated="2026-10-20T10:00:00Z", draft=False, typ="User"):
    return {"number": n, "title": f"PR {n}", "user": {"login": login, "type": typ}, "draft": draft,
            "updated_at": updated, "created_at": updated, "html_url": f"https://github.com/o/r/pull/{n}"}


def test_stale_prs_skip_bots_owner_drafts_and_fresh_ones():
    prs = [pr(1), pr(2, login="owner"), pr(3, login="dependabot[bot]", typ="Bot"), pr(4, draft=True),
           pr(5, updated="2026-11-01T10:00:00Z"), pr(6, updated="2026-10-01T10:00:00Z")]
    out = insights.stale_prs(prs, NOW, "owner")
    assert [p["number"] for p in out] == [6, 1]  # longest wait first
    assert out[1]["days"] == 12 and out[1]["author"] == "alice"


def test_digest_sections_list_waiting_prs_failing_checks_releases_and_alerts():
    rows = [{"name": "app", "full_name": "o/app", "stale": insights.stale_prs([pr(7)], NOW, "o"), "failing": ["tests"],
             "release": "v1.0.0", "unreleased": 3, "alerts": 2},
            {"name": "lib", "full_name": "o/lib", "stale": [], "failing": [], "release": None, "unreleased": None,
             "alerts": None}]
    text = insights.digest_sections(rows)
    assert "app#7" in text and "by alice" in text and "@alice" not in text and "12 days" in text
    assert "tests" in text and "3 commits since v1.0.0" in text and "2 open" in text


def test_security_section_explains_missing_permission():
    rows = [{"name": "a", "full_name": "o/a", "stale": [], "failing": [], "release": None, "unreleased": None,
             "alerts": None}]
    assert "Dependabot alerts: read" in insights.digest_sections(rows)


def test_activity_page_escapes_names_and_shows_counts():
    rows = [{"name": "<x>", "full_name": "o/<x>", "stars": 2, "issues": 1, "prs": 0, "failing": [], "release": "v2",
             "unreleased": 1, "stale": [], "alerts": None}]
    page = insights.activity_html(rows, {"repositories": 1}, "2026-W45", "now")
    assert "&lt;x&gt;" in page and "<x>" not in page.split("<tbody>")[1]
    assert "v2 (+1 unreleased)" in page and "<b>1</b>" in page


def test_missing_community_files():
    files = health.missing_files(["README.md", "src/a.py"], "app", "main")
    assert set(files) == {"CONTRIBUTING.md", ".github/ISSUE_TEMPLATE/bug_report.yml",
                          ".github/ISSUE_TEMPLATE/feature_request.yml", ".github/pull_request_template.md"}
    assert "branch from `main`" in files["CONTRIBUTING.md"] and "# Contributing to app" in files["CONTRIBUTING.md"]
    have = ["docs/CONTRIBUTING.md", ".github/ISSUE_TEMPLATE/x.yml", ".github/PULL_REQUEST_TEMPLATE.md"]
    assert health.missing_files(have, "app", "main") == {}


def test_repo_templates_are_parsed_and_used_in_triage():
    cfg = '[templates]\ngreeting = "Hey {author}, thanks for {issue} in {repo}!"\nclosing = "We reply within a week."\n' \
          'unknown = "ignored"\nbug_repro = 5\n'
    s = repo_settings.parse(cfg, dict(repo_settings.PUBLIC_DEFAULTS))
    assert s["templates"] == {"greeting": "Hey {author}, thanks for {issue} in {repo}!", "closing": "We reply within a week."}

    fake = FakeGitHub()
    fake.files[".github/nexonic-sentinel.toml"] = cfg
    fake.issues = [issue(1, "Crash on start {weird}", "Traceback")]
    make(fake).run()
    body = next(w for w in fake.writes if w[1].endswith("/1/comments"))[2]["body"]
    assert "Hey @alice, thanks for #1 in owner/app!" in body and "We reply within a week." in body
    assert "To help reproduce it" in body  # templates not set keep the default text


def test_broken_template_is_shown_as_written():
    fake = FakeGitHub()
    fake.files[".github/nexonic-sentinel.toml"] = '[templates]\ngreeting = "Thanks {author} {"\n'
    fake.issues = [issue(2, "Question about setup")]
    make(fake).run()
    body = next(w for w in fake.writes if w[1].endswith("/2/comments"))[2]["body"]
    assert "Thanks {author} {" in body
