# Nexonic Sentinel

**A free GitHub App that looks after your repositories.** It triages issues, reviews pull requests for bugs, scans your code each week, answers questions from your docs and merges safe dependency updates. It runs on GitHub Actions, so there's no server to keep online, and it uses free AI models.

[![CI](https://github.com/Shahrukhalid/nexonic-sentinel/actions/workflows/ci.yml/badge.svg)](https://github.com/Shahrukhalid/nexonic-sentinel/actions/workflows/ci.yml)
![License: MIT](https://img.shields.io/badge/license-MIT-blue)

**[Install Nexonic Sentinel](https://github.com/apps/nexonic-sentinel/installations/new)** on the repositories you choose. It starts in a safe mode: nothing is sent to an AI model and nothing is merged until you switch it on (see [Settings for your repository](#settings-for-your-repository)).

## What it does

| Where | What happens | Needs `ai = true` |
|---|---|---|
| New issues | Adds matching labels (`bug`, `enhancement`, `question`, `documentation`) when they exist, thanks the author, asks for reproduction details on short bug reports and links the docs that look relevant. Possible security reports are pointed to private vulnerability reporting. | no |
| Pull requests | Welcomes first-time contributors. **Bug review:** on every new commit it reads the changed lines and posts the concrete bugs it finds (file, line, why, suggested fix), plus any failing checks. It only comments: it never approves, blocks or pushes code. | review only |
| Weekly code health | Reads a different few source files each week and opens one "Code health report" issue when it finds likely bugs. Findings already reported are never repeated. | yes |
| Discussions (Q&A) | Answers from your README and `docs/` only, lists its sources, and says so when the docs don't cover it instead of guessing. Ideas get a thank-you. | AI answers only |
| Dependabot | Merges the update types you allow once every check has passed, asks Dependabot to rebase when a branch is behind, and flags the rest. | no |

Every finding is checked against the real diff or file before it is posted, so a model cannot point at lines that don't exist. Model output is stripped of @mentions and hidden HTML. The bot never runs code from your repository.

## Settings for your repository

Add `.github/nexonic-sentinel.toml` to a repository to change what the bot does there:

```toml
enabled = true      # false = Nexonic Sentinel leaves this repository alone
triage = true       # label new issues and point to relevant docs
welcome = true      # welcome first-time contributors
discussions = true  # reply in Q&A discussions (AI answers need ai = true)
ai = false          # allow sending issue, discussion and code text to free OpenRouter models
pr_review = true    # AI bug review of pull requests (needs ai = true)
code_scan = true    # weekly AI code health report (needs ai = true)
dependabot = []     # Dependabot updates to merge once all checks pass, e.g. ["patch", "minor"]
```

Without the file, the defaults are the values shown. To stop the bot on a single thread, add the `bot-off` label, or write `bot-off` in a discussion. Uninstalling the app removes all of its access.

## Privacy and data

- **What it reads:** issues, pull requests, discussions, docs and, when `ai = true`, the code it reviews. Only on repositories you installed it on.
- **What leaves GitHub:** only when `ai = true`, the text being reviewed or answered is sent to free models on [OpenRouter](https://openrouter.ai/privacy). Free models may log prompts, so don't enable `ai` on repositories with code you can't share.
- **What is stored:** nothing outside GitHub. Scheduled jobs keep a small ledger issue in this repository with **hashed** repository names, so installations are never revealed. This repository's public Actions logs show details only for its owner's repositories; other installations appear as counts.
- **Limits:** AI use is capped per run and per day, so one installation can't use up the shared free quota.

See also [SECURITY.md](SECURITY.md).

## Contribution finder (owner only)

For the account that runs the bot, a daily "Contribution shortlist" issue in this repository lists open issues on other projects where maintainers asked for help (`good first issue`, `help wanted`). Each comes with a drafted approach and the files to look at. The bot never comments or opens pull requests on those projects. You decide what to work on and submit it yourself.

## Run your own copy

1. Create a GitHub App: webhook off; repository permissions *Contents: read & write*, *Issues: read & write*, *Pull requests: read & write*, *Discussions: read & write*, *Checks: read*, *Commit statuses: read*, *Metadata: read*. Install it where you want it.
2. In your fork, add the variable `SENTINEL_CLIENT_ID` (the App's Client ID), the secret `SENTINEL_PRIVATE_KEY` (the whole `.pem`) and, optionally, the secret `OPENROUTER_API_KEY` (a free key from [OpenRouter](https://openrouter.ai/keys)).
3. Set `owner` and the options in [`sentinel.toml`](sentinel.toml). Then run **Actions → Sentinel → Run workflow** with *dry run* ticked, read the summary, and let the 30-minute schedule take over.

The owner's own repositories use the `[features]` defaults in `sentinel.toml`. `mention_owner = false` keeps comments from sending you notification emails; anything waiting on you is listed in the Monday digest. Set `enabled = false` to stop the bot everywhere.

## Development

```bash
pip install --require-hashes -r requirements.lock && pip install pytest ruff
ruff check sentinel tests && python -m pytest -q
GITHUB_TOKEN=... python -m sentinel --dry-run   # a token that can read your repositories
```

## Licence

MIT
