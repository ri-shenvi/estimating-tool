"""Send digests over SMTP (dev: Mailpit). Graph Mail.Send is a drop-in alternative (SPEC-05 F6)."""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from urllib.parse import urlparse


def send_email(
    *,
    smtp_url: str,
    sender: str,
    to: str,
    subject: str,
    html: str,
    text: str,
    reply_to: str | None = None,
) -> str:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    u = urlparse(smtp_url)
    host, port = u.hostname or "localhost", u.port or (465 if u.scheme == "smtps" else 25)
    cls = smtplib.SMTP_SSL if u.scheme == "smtps" else smtplib.SMTP
    with cls(host, port, timeout=30) as s:
        if u.scheme == "smtp+tls":
            s.starttls()
        if u.username:
            s.login(u.username, u.password or "")
        s.send_message(msg)
    return str(msg["Message-ID"] or "")
