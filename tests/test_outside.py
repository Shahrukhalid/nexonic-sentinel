from sentinel import outside

OWNER = "Shahrukhalid"


def c(user, body):
    return {"user": {"login": user}, "body": body}


def test_waiting_for_moderator_is_not_for_the_owner():
    who, what = outside.classify({"state": "open"}, ["Azure-Pipeline-Passed", "Validation-Completed"], [], [],
                                 [c("wingetbot", "@wingetbot run"),
                                  c("microsoft-github-policy-service[bot]",
                                    "The check-in policies require a moderator to approve PRs from the community.")], OWNER)
    assert who == "them" and "moderators" in what


def test_author_feedback_label_needs_the_owner():
    who, what = outside.classify({"state": "open"}, ["Needs-Author-Feedback", "Manifest-Validation-Error"], [], [],
                                 [c("wingetbot", "Validation failed")], OWNER)
    assert who == "you" and "needs-author-feedback" in what


def test_owner_reply_hands_it_back():
    who, _ = outside.classify({"state": "open"}, ["Needs-Author-Feedback"], [], [],
                              [c("wingetbot", "Validation failed"), c(OWNER, "Fixed the installer URL")], OWNER)
    assert who == "them"


def test_changes_requested_conflict_and_failed_check():
    asked = [{"user": {"login": "mod"}, "state": "CHANGES_REQUESTED"}]
    assert outside.classify({"state": "open"}, [], asked, [], [], OWNER)[0] == "you"
    assert outside.classify({"state": "open", "mergeable_state": "dirty"}, [], [], [], [], OWNER)[0] == "you"
    failed = [{"name": "Validation", "status": "completed", "conclusion": "failure"}]
    who, what = outside.classify({"state": "open"}, [], [], failed, [], OWNER)
    assert who == "you" and "Validation" in what
    # A later approval by the same reviewer replaces the request.
    reviews = [{"user": {"login": "mod"}, "state": "CHANGES_REQUESTED"}, {"user": {"login": "mod"}, "state": "APPROVED"}]
    assert outside.classify({"state": "open"}, [], reviews, [], [], OWNER)[1] == "approved; waiting for the project to merge it."


def test_finished():
    assert outside.classify({"state": "closed", "merged_at": "2026-11-11T00:00:00Z"}, [], [], [], [], OWNER) == ("done", "merged")
    assert outside.classify({"state": "closed"}, [], [], [], [], OWNER)[0] == "done"


class FakeGH:
    def __init__(self):
        self.calls = []

    def get(self, path, **params):
        self.calls.append(("GET", path, params))
        if path == "/search/issues":
            assert "author:Shahrukhalid" in params["q"] and "-user:Shahrukhalid" in params["q"]
            return {"items": [{"number": 470114, "html_url": "https://github.com/microsoft/winget-pkgs/pull/470114",
                               "repository_url": "https://api.github.com/repos/microsoft/winget-pkgs",
                               "title": "New package", "labels": [{"name": "Needs-Review"}], "pull_request": {}}]}
        if path.endswith("/pulls/470114"):
            return {"state": "open", "head": {"sha": "abc"}, "mergeable_state": "blocked"}
        if path.endswith("/check-runs"):
            return {"check_runs": [{"name": "Validation", "status": "completed", "conclusion": "success"}]}
        return {}

    def paged(self, path, limit=100, **params):
        self.calls.append(("PAGED", path, params))
        if path.endswith("/comments"):
            return [c("microsoft-github-policy-service[bot]", "The check-in policies require a moderator to approve PRs")]
        return []


def test_find_and_check_only_read():
    gh = FakeGH()
    rows = [outside.check(gh, i, OWNER) for i in outside.find(gh, OWNER)]
    assert rows[0]["repo"] == "microsoft/winget-pkgs" and rows[0]["who"] == "them"
    assert all(kind in ("GET", "PAGED") for kind, _, _ in gh.calls)  # nothing is written outside the owner's repos
