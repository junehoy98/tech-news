"""Render the digest and send via Gmail SMTP."""

from __future__ import annotations

import logging
import re
import smtplib
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


def render_html(digest: Digest, templates_dir: Path) -> str:
    env = Environment(
        loader=FileSystemLoader(templates_dir),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["bold_md"] = _bold_md
    template = env.get_template("digest.html")
    return template.render(digest=digest)


def send(
    html: str,
    *,
    subject: str,
    from_address: str,
    to_address: str,
    app_password: str,
) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_address
    msg["To"] = to_address
    msg.set_content("This is an HTML email. View in an HTML-capable client.")
    msg.add_alternative(html, subtype="html")

    with smtplib.SMTP_SSL(GMAIL_SMTP_HOST, GMAIL_SMTP_PORT) as smtp:
        smtp.login(from_address, app_password)
        smtp.send_message(msg)
    log.info("Sent digest to %s", to_address)
