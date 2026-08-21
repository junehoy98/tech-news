"""Tests for SMTP sending: multipart shape, retry behavior, plain-text alerts."""

from __future__ import annotations

import smtplib
from unittest.mock import MagicMock, patch

import pytest

from tech_news import mailer


def _smtp_ctx(smtp_instance) -> MagicMock:
    """A MagicMock standing in for smtplib.SMTP_SSL used as a context manager."""
    ctx = MagicMock()
    ctx.return_value.__enter__.return_value = smtp_instance
    ctx.return_value.__exit__.return_value = False
    return ctx


def test_send_builds_multipart_alternative_and_logs_in():
    smtp = MagicMock()
    with patch.object(mailer.smtplib, "SMTP_SSL", _smtp_ctx(smtp)):
        mailer.send(
            "<html><b>hi</b></html>",
            subject="Subject",
            from_address="from@example.com",
            to_address="to@example.com",
            app_password="pw",
        )

    smtp.login.assert_called_once_with("from@example.com", "pw")
    (msg,) = smtp.send_message.call_args.args
    assert msg["Subject"] == "Subject"
    assert msg["From"] == "from@example.com"
    assert msg["To"] == "to@example.com"
    assert msg.get_content_type() == "multipart/alternative"
    parts = [p.get_content_type() for p in msg.iter_parts()]
    assert parts == ["text/plain", "text/html"]


def test_send_retries_transient_smtp_failure_then_succeeds():
    smtp = MagicMock()
    smtp.send_message.side_effect = [
        smtplib.SMTPServerDisconnected("blip"),
        None,
    ]
    with (
        patch.object(mailer.smtplib, "SMTP_SSL", _smtp_ctx(smtp)),
        patch.object(mailer.time, "sleep") as fake_sleep,
    ):
        mailer.send(
            "<html></html>",
            subject="s",
            from_address="f@example.com",
            to_address="t@example.com",
            app_password="pw",
        )

    assert smtp.send_message.call_count == 2
    fake_sleep.assert_called_once()


def test_send_raises_after_exhausting_retries():
    smtp = MagicMock()
    smtp.login.side_effect = OSError("network down")
    with (
        patch.object(mailer.smtplib, "SMTP_SSL", _smtp_ctx(smtp)),
        patch.object(mailer.time, "sleep"),
        pytest.raises(OSError),
    ):
        mailer.send(
            "<html></html>",
            subject="s",
            from_address="f@example.com",
            to_address="t@example.com",
            app_password="pw",
        )

    assert smtp.login.call_count == mailer.SEND_ATTEMPTS


def test_send_text_is_plain_singlepart():
    smtp = MagicMock()
    with patch.object(mailer.smtplib, "SMTP_SSL", _smtp_ctx(smtp)):
        mailer.send_text(
            "run failed: traceback...",
            subject="Semi Daily FAILED",
            from_address="f@example.com",
            to_address="t@example.com",
            app_password="pw",
        )

    (msg,) = smtp.send_message.call_args.args
    assert msg.get_content_type() == "text/plain"
    assert "run failed" in msg.get_content()
