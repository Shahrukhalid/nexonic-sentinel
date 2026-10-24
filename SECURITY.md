# Security

Please report vulnerabilities privately through GitHub (Security tab, "Report a vulnerability"), not in public issues.

## How the bot handles access

- Each run signs a short-lived JWT with the App's private key, then creates a separate one-hour installation token for each account that installed the app. Tokens are never stored or logged.
- The private key lives only in this repository's Actions secrets.
- The bot never executes code from the repositories it reads. Pull request review and code scans only send text to the model and post comments.
- Model output is treated as untrusted. Findings must point at a real file and line, and @mentions and hidden HTML are removed before anything is posted.
- AI features are off for every installation until the repository sets `ai = true` in `.github/nexonic-sentinel.toml`. Automatic Dependabot merges are off until the repository lists allowed update types.
- Public Actions logs show details only for the owner's repositories. The ledger issue stores hashed repository names only.
