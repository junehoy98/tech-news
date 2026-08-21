"""Render the digest and send via Gmail SMTP."""

from __future__ import annotations

import logging
import re
import smtplib
import time
from email.message import EmailMessage
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import escape

from .synthesize import Digest

# Matches **xyz** but NOT *xyz* (avoid italics false positives) and not **
# crossing newlines. Lazy match so consecutive bolds don't merge.
_BOLD_RE = re.compile(r"\*\*([^*\n]+?)\*\*")


def _bold_md(text: str) -> str:
    """Render markdown **bold** as <strong>, escaping the rest for XSS safety.

    The model is instructed to emphasize key entities with **markdown bold**.
    We escape first (so user-provided text can't inject HTML), then promote
    the literal asterisks to <strong> tags. Result is marked `|safe` in the
    template since we know exactly what we produced.
    """
    if not text:
        return ""
    safe = str(escape(text))
    return _BOLD_RE.sub(r"<strong>\1</strong>", safe)

log = logging.getLogger(__name__)

GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_PORT = 465  # SSL

# A transient SMTP hiccup used to forfeit the whole (paid) pipeline run —
# retry a few times before giving up.
SEND_ATTEMPTS = 3
SEND_RETRY_BACKOFF_S = 20


def render_html(
    digest: Digest,
    templates_dir: Path,
    source_warnings: list[str] | None = None,
) -> str:
    env = Environment(
        loader=FileSystemLoader(templates_dir),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["bold_md"] = _bold_md
    template = env.get_template("digest.html")
    return template.render(digest=digest, source_warnings=source_warnings or [])


def render_text(digest: Digest, source_warnings: list[str] | None = None) -> str:
    """Plain-text alternative body — same content as the HTML, no markup."""

    def _plain(t: str) -> str:
        return (t or "").replace("**", "")

    lines = [f"SEMI DAILY — {digest.date_long}", ""]
    if digest.intro:
        lines += [_plain(digest.intro), ""]
    briefs = ([digest.lead_brief] if digest.lead_brief else []) + list(digest.briefs)
    for b in briefs:
        lines.append(b.headline)
        lines.append(_plain(b.paragraph))
        if b.citations:
            lines.append("  " + " | ".join(f"{c.source}: {c.url}" for c in b.citations))
        lines.append("")
    if source_warnings:
        lines.append("Source health: " + " ; ".join(source_warnings))
        lines.append("")
    return "\n".join(lines)


def send(
    html: str,
    *,
    subject: str,
    from_address: str,
    to_address: str,
    app_password: str,
    text: str | None = None,
) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_address
    msg["To"] = to_address
    msg.set_content(text or "This is an HTML email. View in an HTML-capable client.")
    msg.add_alternative(html, subtype="html")

    _send_with_retry(msg, from_address, app_password)
    log.info("Sent digest to %s", to_address)


def send_text(
    body: str,
    *,
    subject: str,
    from_address: str,
    to_address: str,
    app_password: str,
) -> None:
    """Send a small plain-text message (used for failure alerts)."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_address
    msg["To"] = to_address
    msg.set_content(body)
    _send_with_retry(msg, from_address, app_password)
    log.info("Sent plain-text mail to %s", to_address)


def _send_with_retry(msg: EmailMessage, from_address: str, app_password: str) -> None:
    for attempt in range(1, SEND_ATTEMPTS + 1):
        try:
            with smtplib.SMTP_SSL(GMAIL_SMTP_HOST, GMAIL_SMTP_PORT) as smtp:
                smtp.login(from_address, app_password)
                smtp.send_message(msg)
            return
        except (smtplib.SMTPException, OSError) as e:
            if attempt == SEND_ATTEMPTS:
                raise
            wait = SEND_RETRY_BACKOFF_S * attempt
            log.warning(
                "SMTP send failed (attempt %d/%d): %s — retrying in %ds",
                attempt, SEND_ATTEMPTS, e, wait,
            )
            time.sleep(wait)
