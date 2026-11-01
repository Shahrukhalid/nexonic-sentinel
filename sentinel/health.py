"""Community files every repository should have: a contributing guide, issue forms and a pull request template.
Missing ones are proposed once per repository in a pull request (label `repo-health`) for the owner to merge."""

from __future__ import annotations

CONTRIBUTING = """# Contributing to {name}

Thanks for helping! Bug reports, ideas, documentation fixes and code are all welcome.

## Reporting a bug or asking for a feature

Open an issue and choose a form. For bugs, include your version, your operating system, the exact steps and any
error output. Security problems: please report them privately (see SECURITY.md if present) instead of in a public issue.

## Changing code

1. Fork the repository and create a branch from `{branch}`.
2. Keep the change focused on one thing, and add or update tests when behaviour changes.
3. Run the tests and make sure they pass (see the README for how).
4. Open a pull request that explains what changed and how you tested it.

Issues labelled `good first issue` or `help wanted` are a good place to start. Comment on one before you begin so
nobody else works on the same thing.

## Code of conduct

Be kind and patient. Disagree with ideas, not with people.
"""

BUG_FORM = """name: Bug report
description: Something doesn't work as expected
labels: [bug]
body:
  - type: textarea
    id: what
    attributes:
      label: What happened?
      description: What did you do, what did you expect, and what happened instead?
    validations:
      required: true
  - type: textarea
    id: steps
    attributes:
      label: Steps to reproduce
      placeholder: "1. ...\\n2. ...\\n3. ..."
    validations:
      required: true
  - type: input
    id: version
    attributes:
      label: Version or commit
  - type: input
    id: os
    attributes:
      label: Operating system
  - type: textarea
    id: logs
    attributes:
      label: Error output or logs
      render: shell
"""

FEATURE_FORM = """name: Feature request
description: Suggest an idea or improvement
labels: [enhancement]
body:
  - type: textarea
    id: problem
    attributes:
      label: What problem would this solve?
    validations:
      required: true
  - type: textarea
    id: idea
    attributes:
      label: Your idea
      description: How would you like it to work?
    validations:
      required: true
"""

PR_TEMPLATE = """## What does this change?

## How did you test it?

- [ ] Tests pass locally
- [ ] Documentation updated if needed
"""


def missing_files(paths: list[str], name: str, branch: str) -> dict[str, str]:
    """Files to add, keyed by path, for whatever the repository doesn't have yet."""
    low = [p.lower() for p in paths]
    out: dict[str, str] = {}
    if not any(p in ("contributing.md", ".github/contributing.md", "docs/contributing.md") for p in low):
        out["CONTRIBUTING.md"] = CONTRIBUTING.format(name=name, branch=branch)
    if not any(p.startswith(".github/issue_template/") for p in low):
        out[".github/ISSUE_TEMPLATE/bug_report.yml"] = BUG_FORM
        out[".github/ISSUE_TEMPLATE/feature_request.yml"] = FEATURE_FORM
    if not any(p.endswith("pull_request_template.md") for p in low):
        out[".github/pull_request_template.md"] = PR_TEMPLATE
    return out
