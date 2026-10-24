"""Run one pass over every installation: python -m sentinel [--dry-run] [--digest].

Credentials, in order of preference:
- SENTINEL_CLIENT_ID + SENTINEL_PRIVATE_KEY: the GitHub App itself; every account that installed it is served.
- GITHUB_TOKEN: a single token (local testing); treated as the owner's own installation.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .bot import Report, Sentinel
from .github import GitHub
from .ledger import Ledger
from .llm import OpenRouter

log = logging.getLogger("sentinel")


def installations(cfg: dict) -> list[tuple[str, Callable[[], str]]]:
    """(account login, token getter) pairs, the owner's own account first. Tokens are minted lazily."""
    client_id, key = os.environ.get("SENTINEL_CLIENT_ID", ""), os.environ.get("SENTINEL_PRIVATE_KEY", "")
    if client_id and key:
        from .auth import App
        app = App(client_id, key)
        found = [(i["account"]["login"], i["id"]) for i in app.installations()]
        found.sort(key=lambda t: t[0].lower() != cfg["owner"].lower())
        limit = int(cfg.get("max_installations", 50))
        return [(login, (lambda iid=iid: app.token(iid))) for login, iid in found[:limit]]
    token = os.environ.get("GITHUB_TOKEN", "")
    return [(cfg["owner"], lambda: token)] if token else []


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sentinel")
    ap.add_argument("--config", default=str(Path(__file__).resolve().parents[1] / "sentinel.toml"))
    ap.add_argument("--dry-run", action="store_true", help="log what would happen, change nothing")
    ap.add_argument("--digest", action="store_true", help="also post the weekly digest (default: on Mondays)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    cfg = tomllib.loads(Path(args.config).read_text())
    dry = args.dry_run or os.environ.get("SENTINEL_DRY_RUN") == "1"
    now = datetime.now(timezone.utc)
    accounts = installations(cfg)
    if not accounts:
        print("No credentials: set SENTINEL_CLIENT_ID and SENTINEL_PRIVATE_KEY, or GITHUB_TOKEN.", file=sys.stderr)
        return 2
    owner = cfg["owner"]
    home_getter = next((t for login, t in accounts if login.lower() == owner.lower()), None)
    home_token = home_getter() if home_getter else None
    ledger = Ledger(GitHub(home_token) if home_token else None, f"{owner}/{cfg.get('home_repo', 'nexonic-sentinel')}",
                    dry_run=dry)

    # One AI budget for the whole run, capped by what is left of today's allowance (free models allow ~50 a day).
    today = now.date().isoformat()
    used_today = int(ledger.get(f"llm:{today}", 0))
    left = max(0, int(cfg.get("daily_llm_budget", 45)) - used_today)
    key = os.environ.get("OPENROUTER_API_KEY", "")
    llm = OpenRouter(key, cfg.get("models", []), budget=min(left, int(cfg.get("max_llm_calls_per_run", 10)))) if key else None

    home_report, others = Report(), Report(private=True)
    other_count = 0
    for login, get_token in accounts:
        home = login.lower() == owner.lower()
        try:
            token = home_token if home else get_token()
        except Exception as exc:  # noqa: BLE001
            (home_report if home else others).errors.append(f"token: {exc.__class__.__name__}")
            other_count += 0 if home else 1
            continue
        icfg = cfg if home else {**cfg, "owner": login, "mention_owner": False}
        bot = Sentinel(GitHub(token), icfg, llm=llm, dry_run=dry, now=now, ledger=ledger, home=home,
                       ai_cap=None if home else int(cfg.get("public_llm_calls_per_install", 3)))
        try:
            report = bot.run()
            if home:
                bot.contributions()
                if args.digest or now.weekday() == 0 and os.environ.get("GITHUB_EVENT_NAME") == "schedule":
                    bot.digest()
        except Exception as exc:  # noqa: BLE001 - one broken installation must not stop the others
            report = bot.report
            report.errors.append(f"{exc.__class__.__name__}" if not home else f"{exc}")
        target = home_report if home else others
        target.actions += report.actions
        target.flagged += report.flagged if home else []
        target.errors += report.errors
        other_count += 0 if home else 1

    if llm is not None:
        ledger.set(f"llm:{today}", used_today + llm.calls)
        cutoff = (now - timedelta(days=7)).date().isoformat()
        for k in [k for k in ledger.data if k.startswith("llm:") and k[4:] < cutoff]:
            del ledger.data[k]
    ledger.save()

    ai = "off (no OPENROUTER_API_KEY)" if llm is None else f"{llm.calls} calls this run, {used_today + llm.calls} today"
    summary = ["## Nexonic Sentinel run", f"Dry run: {dry}. AI: {ai}.", "", "**Your repositories**"]
    summary += [f"- {a}" for a in home_report.actions or ["Nothing to do."]]
    if home_report.flagged:
        summary += ["", "**Waiting on you**"] + [f"- {f}" for f in home_report.flagged]
    if home_report.errors:
        summary += ["", "**Errors**"] + [f"- {e}" for e in home_report.errors]
    if other_count:
        # Other accounts' repository names stay out of this public log.
        summary += ["", f"**Other installations:** {other_count} account{'s' if other_count > 1 else ''}, "
                        f"{len(others.actions)} actions, {len(others.errors)} errors."]
    text = "\n".join(summary)
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return 1 if home_report.errors and not home_report.actions else 0


if __name__ == "__main__":
    sys.exit(main())
