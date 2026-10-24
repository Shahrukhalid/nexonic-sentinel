"""Run one pass: python -m sentinel [--dry-run] [--digest]. Reads GITHUB_TOKEN (the App installation token)."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from .bot import Sentinel
from .github import GitHub
from .llm import OpenRouter


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sentinel")
    ap.add_argument("--config", default=str(Path(__file__).resolve().parents[1] / "sentinel.toml"))
    ap.add_argument("--dry-run", action="store_true", help="log what would happen, change nothing")
    ap.add_argument("--digest", action="store_true", help="also post the weekly digest (default: on Mondays)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    cfg = tomllib.loads(Path(args.config).read_text())
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("GITHUB_TOKEN is not set (the workflow creates it from the GitHub App).", file=sys.stderr)
        return 2
    key = os.environ.get("OPENROUTER_API_KEY", "")
    llm = OpenRouter(key, cfg.get("models", [])) if key else None
    dry = args.dry_run or os.environ.get("SENTINEL_DRY_RUN") == "1"

    bot = Sentinel(GitHub(token), cfg, llm=llm, dry_run=dry)
    report = bot.run()
    if args.digest or datetime.now(timezone.utc).weekday() == 0 and os.environ.get("GITHUB_EVENT_NAME") == "schedule":
        bot.digest()

    summary = ["## Nexonic Sentinel run", f"Dry run: {dry}. AI answers: {'on' if llm else 'off (no OPENROUTER_API_KEY)'}.",
               "", "**Actions**"] + [f"- {a}" for a in report.actions or ["Nothing to do."]]
    if report.flagged:
        summary += ["", "**Waiting on the owner**"] + [f"- {f}" for f in report.flagged]
    if report.errors:
        summary += ["", "**Errors**"] + [f"- {e}" for e in report.errors]
    text = "\n".join(summary)
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return 1 if report.errors and not report.actions else 0


if __name__ == "__main__":
    sys.exit(main())
