# Nexonic Sentinel

**An automated maintainer assistant for GitHub repositories.** It runs on a schedule with GitHub Actions (no server, nothing to keep online) and looks after every repository its GitHub App is installed on: it triages new issues, welcomes first-time contributors, answers questions in Discussions from your own documentation, and merges safe dependency updates once all checks pass.

[![CI](https://github.com/Shahrukhalid/nexonic-sentinel/actions/workflows/ci.yml/badge.svg)](https://github.com/Shahrukhalid/nexonic-sentinel/actions/workflows/ci.yml)
![License: MIT](https://img.shields.io/badge/license-MIT-blue)

## What it does

| Where | What happens |
|---|---|
| New issues | Adds matching labels (`bug`, `enhancement`, `question`, `documentation`) when they exist, thanks the author, asks for reproduction details on short bug reports, links the docs that look relevant, and mentions the owner. Possible security reports are pointed to private vulnerability reporting. |
| Discussions (Q&A) | Answers from the repository's README and `docs/` only, with a free OpenRouter model, and lists its sources. If the docs don't clearly answer it, or the topic is security or legal, it says so and mentions the owner instead of guessing. Ideas get a thank-you. |
| Pull requests | Welcomes first-time contributors. Merges Dependabot patch and minor updates when every check has passed; asks Dependabot to rebase when a branch is behind; flags major updates for a human. |
| Weekly | A digest issue in this repository every Monday: stars, open issues and PRs per repository, and everything waiting on the owner. |

It never accepts its own answers, never closes or deletes anything, never approves workflows from forks, and replies at most once per thread.

## Who it acts as

Everything is done by the GitHub App, so it appears as **nexonic-sentinel[bot]** with a clear "bot" label. It does not use the owner's account or personal token, so nothing it writes can be mistaken for something the owner wrote, and it keeps working when the owner is signed out.

## Setup

1. **Create the GitHub App** (Settings → Developer settings → GitHub Apps → New): webhook off; repository permissions *Contents: read & write*, *Issues: read & write*, *Pull requests: read & write*, *Discussions: read & write*, *Checks: read*, *Commit statuses: read*, *Metadata: read*. Install it on the repositories it should manage.
2. In this repository, add:
   - variable `SENTINEL_CLIENT_ID`: the App's Client ID;
   - secret `SENTINEL_PRIVATE_KEY`: a private key generated on the App's page (the whole `.pem` file);
   - secret `OPENROUTER_API_KEY` (optional): a free [OpenRouter](https://openrouter.ai/keys) key for Discussion answers. Without it the bot still triages and flags questions.
3. Run **Actions → Sentinel → Run workflow** with *dry run* ticked, read the summary, then run it normally. After that it runs by itself every 30 minutes.

## Settings

Everything lives in [`sentinel.toml`](sentinel.toml): the owner to mention, repositories to skip, how far back to look, the per-run limit on AI answers (to stay inside the free 50 requests a day), the free models to try, and which Dependabot updates may be merged. Set `enabled = false` to stop the bot everywhere. Add the `bot-off` label to an issue or PR, or write `bot-off` in a discussion, to stop it on one thread.

## Development

```bash
pip install --require-hashes -r requirements.lock && pip install pytest ruff
ruff check sentinel tests && python -m pytest -q
GITHUB_TOKEN=... python -m sentinel --dry-run   # a token that can read your repositories
```

## Licence

MIT
