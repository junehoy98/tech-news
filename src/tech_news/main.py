"""CLI entrypoint: fetch -> dedupe -> rank -> synthesize -> send."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from . import mailer, rank, sources, store, synthesize

log = logging.getLogger("tech_news")


def _seconds_until(target_hms: str, tz_name: str, now: datetime | None = None) -> float:
    """Seconds to wait until today's `target_hms` (HH:MM or HH:MM:SS) in `tz_name`.

    Returns 0 if that wall-clock time has already passed today, so a run that
    started late (GitHub's scheduler can lag by hours) sends immediately rather
    than waiting until tomorrow.
    """
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz) if now is None else now.astimezone(tz)
    parts = [int(p) for p in target_hms.split(":")]
    h, m, s = (parts + [0, 0])[:3]
    target = now.replace(hour=h, minute=m, second=s, microsecond=0)
    return max(0.0, (target - now).total_seconds())


def main(argv: list[str] | None = None) -> int:
    project_root = Path(__file__).resolve().parents[2]
    load_dotenv(project_root / ".env")  # no-op if .env missing

    parser = argparse.ArgumentParser(description="Semi equipment news daily digest")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Write HTML to out/digest.html instead of sending email",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate setup (env vars, config files) and exit without doing work",
    )
    parser.add_argument(
        "--reset-seen",
        action="store_true",
        help="Wipe the dedupe DB before running, so all fetched articles are re-processed",
    )
    parser.add_argument(
        "--to",
        default=None,  # resolved after .env is loaded
        help="Recipient email (defaults to DIGEST_TO_ADDRESS env var)",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=project_root,
        help="Project root (auto-detected; rarely needed)",
    )
    parser.add_argument(
        "--target-briefs",
        type=int,
        default=synthesize.DEFAULT_TARGET_BRIEFS,
        help=f"Target number of clustered briefs (default {synthesize.DEFAULT_TARGET_BRIEFS})",
    )
    parser.add_argument(
        "--send-at",
        default=None,
        metavar="HH:MM[:SS]",
        help=(
            "Do all the work now, then wait until this wall-clock time (in --send-tz) "
            "to send. Lets GitHub Actions start the runner early and still deliver at a "
            "fixed time despite the scheduler's multi-hour lag. Ignored with --dry-run."
        ),
    )
    parser.add_argument(
        "--send-tz",
        default="America/New_York",
        help="IANA timezone for --send-at (default America/New_York; auto-handles DST)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable debug logging"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    root: Path = args.root
    sources_path = root / "config" / "sources.toml"
    criteria_path = root / "config" / "criteria.md"
    db_path = root / "data" / "seen.sqlite"

    if args.check:
        return _check_setup(root, sources_path, criteria_path)

    args.to = args.to or os.environ.get("DIGEST_TO_ADDRESS")

    db = store.Store(db_path)
    if args.reset_seen:
        wiped = db.clear()
        log.info("Wiped %d entries from the dedupe DB (--reset-seen)", wiped)

    # In scheduled mode, several staggered runners may start (backups against
    # GitHub dropping the earliest run). The first to send records the date; any
    # later run bails here before doing any fetch/LLM work, so exactly one
    # digest goes out per day.
    send_day = None
    if args.send_at and not args.dry_run:
        send_day = datetime.now(ZoneInfo(args.send_tz)).date().isoformat()
        if db.already_sent(send_day):
            log.info("Digest already sent today (%s) — nothing to do.", send_day)
            return 0

    log.info("Loading sources from %s", sources_path)
    src_list = sources.load_sources(sources_path)
    log.info("Loaded %d sources", len(src_list))

    log.info("Fetching feeds...")
    all_articles = sources.fetch_all(src_list)
    log.info("Fetched %d total articles", len(all_articles))

    db.prune()
    new_articles = db.filter_new(all_articles)
    log.info("%d new articles after dedupe", len(new_articles))

    if not new_articles:
        log.info(
            "Nothing new since the last send — %d articles fetched, all already seen.",
            len(all_articles),
        )
        log.info("To re-process everything (e.g. for testing), rerun with --reset-seen.")
        return 0

    log.info("Scoring %d articles with Haiku...", len(new_articles))
    ranked = rank.rank_articles(new_articles, criteria_path)
    log.info("Got %d scored articles back", len(ranked))

    log.info("Synthesizing briefs with Sonnet...")
    digest = synthesize.synthesize(
        ranked,
        criteria_path=criteria_path,
        total_fetched=len(new_articles),
        target_briefs=args.target_briefs,
    )
    log.info("Digest: %d briefs from %d candidates", len(digest.briefs), digest.total_kept)
    log.info("Email subject would be: %r", f"{digest.email_subject} — {digest.date_short}")

    templates_dir = Path(__file__).resolve().parent / "templates"
    html = mailer.render_html(digest, templates_dir)

    if args.dry_run:
        out_path = root / "out" / "digest.html"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(html, encoding="utf-8")
        log.info("Dry run: wrote %d bytes to %s", len(html), out_path)
        log.info("(Dry runs don't mark articles as seen, so you can iterate freely.)")
        return 0

    if not args.to:
        log.error(
            "No recipient. Set DIGEST_TO_ADDRESS in your .env file, "
            "or pass --to your@email.com on the command line."
        )
        return 2

    from_address = os.environ.get("GMAIL_FROM_ADDRESS")
    app_password = os.environ.get("GMAIL_APP_PASSWORD")
    if not from_address or not app_password:
        log.error(
            "Missing GMAIL_FROM_ADDRESS or GMAIL_APP_PASSWORD. "
            "Add them to your .env file. Run `python -m tech_news.main --check` "
            "to see what's currently set."
        )
        return 2

    if args.send_at:
        wait = _seconds_until(args.send_at, args.send_tz)
        if wait > 0:
            log.info(
                "Digest ready. Holding until %s %s to send (%.0f min from now)...",
                args.send_at, args.send_tz, wait / 60,
            )
            time.sleep(wait)
        else:
            log.info(
                "Send time %s %s already passed — sending now.", args.send_at, args.send_tz
            )

    subject = f"{digest.email_subject} — {digest.date_short}"
    mailer.send(
        html,
        subject=subject,
        from_address=from_address,
        to_address=args.to,
        app_password=app_password,
    )
    db.mark_seen(new_articles)
    if send_day is not None:
        db.mark_sent(send_day)
    log.info("Done.")
    return 0


def _check_setup(root: Path, sources_path: Path, criteria_path: Path) -> int:
    """Print a friendly status of env vars and config files."""
    print(f"Project root: {root}\n")

    print("Configuration files:")
    for label, path in [("Sources", sources_path), ("Criteria", criteria_path)]:
        mark = "OK " if path.exists() else "MISSING"
        print(f"  [{mark}] {label:8} {path}")

    print("\nEnvironment variables:")
    secrets = {
        "ANTHROPIC_API_KEY":   "Anthropic API key (for ranking + intro)",
        "GMAIL_FROM_ADDRESS":  "Gmail address that sends the digest",
        "GMAIL_APP_PASSWORD":  "16-char Gmail app password",
        "DIGEST_TO_ADDRESS":   "Where the digest is delivered",
    }
    missing = []
    for var, desc in secrets.items():
        val = os.environ.get(var)
        if val:
            preview = val[:6] + "..." if len(val) > 10 else "set"
            print(f"  [OK     ] {var:22} ({desc}) = {preview}")
        else:
            print(f"  [MISSING] {var:22} ({desc})")
            missing.append(var)

    env_file = root / ".env"
    if env_file.exists():
        print(f"\n.env file found at {env_file} (auto-loaded)")
    else:
        print(f"\nNo .env file at {env_file}")
        print("Tip: copy .env.example to .env and fill in your values.")

    if missing:
        print(f"\nNeed to set: {', '.join(missing)}")
        return 1
    print("\nAll required secrets present. You're ready to run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
